import os
import uuid
import asyncio
import tempfile
import threading
import json
import redis.asyncio as redis
from yt_dlp import YoutubeDL
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from fastapi import FastAPI
from uvicorn import Config, Server

# Load environment variables
load_dotenv()

API_ID = os.getenv("API_ID", "3335796")
API_HASH = os.getenv("API_HASH", "138b992a0e672e8346d8439c3f42ea78")
BOT_TOKEN = os.getenv("BOT_TOKEN", "1806450812:AAGhHSWPd3sH5SVFBB8_Xadw_SbdbvZm0_Q")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("API_ID, API_HASH or BOT_TOKEN are not set in the .env file.")

app = Client("okru_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
redis_client = None

# FastAPI for Health Check
fastapi_app = FastAPI()

@fastapi_app.get("/health")
async def health_check():
    return {"status": "healthy"}

# Helper function to format format descriptions
def fmt_label(f):
    ext = f.get("ext") or ""
    vcodec = f.get("vcodec") or ""
    acodec = f.get("acodec") or ""
    resolution = f.get("format_note") or f.get("resolution") or f.get("format") or ""
    filesize = None
    if f.get("filesize") is None:
        if f.get("filesize_approx"):
            filesize = f["filesize_approx"]
    else:
        filesize = f["filesize"]
    if filesize:
        mb = filesize / (1024*1024)
        size_s = f"{mb:.1f} MB"
    else:
        size_s = "—"
    
    format_id = f.get("format_id") or ""
    return f"{resolution} — {ext} ({size_s}) - {format_id}"

# Helper function to format speed
def format_speed(speed_bytes_per_second):
    speed_kb = speed_bytes_per_second / 1024
    if speed_kb >= 1024:
        return f"{speed_kb / 1024:.2f} MB/s"
    else:
        return f"{speed_kb:.2f} KB/s"

# Handler: /start
@app.on_message(filters.command("start"))
async def start(_, msg):
    await msg.reply_text("سلام! لینک ویدیو بفرست تا فرمت‌ها استخراج بشن و بتونی دانلود و آپلود کنی.\nمثال: `https://ok.ru/video/....`\nبرای تغییر نام فایل، بعد از لینک `|` و نام جدید را اضافه کن.\nمثال: `https://link-to-video.com/video|my_new_video.mp4`")

# Handler: Messages containing a URL
@app.on_message(filters.private & filters.text)
async def extract_formats(client, msg):
    full_text = msg.text.strip()
    custom_name = None
    url = full_text
    
    if "|" in full_text:
        parts = full_text.split("|", 1)
        url = parts[0].strip()
        custom_name = parts[1].strip()

    if not url.startswith("http"):
        return

    processing = await msg.reply_text("⏳ در حال پردازش و استخراج فرمت‌ها...")
    key = str(uuid.uuid4())
    
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "format": "best",
        "extractor_args": {
            'youtube': {'--skip-unavailable-fragments': True},
            'okru': {'--geo-bypass': True}
        }
    }
    
    try:
        info = await asyncio.to_thread(YoutubeDL(ydl_opts).extract_info, url, download=False)
    except Exception as e:
        error_message = f"❌ خطا در استخراج فرمت: {e}"
        if "unable to download video" in str(e).lower():
            error_message += "\nاین لینک به دلیل محدودیت جغرافیایی یا خصوصی بودن قابل دسترسی نیست. ممکن است نیاز به استفاده از VPN باشد."
        await processing.edit_text(error_message)
        return

    title = info.get("title", "video")
    formats = info.get("formats", []) or [info]
    
    fmts = {}
    for f in formats:
        fid = f.get("format_id") or f.get("format")
        if not fid:
            continue
        if fid in fmts:
            continue
        fmts[fid] = {
            "format_id": fid,
            "ext": f.get("ext"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "resolution": f.get("format_note") or f.get("resolution"),
            "filesize": f.get("filesize") or f.get("filesize_approx")
        }
    
    record = {"url": url, "title": title, "formats": fmts, "user_id": msg.from_user.id, "custom_name": custom_name}
    await redis_client.set(key, json.dumps(record), ex=3600)
    
    buttons = []
    count = 0
    for fid, meta in fmts.items():
        label = fmt_label(meta)
        cb = f"DL|{key}|{fid}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])
        count += 1
        if count >= 20:
            break

    if not buttons:
        await processing.edit_text("هیچ فرمتی پیدا نشد.")
        await redis_client.delete(key)
        return

    await processing.edit_text(
        f"🎬 عنوان: {title}\nانتخاب فرمت برای دانلود و آپلود:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# Handler: CallbackQuery for format selection
@app.on_callback_query(filters.regex(r"^DL\|"))
async def on_select_format(client, cq):
    data = cq.data
    await cq.answer("درخواست دریافت شد. آماده دانلود می‌شوم...", show_alert=False)
    
    try:
        _, key, fid = data.split("|", 2)
    except Exception:
        await cq.message.edit_text("داده نامعتبر.")
        return

    record_json = await redis_client.get(key)
    if not record_json:
        await cq.message.edit_text("کد منقضی شده یا اطلاعات پیدا نشد. دوباره لینک را بفرستید.")
        return
    
    record = json.loads(record_json)

    url = record["url"]
    title = record["title"]
    custom_name = record.get("custom_name")

    tmpdir = tempfile.mkdtemp(prefix="okru_dl_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

    ydl_opts = {
        "format": fid,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "no_warnings": True,
        "quiet": True,
        "external_downloader": "aria2c",
        "external_downloader_args": ["-x", "16", "-s", "16", "--file-allocation=none"],
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [],
    }
    
    status_msg = cq.message
    await status_msg.edit_text(f"⬇️ شروع دانلود: {title}\nفرمت: {fid}\nدر حال دانلود ...")

    loop = asyncio.get_event_loop()
    last_update = 0
    def progress_hook(d):
        nonlocal last_update
        status = d.get("status")
        if status == "downloading":
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            now = asyncio.get_event_loop().time()
            if now - last_update < 5:
                return
            last_update = now
            try:
                percent = (downloaded / total_bytes * 100) if total_bytes > 0 else 0
                text = f"⬇️ دانلود: {title}\nفرمت: {fid}\n{downloaded/(1024*1024):.1f} / {total_bytes/(1024*1024):.1f} MB ({percent:.1f}%)\nسرعت: {format_speed(speed)}  ETA: {int(eta)}s"
                asyncio.run_coroutine_threadsafe(status_msg.edit_text(text), loop)
            except Exception:
                pass
        elif status == "finished":
            asyncio.run_coroutine_threadsafe(status_msg.edit_text("✅ دانلود کامل شد؛ در حال آماده‌سازی برای آپلود..."), loop)

    ydl_opts["progress_hooks"].append(progress_hook)

    try:
        await asyncio.to_thread(YoutubeDL(ydl_opts).download, [url])
    except Exception as e:
        await status_msg.edit_text(f"❌ خطا در دانلود: {e}")
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass
        return

    files = os.listdir(tmpdir)
    if not files:
        await status_msg.edit_text("❌ فایل دانلود شده پیدا نشد.")
        return
    file_path = os.path.join(tmpdir, files[0])

    last_uploaded_bytes = 0
    last_update_time = asyncio.get_event_loop().time()
    async def upload_progress(current, total):
        nonlocal last_uploaded_bytes, last_update_time
        now = asyncio.get_event_loop().time()
        if now - last_update_time < 5 and current != total:
            return
        
        try:
            percent = (current / total * 100) if total else 0
            speed = (current - last_uploaded_bytes) / (now - last_update_time) if (now - last_update_time) > 0 else 0
            text = f"⬆️ در حال آپلود: {file_to_upload_name}\n{current/(1024*1024):.1f}/{total/(1024*1024):.1f} MB ({percent:.1f}%)\nسرعت: {format_speed(speed)}"
            await status_msg.edit_text(text)
        except Exception:
            pass
        finally:
            last_uploaded_bytes = current
            last_update_time = now

    file_to_upload_name = custom_name if custom_name else files[0]
    await status_msg.edit_text(f"⬆️ شروع آپلود: {file_to_upload_name}")

    try:
        await client.send_document(
            chat_id=cq.message.chat.id,
            document=file_path,
            caption=file_to_upload_name,
            file_name=file_to_upload_name,
            progress=upload_progress
        )
        await status_msg.edit_text("✅ آپلود کامل شد. فایل ارسال شد.")
    except Exception as e:
        await status_msg.edit_text(f"❌ خطا در آپلود به تلگرام: {e}")
    finally:
        try:
            os.remove(file_path)
            os.rmdir(tmpdir)
        except Exception:
            pass
        await redis_client.delete(key)

# Function to run FastAPI server in a separate thread
def run_fastapi():
    port = int(os.getenv("PORT", 8000))
    config = Config(app=fastapi_app, host="0.0.0.0", port=port, loop="asyncio")
    server = Server(config)
    asyncio.run(server.serve())

@app.on_startup()
async def on_startup():
    global redis_client
    redis_client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), 
                               port=os.getenv("REDIS_PORT", 6379), 
                               db=int(os.getenv("REDIS_DB", 0)), 
                               decode_responses=True)
    await redis_client.ping()
    print("Connected to Redis successfully!")

@app.on_shutdown()
async def on_shutdown():
    await redis_client.close()

if __name__ == "__main__":
    print("Bot and health check server starting...")
    threading.Thread(target=run_fastapi, daemon=True).start()
    app.run()
