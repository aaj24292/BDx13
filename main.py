import asyncio
import html
import logging
import os
import re
import uuid
from contextlib import suppress
from typing import Any

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_TOKEN = os.getenv("API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "1654131109")
PORT = int(os.getenv("PORT", "10000"))
DEFAULT_SPONSOR_CHANNEL = os.getenv("DEFAULT_SPONSOR_CHANNEL", "@BDSM_X13").strip()

if not API_TOKEN:
    raise RuntimeError("Missing required environment variable: API_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Missing required environment variable: DATABASE_URL")
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a Telegram numeric user ID") from exc

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram_video_bot")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db_pool: asyncpg.Pool | None = None
web_runner: web.AppRunner | None = None

# This bot is intended for lawful adult-only content. Do not upload or distribute
# content involving minors, non-consensual material, or material you lack rights to.
AVAILABLE_TAGS = [
    "Oral", "Anal", "Threesome", "Hardcore", "Group",
    "POV", "Amateur", "Asian", "MILF", "Ebony",
    "Lesbian", "Hentai", "BDSM",
]

# Temporary upload sessions expire to prevent stale entries from accumulating.
PENDING_UPLOAD_TTL_SECONDS = 60 * 60
MAX_PACK_SIZE = 100
pending_uploads: dict[str, dict[str, Any]] = {}


def is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_ID


def require_pool() -> asyncpg.Pool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialized")
    return db_pool


def normalize_channel(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = "@" + value.removeprefix("https://t.me/").strip("/")
    if not value.startswith("@"):
        value = "@" + value
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,}", value):
        raise ValueError("Invalid channel username. Use @channelusername.")
    return value


async def init_db() -> None:
    """Create the tables and add compatible columns if an older schema exists."""
    pool = require_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sponsor_channels (
                id SERIAL PRIMARY KEY,
                channel_username TEXT UNIQUE NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_unique_id TEXT UNIQUE NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                unique_code TEXT UNIQUE,
                is_published BOOLEAN NOT NULL DEFAULT FALSE,
                pack_id TEXT
            )
        """)

        # Safe additive migration for databases created by earlier bot versions.
        await conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS file_id TEXT")
        await conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS file_unique_id TEXT")
        await conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS tags TEXT NOT NULL DEFAULT ''")
        await conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS unique_code TEXT")
        await conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS is_published BOOLEAN NOT NULL DEFAULT FALSE")
        await conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS pack_id TEXT")

        # Indexes support exact tag matching and common lookups.
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS videos_file_unique_id_uq
            ON videos (file_unique_id)
            WHERE file_unique_id IS NOT NULL
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS videos_unique_code_uq
            ON videos (unique_code)
            WHERE unique_code IS NOT NULL
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS videos_pack_id_idx
            ON videos (pack_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS videos_published_idx
            ON videos (is_published)
        """)

        if DEFAULT_SPONSOR_CHANNEL:
            try:
                channel = normalize_channel(DEFAULT_SPONSOR_CHANNEL)
            except ValueError:
                logger.warning("DEFAULT_SPONSOR_CHANNEL is invalid; skipping seed")
            else:
                await conn.execute(
                    """
                    INSERT INTO sponsor_channels (channel_username)
                    VALUES ($1)
                    ON CONFLICT (channel_username) DO NOTHING
                    """,
                    channel,
                )


def get_tags_keyboard(request_id: str, selected_tags: list[str] | None = None) -> InlineKeyboardMarkup:
    selected = set(selected_tags or [])
    builder = InlineKeyboardBuilder()
    for tag in AVAILABLE_TAGS:
        label = f"✓ {tag}" if tag in selected else tag
        builder.button(text=label, callback_data=f"tag:{request_id}:{tag}")
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="Save & Finish", callback_data=f"save:{request_id}"))
    return builder.as_markup()


async def get_sponsor_channels() -> list[str]:
    pool = require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT channel_username FROM sponsor_channels ORDER BY id"
        )
    return [row["channel_username"] for row in rows]


