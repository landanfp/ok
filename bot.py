import os
import uuid
import asyncio
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse
from functools import partial

from yt_dlp import YoutubeDL
from pyrogram import Client, filters, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from fastapi import FastAPI
from uvicorn import Config, Server

load_dotenv()

# --- Constants ---
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8000))
REQUEST_TTL = 3600  # 1 hour expiration for format selection

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("One or more environment variables (API_ID, API_HASH, BOT_TOKEN) are not set.")

# --- Globals & App Initialization ---
app = Client("okru_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
fastapi_app = FastAPI()
EXTRACTS = {}

# --- Helper Functions ---
def fmt_label(f):
    ext = f.get("ext", "")
    resolution = f.get("format_note") or f.get("resolution") or f.get("format", "")
    filesize_bytes = f.get("filesize") or f.get("filesize_approx")
    size_str = f"{(filesize_bytes / (1024*1024)):.1f} MB" if filesize_bytes else "—"
    format_id = f.get("format_id", "")
    return f"{resolution} — {ext} ({size_str}) [{format_id}]"

def format_speed(speed_bytes):
    if speed_bytes > 1024 * 1024:
        return f"{speed_bytes / (1024 * 1024):.2f} MB/s"
    return f"{speed_bytes / 1024:.2f} KB/s"

# --- FastAPI Health Check ---
@fastapi_app.get("/health")
async def health_check():
    return {"status": "healthy"}

# --- Pyrogram Handlers ---
@app.on_message(filters.command("start"))
async def start_handler(_, msg):
    await msg.reply_text(
        "سلام! لینک ویدیو یا فایل را برای دانلود ارسال کنید.\n"
        "مثال: `https://ok.ru/video/12345`\n"
        "مثال لینک مستقیم: `https://link.com/file.zip`\n\n"
        "برای تغییر نام فایل، بعد از لینک `|` و نام جدید را اضافه کنید:\n"
        "مثال: `https://link.com/video|my_custom_name.mp4`"
    )

@app.on_message(filters.private & filters.text & ~filters.command("start"))
async def extract_formats_handler(client, msg):
    url, _, custom_name = msg.text.strip().partition("|")
    url = url.strip()
    custom_name = custom_name.strip() or None

    if not url.startswith("http"):
        await msg.reply_text("لطفاً یک لینک معتبر ارسال کنید.")
        return

    processing_msg = await msg.reply_text("⏳ در حال پردازش لینک...")
    
    ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    
    try:
        loop = asyncio.get_event_loop()
        with YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, partial(ydl.extract_info, url, download=False))
    except Exception as e:
        await processing_msg.edit_text(f"❌ خطا در پردازش لینک: `{e}`")
        return

    # --- NEW: Direct Link Detection ---
    # Direct links often have no 'formats' but a top-level 'url'
    is_direct = not info.get('formats') and 'url' in info

    if is_direct:
        await processing_msg.edit_text("🔗 **لینک مستقیم شناسایی شد.**\nدر حال آماده‌سازی برای دانلود...")
        # Try to get a title, otherwise use the filename from the URL
        title = info.get('title') or Path(urlparse(info['url']).path).name
        with tempfile.TemporaryDirectory() as tmpdir:
            await process_video(
                status_msg=processing_msg,
                url=info['url'],
                title=title,
                custom_name=custom_name,
                tmp_path=Path(tmpdir),
                fid=None  # No format ID needed for direct links
            )
        return

    # --- Existing Logic for Video Pages with Multiple Formats ---
    title = info.get("title", "Untitled Video")
    formats = info.get("formats", []) or ([info] if "format_id" in info else [])
    
    fmts = {f["format_id"]: f for f in formats if f.get("format_id") and f.get("ext")}

    if not fmts:
        await processing_msg.edit_text("هیچ فرمت قابل دانلودی پیدا نشد.")
        return

    key = str(uuid.uuid4())
    EXTRACTS[key] = {
        "url": url, "title": title, "formats": fmts, "user_id": msg.from_user.id, 
        "custom_name": custom_name, "timestamp": time.time()
    }

    buttons = [
        [InlineKeyboardButton(fmt_label(meta), callback_data=f"DL|{key}|{fid}")]
        for fid, meta in list(fmts.items())[:25]
    ]
    
    await processing_msg.edit_text(
        f"🎬 **عنوان:** `{title}`\n\nفرمت مورد نظر را برای دانلود انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex(r"^DL\|"))
async def on_select_format_handler(client, cq):
    try:
        _, key, fid = cq.data.split("|", 2)
    except ValueError:
        return await cq.answer("داده نامعتبر.", show_alert=True)

    record = EXTRACTS.get(key)
    
    if not record or record["user_id"] != cq.from_user.id or (time.time() - record.get("timestamp", 0) > REQUEST_TTL):
        await cq.answer("این درخواست منقضی شده یا نامعتبر است.", show_alert=True)
        return await cq.message.edit_text("درخواست منقضی شده است.")
        
    record = EXTRACTS.pop(key)
    
    await cq.answer("درخواست شما پذیرفته شد...", show_alert=False)
    await cq.message.edit_text(f"🚀 آماده‌سازی برای دانلود: `{record['title']}`")

    with tempfile.TemporaryDirectory() as tmpdir:
        await process_video(
            status_msg=cq.message,
            url=record['url'],
            title=record['title'],
            custom_name=record['custom_name'],
            tmp_path=Path(tmpdir),
            fid=fid
        )

# --- REFACTORED: Unified Download & Upload Logic ---
async def process_video(status_msg, url, title, custom_name, tmp_path, fid=None):
    loop = asyncio.get_event_loop()
    last_update_time = 0

    def progress_hook(d):
        nonlocal last_update_time
        if d['status'] == 'downloading':
            now = loop.time()
            if now - last_update_time < 5: return
            last_update_time = now
            
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            speed = d.get('speed', 0)
            percent = (downloaded / total * 100) if total > 0 else 0
            
            text = (f"⬇️ **در حال دانلود...**\n`{title}`\n\n"
                    f"**پیشرفت:** `{percent:.1f}%`\n"
                    f"**حجم:** `{downloaded/1e6:.1f} / {total/1e6:.1f} MB`\n"
                    f"**سرعت:** `{format_speed(speed)}`")
            with suppress(errors.MessageNotModified):
                asyncio.run_coroutine_threadsafe(status_msg.edit_text(text), loop)

    ydl_opts = {
        "outtmpl": str(tmp_path / "%(title)s.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "external_downloader": "aria2c",
        "external_downloader_args": ["-x", "16", "-s", "16", "-k", "1M"],
        "progress_hooks": [progress_hook],
    }
    if fid:
        ydl_opts["format"] = fid

    try:
        with YoutubeDL(ydl_opts) as ydl:
            await loop.run_in_executor(None, partial(ydl.download, [url]))
        
        downloaded_files = list(tmp_path.iterdir())
        if not downloaded_files:
            raise FileNotFoundError("فایل دانلود شده پیدا نشد.")
        file_path = downloaded_files[0]
    except Exception as e:
        return await status_msg.edit_text(f"❌ **خطا در دانلود:**\n`{e}`")

    async def upload_progress(current, total):
        nonlocal last_update_time
        now = loop.time()
        if now - last_update_time < 5 and current != total: return
        last_update_time = now
        percent = (current / total * 100)
        text = (f"⬆️ **در حال آپلود...**\n`{upload_filename}`\n\n"
                f"**پیشرفت:** `{percent:.1f}%`\n"
                f"**حجم:** `{current/1e6:.1f} / {total/1e6:.1f} MB`")
        with suppress(errors.MessageNotModified):
            await status_msg.edit_text(text)
            
    original_filename = file_path.name
    upload_filename = custom_name or original_filename
    await status_msg.edit_text(f"✅ دانلود کامل شد.\n⬆️ شروع آپلود: `{upload_filename}`")
    
    try:
        await app.send_document(
            chat_id=status_msg.chat.id, document=str(file_path),
            file_name=upload_filename, caption=upload_filename,
            progress=upload_progress
        )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ **خطا در آپلود:**\n`{e}`")

# --- Main Execution ---
def run_fastapi():
    config = Config(app=fastapi_app, host="0.0.0.0", port=PORT, loop="asyncio")
    server = Server(config)
    loop = asyncio.get_event_loop()
    loop.create_task(server.serve())

if __name__ == "__main__":
    print("Bot is starting...")
    app.start()
    print("Pyrogram client started.")
    run_fastapi()
    print(f"FastAPI server will run on port {PORT}.")
    print("Bot and server are running!")
    asyncio.get_event_loop().run_forever()
