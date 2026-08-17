import asyncio
import logging
import os
import uuid
import html
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ==========================================
# تنظیمات امنیتی (از متغیرهای محیطی خوانده می‌شود)
# ==========================================
API_TOKEN = os.environ.get('API_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_ID = 1654131109  # آیدی شما

SPONSOR_CHANNELS = [
    '@BDSM_X13'  # کانال شما
]
# ==========================================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
db_pool = None

PENDING_UPLOADS = {}

AVAILABLE_TAGS = [
    "Oral", "Anal", "Threesome", "Hardcore", "Group", 
    "POV", "Amateur", "Asian", "Milf", "Teen", 
    "Ebony", "Lesbian", "Hentai", "BDSM"
]

# --- وب‌سرور فیک برای زنده ماندن در Render ---
async def health_check(request):
    return web.Response(text="BDSM_X13 Bot is running perfectly!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Dummy web server started on port {port}")

# --- توابع کمکی ---

def get_tags_keyboard(request_id, selected_tags=None):
    if selected_tags is None:
        selected_tags = []
    builder = InlineKeyboardBuilder()
    for tag in AVAILABLE_TAGS:
        text = f"[*] {tag}" if tag in selected_tags else tag
        callback_data = f"tag:{request_id}:{tag}"
        builder.button(text=text, callback_data=callback_data)
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="Save & Finish", callback_data=f"save:{request_id}"))
    return builder.as_markup()

async def check_subscription(user_id):
    if not SPONSOR_CHANNELS:
        return True
    for channel in SPONSOR_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception:
            continue
    return True