async def check_subscription(user_id: int) -> bool:
    """Fail closed: any channel membership verification error denies access."""
    channels = await get_sponsor_channels()
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            status = str(member.status)
            if status in {"left", "kicked"}:
                return False
        except TelegramAPIError:
            logger.exception("Could not verify user %s in sponsor channel %s", user_id, channel)
            return False
    return True


async def delete_message_delayed(chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        logger.debug("Delayed deletion failed for chat=%s message=%s", chat_id, message_id)


async def health_check(_: web.Request) -> web.Response:
    return web.Response(text="Telegram video bot is running.")


async def start_health_server() -> None:
    global web_runner
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    web_runner = web.AppRunner(app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health server listening on port %s", PORT)


async def cleanup_pending_uploads() -> None:
    while True:
        await asyncio.sleep(300)
        now = asyncio.get_running_loop().time()
        expired = [
            key for key, value in pending_uploads.items()
            if now - value["created_at"] > PENDING_UPLOAD_TTL_SECONDS
        ]
        for key in expired:
            pending_uploads.pop(key, None)


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_dashboard(message: types.Message) -> None:
    channels = await get_sponsor_channels()
    channel_lines = "\n".join(f"▫️ {html.escape(ch)}" for ch in channels)
    if not channel_lines:
        channel_lines = "No sponsor channels configured."

    lines = [
        "<b>Admin dashboard</b>",
        "",
        "<b>Mandatory-join channels:</b>",
        channel_lines,
        "",
        "Add channel: <code>/addchannel @channelusername</code>",
        "Delete channel: <code>/delchannel @channelusername</code>",
        "Publish pack: <code>/pack TAG COUNT</code>",
        "Single link: <code>/link v123</code>",
        "",
        "<b>Video statistics (total / published / available):</b>",
    ]

    try:
        pool = require_pool()
        async with pool.acquire() as conn:
            total_all = await conn.fetchval("SELECT COUNT(*) FROM videos") or 0
            for tag in AVAILABLE_TAGS:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM videos WHERE string_to_array(tags, ',') @> ARRAY[$1]::text[]",
                    tag,
                ) or 0
                published = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM videos
                    WHERE string_to_array(tags, ',') @> ARRAY[$1]::text[]
                      AND is_published = TRUE
                    """,
                    tag,
                ) or 0
                if total:
                    lines.append(
                        f"▫️ <b>{html.escape(tag)}:</b> {total} / {published} / 🟢 {total - published}"
                    )
        lines.append(f"\n<b>Total videos:</b> {total_all}")
    except Exception:
        logger.exception("Admin statistics query failed")
        lines.append("\n⚠️ Statistics could not be loaded.")

    await message.reply("\n".join(lines), parse_mode="HTML")


@dp.message(Command("addchannel"), F.from_user.id == ADMIN_ID)
async def add_sponsor_channel(message: types.Message) -> None:
    args = (message.text or "").split()
    if len(args) != 2:
        await message.reply("Usage: /addchannel @channelusername")
        return
    try:
        channel = normalize_channel(args[1])
        # Check that Telegram can resolve this channel for the bot.
        await bot.get_chat(channel)
        pool = require_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO sponsor_channels (channel_username)
                VALUES ($1) ON CONFLICT (channel_username) DO NOTHING
                """,
                channel,
            )
        await message.reply(f"Channel {html.escape(channel)} saved. ({result})", parse_mode="HTML")
    except ValueError as exc:
        await message.reply(str(exc))
    except TelegramAPIError:
        logger.exception("Telegram could not resolve sponsor channel")
        await message.reply("Telegram could not access that channel. Check the username and bot permissions.")
    except Exception:
        logger.exception("Failed to add sponsor channel")
        await message.reply("Could not save the channel due to a database error.")


@dp.message(Command("delchannel"), F.from_user.id == ADMIN_ID)
async def delete_sponsor_channel(message: types.Message) -> None:
    args = (message.text or "").split()
    if len(args) != 2:
        await message.reply("Usage: /delchannel @channelusername")
        return
    try:
        channel = normalize_channel(args[1])
        pool = require_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sponsor_channels WHERE channel_username = $1",
                channel,
            )
        deleted = result.endswith("1")
        await message.reply(
            f"Channel {html.escape(channel)} {'removed' if deleted else 'was not in the list'}.",
            parse_mode="HTML",
        )
    except ValueError as exc:
        await message.reply(str(exc))
    except Exception:
        logger.exception("Failed to delete sponsor channel")
        await message.reply("Could not delete the channel due to a database error.")


@dp.message(F.video, F.from_user.id == ADMIN_ID)
async def admin_upload_video(message: types.Message) -> None:
    if message.video is None:
        return
    video = message.video
    try:
        pool = require_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT unique_code, tags FROM videos WHERE file_unique_id = $1",
                video.file_unique_id,
            )
        if existing:
            await message.reply(
                f"Duplicate video.\nCode: {existing['unique_code'] or 'not assigned'}\n"
                f"Tags: {existing['tags']}",
            )
            return

        request_id = uuid.uuid4().hex[:8]
        pending_uploads[request_id] = {
            "file_id": video.file_id,
            "file_unique_id": video.file_unique_id,
            "tags": [],
            "created_at": asyncio.get_running_loop().time(),
            "admin_id": message.from_user.id if message.from_user else None,
            "chat_id": message.chat.id,
        }
        await message.reply(
            "Video received. Select one or more tags, then tap Save & Finish:",
            reply_markup=get_tags_keyboard(request_id),
        )
    except Exception:
        logger.exception("Video upload initialization failed")
        await message.reply("Could not prepare this upload. Please try again.")


@dp.callback_query(F.data.startswith("tag:"))
async def process_tag_selection(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized.", show_alert=True)
        return
    try:
        _, request_id, tag_name = (callback.data or "").split(":", 2)
    except ValueError:
        await callback.answer("Invalid callback.", show_alert=True)
        return

    data = pending_uploads.get(request_id)
    if not data or data.get("admin_id") != callback.from_user.id:
        await callback.answer("Upload expired or not yours.", show_alert=True)
        return
    if tag_name not in AVAILABLE_TAGS:
        await callback.answer("Invalid tag.", show_alert=True)
        return

    tags = data["tags"]
    if tag_name in tags:
        tags.remove(tag_name)
    else:
        tags.append(tag_name)

    try:
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=get_tags_keyboard(request_id, tags)
            )
        await callback.answer()
    except TelegramAPIError:
        logger.exception("Could not update tag-selection keyboard")
        await callback.answer("Could not update the keyboard.", show_alert=True)