async def delete_message_delayed(chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

# --- بخش ادمین ---

@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_dashboard(message: types.Message):
    help_text = (
        "<b>👮‍♂️ داشبورد مدیریت</b>\n\n"
        "🔹 <code>/pack Oral 10</code>\n"
        "🔹 <code>/link v50</code>\n\n"
        "<b>📊 آمار:</b> (کل / منتشر شده / <b>آماده</b>)\n\n"
    )
    stats_text = ""
    try:
        async with db_pool.acquire() as conn:
            total_all = 0
            for tag in AVAILABLE_TAGS:
                total = await conn.fetchval("SELECT COUNT(*) FROM videos WHERE tags LIKE $1", f'%{tag}%')
                published = await conn.fetchval("SELECT COUNT(*) FROM videos WHERE tags LIKE $1 AND is_published = TRUE", f'%{tag}%')
                ready = total - published
                total_all += total
                if total > 0:
                    stats_text += f"▫️ <b>{tag}:</b> {total} / {published} / 🟢 <b>{ready}</b>\n"
            help_text += stats_text
            help_text += f"\n📦 <b>Total Videos:</b> {total_all}"
    except Exception as e:
        help_text += f"\n⚠️ Error: {e}"
    await message.reply(help_text, parse_mode="HTML")

@dp.message(F.video, F.from_user.id == ADMIN_ID)
async def admin_upload_video(message: types.Message):
    file_id = message.video.file_id
    file_unique_id = message.video.file_unique_id
    
    try:
        async with db_pool.acquire() as conn:
            existing_video = await conn.fetchrow("SELECT unique_code, tags FROM videos WHERE file_unique_id = $1", file_unique_id)
        
        if existing_video:
            code, tags = existing_video['unique_code'], existing_video['tags']
            await message.reply(f"! Duplicate Video !\nCode: `{code}`\nTags: `{tags}`", parse_mode="Markdown")
            return

        request_id = str(uuid.uuid4())[:8]
        PENDING_UPLOADS[request_id] = {'file_id': file_id, 'file_unique_id': file_unique_id, 'tags': []}
        await message.reply("Video received. Select tags:", reply_markup=get_tags_keyboard(request_id, []))
    except Exception as e:
        await message.reply(f"DB Error: {e}")

@dp.callback_query(F.data.startswith('tag:'))
async def process_tag_selection(callback_query: types.CallbackQuery):
    _, request_id, tag_name = callback_query.data.split(':')
    if request_id not in PENDING_UPLOADS:
        await callback_query.answer("Expired.", show_alert=True)
        return
    current_data = PENDING_UPLOADS[request_id]
    if tag_name in current_data['tags']:
        current_data['tags'].remove(tag_name)
    else:
        current_data['tags'].append(tag_name)
    PENDING_UPLOADS[request_id]['tags'] = current_data['tags']
    await callback_query.message.edit_reply_markup(reply_markup=get_tags_keyboard(request_id, current_data['tags']))
    await callback_query.answer()

@dp.callback_query(F.data.startswith('save:'))
async def save_video_to_db(callback_query: types.CallbackQuery):
    _, request_id = callback_query.data.split(':')
    if request_id not in PENDING_UPLOADS:
        await callback_query.answer("Expired.", show_alert=True)
        return
    data = PENDING_UPLOADS[request_id]
    if not data['tags']:
        await callback_query.answer("Select tags!", show_alert=True)
        return
    tags_str = ",".join(data['tags'])
    
    try:
        async with db_pool.acquire() as conn:
            # اینسرت و دریافت آیدی در یک خط
            video_id = await conn.fetchval(
                "INSERT INTO videos (file_id, file_unique_id, tags) VALUES ($1, $2, $3) RETURNING id",
                data['file_id'], data['file_unique_id'], tags_str
            )
            unique_code = f"v{video_id}"
            await conn.execute("UPDATE videos SET unique_code = $1 WHERE id = $2", unique_code, video_id)
            
        del PENDING_UPLOADS[request_id]
        await callback_query.message.edit_text(f"Saved.\nCode: `{unique_code}`\nTags: {tags_str}", parse_mode="Markdown")
    except Exception as e:
        await callback_query.answer(f"Error: {e}", show_alert=True)

@dp.message(Command("pack"), F.from_user.id == ADMIN_ID)
async def create_pack(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 3:
            await message.reply("Format: `/pack Category Count`")
            return
        target_tag = args[1]
        limit = int(args[2])
        
        async with db_pool.acquire() as conn:
            videos = await conn.fetch("SELECT id, unique_code, tags FROM videos WHERE tags LIKE $1 AND is_published = FALSE LIMIT $2", f'%{target_tag}%', limit)
            
            if not videos:
                await message.reply(f"No videos found for: {target_tag}")
                return
                
            bot_info = await bot.get_me()
            bot_username = bot_info.username
            batch_id = f"batch_{str(uuid.uuid4())[:8]}"
            
            response_text = f"#{html.escape(target_tag)}\n\n"
            ids_to_update = []
            
            for vid in videos:
                vid_id, code = vid['id'], vid['unique_code']
                ids_to_update.append(vid_id)
                link = f"https://t.me/{bot_username}?start={code}"
                response_text += f"<a href='{link}'>Video {vid_id}</a>\n"
            
            batch_link = f"https://t.me/{bot_username}?start={batch_id}"
            response_text += f"\n<a href='{batch_link}'>Download All</a>\n\n#V@BDSM_X13"
            
            await message.reply(response_text, parse_mode="HTML", disable_web_page_preview=True)
            
            # آپدیت دسته‌جمعی رکوردها
            await conn.execute("UPDATE videos SET is_published = TRUE, pack_id = $1 WHERE id = ANY($2::int[])", batch_id, ids_to_update)
            
            await message.reply(f"{len(videos)} videos marked as published.")
        
    except Exception as e:
        await message.reply(f"Error: {e}")

@dp.message(Command("link"), F.from_user.id == ADMIN_ID)
async def get_single_link(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.reply("Format: `/link code`")
            return
        target_code = args[1]
        if not target_code.startswith('v'):
            target_code = f"v{target_code}"
            
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("SELECT id, unique_code FROM videos WHERE unique_code = $1", target_code)
            
            if not result:
                await message.reply("Not found.")
                return
                
            vid_id, code = result['id'], result['unique_code']
            bot_info = await bot.get_me()
            bot_username = bot_info.username
            link = f"https://t.me/{bot_username}?start={code}"
            
            response_text = f"<a href='{link}'>Video {vid_id}</a>\n\n#V@BDSM_X13"
            await message.reply(response_text, parse_mode="HTML", disable_web_page_preview=True)
            
            await conn.execute("UPDATE videos SET is_published = TRUE WHERE unique_code = $1", target_code)
    except Exception as e:
        await message.reply(f"Error: {e}")

# --- بخش کاربر ---

@dp.message(CommandStart())
async def user_start(message: types.Message):
    args = message.text.split(' ')
    payload = args[1] if len(args) > 1 else None
    
    if not await check_subscription(message.from_user.id):
        builder = InlineKeyboardBuilder()
        for ch in SPONSOR_CHANNELS:
            url_channel = ch.replace('@', '')
            url = f"https://t.me/{url_channel}"
            builder.button(text="Join Channel", url=url)
        
        bot_info = await bot.get_me()
        start_param = f"?start={payload}" if payload else ""
        retry_url = f"https://t.me/{bot_info.username}{start_param}"
        builder.row(InlineKeyboardButton(text="Joined / Watch Video", url=retry_url))
        await message.reply("🔒 **To watch the video, you must join our channel first:**", reply_markup=builder.as_markup(), parse_mode="Markdown")
        return

    if not payload:
        await message.reply("To receive a file, please use a valid link from our channel.\n#V@BDSM_X13")
        return

    if payload.startswith("batch_"):
        batch_id = payload
        await message.reply("📥 <b>Starting Download...</b>\nVideos will be sent one by one.", parse_mode="HTML")
        try:
            async with db_pool.acquire() as conn:
                videos_to_send = await conn.fetch("SELECT file_id, tags FROM videos WHERE pack_id = $1", batch_id)
            
            if not videos_to_send:
                await message.reply("❌ This pack is empty or invalid.")
                return

            for index, vid in enumerate(videos_to_send):
                file_id, tags = vid['file_id'], vid['tags']
                tag_list = tags.split(',')
                hashtags_str = " ".join([f"#{t.strip().replace(' ', '_')}" for t in tag_list])
                caption = f"{hashtags_str}\n\n#V@BDSM_X13"
                warning_text = "Warning: This video will be deleted in 40 seconds!\nSave it immediately!"
                
                try:
                    sent_vid = await bot.send_video(chat_id=message.chat.id, video=file_id, caption=caption, parse_mode="HTML")
                    sent_warn = await bot.send_message(chat_id=message.chat.id, text=warning_text)
                    asyncio.create_task(delete_message_delayed(message.chat.id, sent_vid.message_id, 43))
                    asyncio.create_task(delete_message_delayed(message.chat.id, sent_warn.message_id, 43))
                    if index < len(videos_to_send) - 1:
                        await asyncio.sleep(2) 
                except Exception:
                    pass
            await message.reply("✅ All videos sent.")
        except Exception:
            await message.reply("Error sending pack.")
        return

    unique_code = payload
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow("SELECT file_id, tags FROM videos WHERE unique_code = $1", unique_code)
        
        if result:
            file_id, tags = result['file_id'], result['tags']
            tag_list = tags.split(',')
            hashtags_str = " ".join([f"#{t.strip().replace(' ', '_')}" for t in tag_list])
            caption = f"{hashtags_str}\n\n#V@BDSM_X13"
            warning_text = "Warning: This video will be deleted in 40 seconds!\nSave it immediately!"
            
            try:
                sent_vid = await bot.send_video(chat_id=message.chat.id, video=file_id, caption=caption, parse_mode="HTML")
                sent_warning = await bot.send_message(chat_id=message.chat.id, text=warning_text)
                asyncio.create_task(delete_message_delayed(message.chat.id, sent_vid.message_id, 43))
                asyncio.create_task(delete_message_delayed(message.chat.id, sent_warning.message_id, 43))
            except Exception:
                await message.reply("Video not found or deleted from server.")
        else:
            await message.reply("Video not found.")
            
    except Exception:
        await message.reply("System busy.")

async def main():
    # ۱. استارت وب‌سرور فیک
    await start_dummy_server()
    
    # ۲. اتصال به دیتابیس نئون
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    logging.info("Connected to Neon Database successfully!")
    
    # ۳. استارت ربات
    logging.info("Bot is polling on Render...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())