@dp.callback_query(F.data.startswith("save:"))
async def save_video_to_db(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized.", show_alert=True)
        return
    try:
        _, request_id = (callback.data or "").split(":", 1)
    except ValueError:
        await callback.answer("Invalid callback.", show_alert=True)
        return

    data = pending_uploads.get(request_id)
    if not data or data.get("admin_id") != callback.from_user.id:
        await callback.answer("Upload expired or not yours.", show_alert=True)
        return
    if not data["tags"]:
        await callback.answer("Select at least one tag.", show_alert=True)
        return

    tags_string = ",".join(data["tags"])
    try:
        pool = require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                video_id = await conn.fetchval(
                    """
                    INSERT INTO videos (file_id, file_unique_id, tags)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (file_unique_id) DO NOTHING
                    RETURNING id
                    """,
                    data["file_id"],
                    data["file_unique_id"],
                    tags_string,
                )
                if video_id is None:
                    existing = await conn.fetchrow(
                        "SELECT unique_code, tags FROM videos WHERE file_unique_id = $1",
                        data["file_unique_id"],
                    )
                    raise ValueError(
                        f"Duplicate video: {existing['unique_code'] or 'code not assigned'}"
                    )
                unique_code = f"v{video_id}"
                await conn.execute(
                    "UPDATE videos SET unique_code = $1 WHERE id = $2",
                    unique_code,
                    video_id,
                )

        pending_uploads.pop(request_id, None)
        if callback.message:
            await callback.message.edit_text(
                f"Saved successfully.\nCode: <code>{unique_code}</code>\n"
                f"Tags: {html.escape(tags_string)}",
                parse_mode="HTML",
            )
        await callback.answer("Saved.")
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
    except Exception:
        logger.exception("Could not save video to database")
        await callback.answer("Database error. The upload remains available to retry.", show_alert=True)


@dp.message(Command("pack"), F.from_user.id == ADMIN_ID)
async def create_pack(message: types.Message) -> None:
    args = (message.text or "").split()
    if len(args) != 3:
        await message.reply("Usage: /pack TAG COUNT")
        return

    target_tag = args[1]
    if target_tag not in AVAILABLE_TAGS:
        await message.reply("Unknown tag. Use one of the configured tags.")
        return
    try:
        limit = int(args[2])
    except ValueError:
        await message.reply("COUNT must be an integer.")
        return
    if not 1 <= limit <= MAX_PACK_SIZE:
        await message.reply(f"COUNT must be between 1 and {MAX_PACK_SIZE}.")
        return

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    pool = require_pool()
    ids: list[int] = []
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT id, unique_code
                    FROM videos
                    WHERE string_to_array(tags, ',') @> ARRAY[$1]::text[]
                      AND is_published = FALSE
                      AND unique_code IS NOT NULL
                    ORDER BY id
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                    """,
                    target_tag,
                    limit,
                )
                if not rows:
                    await message.reply(f"No unpublished videos found for tag: {target_tag}")
                    return

                ids = [row["id"] for row in rows]
                await conn.execute(
                    """
                    UPDATE videos
                    SET is_published = TRUE, pack_id = $1
                    WHERE id = ANY($2::int[])
                    """,
                    batch_id,
                    ids,
                )

        bot_info = await bot.get_me()
        bot_username = bot_info.username
        if not bot_username:
            raise RuntimeError("Bot username is unavailable")

        response_lines = [f"#{html.escape(target_tag)}", ""]
        for row in rows:
            link = f"https://t.me/{bot_username}?start={row['unique_code']}"
            response_lines.append(f"<a href='{link}'>Video {row['id']}</a>")
        batch_link = f"https://t.me/{bot_username}?start={batch_id}"
        response_lines.extend(["", f"<a href='{batch_link}'>Download All</a>", "", "#V@BDSM_X13"])

        await message.reply(
            "\n".join(response_lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await message.reply(f"{len(rows)} videos published in this pack.")
    except Exception:
        logger.exception("Pack publishing failed")
        if ids:
            with suppress(Exception):
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE videos
                        SET is_published = FALSE, pack_id = NULL
                        WHERE pack_id = $1
                        """,
                        batch_id,
                    )
        await message.reply("Pack publishing failed. Selected videos were reset when possible.")


@dp.message(Command("link"), F.from_user.id == ADMIN_ID)
async def get_single_link(message: types.Message) -> None:
    args = (message.text or "").split()
    if len(args) != 2:
        await message.reply("Usage: /link v123 (or /link 123)")
        return
    target_code = args[1].lower()
    if not target_code.startswith("v"):
        target_code = f"v{target_code}"
    if not re.fullmatch(r"v[0-9]+", target_code):
        await message.reply("Invalid video code.")
        return

    try:
        pool = require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, unique_code FROM videos WHERE unique_code = $1",
                target_code,
            )
        if not row:
            await message.reply("Video not found.")
            return

        bot_info = await bot.get_me()
        if not bot_info.username:
            raise RuntimeError("Bot username is unavailable")
        link = f"https://t.me/{bot_info.username}?start={row['unique_code']}"
        await message.reply(
            f"<a href='{link}'>Video {row['id']}</a>\n\n#V@BDSM_X13",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE videos SET is_published = TRUE WHERE unique_code = $1",
                target_code,
            )
    except Exception:
        logger.exception("Single link creation failed")
        await message.reply("Could not create the video link.")


# ---------------------------------------------------------------------------
# User flow
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def user_start(message: types.Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else None

    if not await check_subscription(message.from_user.id):
        builder = InlineKeyboardBuilder()
        channels = await get_sponsor_channels()
        for index, channel in enumerate(channels, 1):
            url_channel = channel.removeprefix("@")
            builder.button(
                text=f"📢 Join Channel {index}",
                url=f"https://t.me/{url_channel}",
            )
        bot_info = await bot.get_me()
        if not bot_info.username:
            await message.reply("Bot configuration error. Please try again later.")
            return
        start_param = f"?start={payload}" if payload else ""
        retry_url = f"https://t.me/{bot_info.username}{start_param}"
        builder.row(InlineKeyboardButton(text="✅ Joined / Check Again", url=retry_url))
        await message.reply(
            "🔒 You must join all sponsor channels before accessing the content.",
            reply_markup=builder.as_markup(),
        )
        return

    if not payload:
        await message.reply("Use a valid video link from the channel.")
        return

    pool = require_pool()
    if payload.startswith("batch_"):
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT file_id, tags FROM videos WHERE pack_id = $1 ORDER BY id",
                    payload,
                )
            if not rows:
                await message.reply("This pack is empty or invalid.")
                return

            await message.reply("📥 Download started. Videos will be sent one by one.")
            sent_count = 0
            failed_count = 0
            for index, row in enumerate(rows):
                tag_list = [tag.strip() for tag in row["tags"].split(",") if tag.strip()]
                hashtags = " ".join(
                    f"#{tag.replace(' ', '_')}" for tag in tag_list
                )
                caption = f"{html.escape(hashtags)}\n\n#V@BDSM_X13"
                warning = "Warning: this video will be deleted in 40 seconds. Save it now!"
                try:
                    sent_video = await bot.send_video(
                        chat_id=message.chat.id,
                        video=row["file_id"],
                        caption=caption,
                        parse_mode="HTML",
                    )
                    sent_warning = await bot.send_message(
                        chat_id=message.chat.id,
                        text=warning,
                    )
                    asyncio.create_task(
                        delete_message_delayed(message.chat.id, sent_video.message_id, 43)
                    )
                    asyncio.create_task(
                        delete_message_delayed(message.chat.id, sent_warning.message_id, 43)
                    )
                    sent_count += 1
                except TelegramAPIError:
                    logger.exception("Could not send a pack video")
                    failed_count += 1
                if index < len(rows) - 1:
                    await asyncio.sleep(2)

            await message.reply(
                f"Pack completed. Sent: {sent_count}. Failed: {failed_count}."
            )
        except Exception:
            logger.exception("Pack delivery failed")
            await message.reply("Could not deliver this pack. Please try again later.")
        return

    if not re.fullmatch(r"v[0-9]+", payload):
        await message.reply("Invalid video link.")
        return

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT file_id, tags FROM videos WHERE unique_code = $1",
                payload,
            )
        if not row:
            await message.reply("Video not found.")
            return

        tags = [tag.strip() for tag in row["tags"].split(",") if tag.strip()]
        hashtags = " ".join(f"#{tag.replace(' ', '_')}" for tag in tags)
        caption = f"{html.escape(hashtags)}\n\n#V@BDSM_X13"
        warning = "Warning: this video will be deleted in 40 seconds. Save it now!"
        sent_video = await bot.send_video(
            chat_id=message.chat.id,
            video=row["file_id"],
            caption=caption,
            parse_mode="HTML",
        )
        sent_warning = await bot.send_message(chat_id=message.chat.id, text=warning)
        asyncio.create_task(
            delete_message_delayed(message.chat.id, sent_video.message_id, 43)
        )
        asyncio.create_task(
            delete_message_delayed(message.chat.id, sent_warning.message_id, 43)
        )
    except TelegramAPIError:
        logger.exception("Video delivery failed")
        await message.reply("Telegram could not send this video. It may be unavailable.")
    except Exception:
        logger.exception("Unexpected error in single-video delivery")
        await message.reply("System is temporarily busy. Please try again.")


async def main() -> None:
    global db_pool
    cleanup_task: asyncio.Task | None = None
    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
        await init_db()
        await start_health_server()
        cleanup_task = asyncio.create_task(cleanup_pending_uploads())
        logger.info("Bot is starting polling")
        await dp.start_polling(bot)
    finally:
        if cleanup_task:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
        if web_runner:
            await web_runner.cleanup()
        if db_pool:
            await db_pool.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
