# bot.py
# Python 3.10+ recommended
# Requirements:
# pip install pyrogram tgcrypto yt-dlp aiofiles python-dotenv

import os
import uuid
import asyncio
import tempfile
from functools import partial
from yt_dlp import YoutubeDL
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()

API_ID = '3335796'
API_HASH = '138b992a0e672e8346d8439c3f42ea78'
BOT_TOKEN = '1806450812:AAGhHSWPd3sH5SVFBB8_Xadw_SbdbvZm0_Q'
#LOG_CHANNEL = -1001792962793  # مقدار دلخواه

app = Client("okru_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# in-memory store for extracted formats (for production use DB/cache)
EXTRACTS = {}  # key -> { "url":..., "title":..., "formats": {fmt_id: {...}} }

# helper to humanize format description
def fmt_label(f):
    # f is a format dict from yt-dlp
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
        # bytes to MB
        mb = filesize / (1024*1024)
        size_s = f"{mb:.1f} MB"
    else:
        size_s = "—"
    
    # عنوان دکمه بهبود یافته
    format_id = f.get("format_id") or ""
    return f"{resolution} — {ext} ({size_s}) - {format_id}"

# handler: /start
@app.on_message(filters.command("start"))
async def start(_, msg):
    await msg.reply_text("سلام! لینک ok.ru بفرست تا فرمت‌ها استخراج بشن و بتونی دانلود و آپلود کنی.\nمثال: https://ok.ru/video/....")

# handler: messages containing ok.ru (بسته به ورودی کاربر می‌تونید regex سخت‌تری بذارید)
@app.on_message(filters.private & filters.regex(r"(https?://)?(www\.)?ok\.ru/"))
async def extract_formats(client, msg):
    url = (msg.text or "").strip()
    processing = await msg.reply_text("⏳ در حال پردازش و استخراج فرمت‌ها...")
    key = str(uuid.uuid4())
    # yt-dlp extract
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "format": "best",
    }
    loop = asyncio.get_event_loop()
    def extract():
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    try:
        info = await loop.run_in_executor(None, extract)
    except Exception as e:
        await processing.edit_text(f"❌ خطا در استخراج فرمت: {e}")
        return

    title = info.get("title", "video")
    formats = info.get("formats", []) or [info]
    # build format map (pick distinct format_id entries and prefer video formats)
    fmts = {}
    for f in formats:
        fid = f.get("format_id") or f.get("format")
        if not fid:
            continue
        # avoid duplicate keys
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
    # store in memory
    EXTRACTS[key] = {"url": url, "title": title, "formats": fmts, "user_id": msg.from_user.id}

    # build keyboard (limit to first 20 formats to avoid too large keyboard)
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
        EXTRACTS.pop(key, None)
        return

    await processing.edit_text(
        f"🎬 عنوان: {title}\nانتخاب فرمت برای دانلود و آپلود:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# CallbackQuery handler for format selection
@app.on_callback_query(filters.regex(r"^DL\|"))
async def on_select_format(client, cq):
    data = cq.data  # "DL|{key}|{format_id}"
    await cq.answer("درخواست دریافت شد. آماده دانلود می‌شوم...", show_alert=False)
    
    # پیام عنوان و دکمه‌ها با پیام وضعیت دانلود جایگزین می‌شود
    try:
        _, key, fid = data.split("|", 2)
    except Exception:
        await cq.message.edit_text("داده نامعتبر.")
        return

    record = EXTRACTS.get(key)
    if not record:
        await cq.message.edit_text("کد منقضی شده یا اطلاعات پیدا نشد. دوباره لینک را بفرستید.")
        return

    url = record["url"]
    title = record["title"]
    # create temp dir + out template
    tmpdir = tempfile.mkdtemp(prefix="okru_dl_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

    # Use aria2c if installed for parallel download (increase speed) — optional
    ydl_opts = {
        "format": fid,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "no_warnings": True,
        "quiet": True,
        # try to speed up (if aria2c available)
        "external_downloader": "aria2c",
        "external_downloader_args": ["-x", "16", "-s", "16", "--file-allocation=none"],
        # ensure partial files are kept until completion
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        # progress hook
        "progress_hooks": [],
    }
    
    status_msg = cq.message # از همین پیام برای نمایش وضعیت دانلود استفاده می‌کنیم
    await status_msg.edit_text(f"⬇️ شروع دانلود: {title}\nفرمت: {fid}\nدر حال دانلود ...")

    loop = asyncio.get_event_loop()
    # progress hook to edit message periodically
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
            # limit updates to once per 5s
            if now - last_update < 5:
                return
            last_update = now
            try:
                percent = (downloaded / total_bytes * 100) if total_bytes else 0
                text = f"⬇️ دانلود: {title}\nفرمت: {fid}\n{downloaded/(1024*1024):.1f} / {total_bytes/(1024*1024):.1f} MB ({percent:.1f}%)\nسرعت: {speed/1024:.1f} KB/s  ETA: {int(eta)}s"
                asyncio.run_coroutine_threadsafe(status_msg.edit_text(text), loop)
            except Exception:
                pass
        elif status == "finished":
            asyncio.run_coroutine_threadsafe(status_msg.edit_text("✅ دانلود کامل شد؛ در حال آماده‌سازی برای آپلود..."), loop)

    ydl_opts["progress_hooks"].append(progress_hook)

    def download():
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.download([url])

    try:
        await loop.run_in_executor(None, download)
    except Exception as e:
        await status_msg.edit_text(f"❌ خطا در دانلود: {e}")
        # cleanup
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass
        return

    # find downloaded file
    files = os.listdir(tmpdir)
    if not files:
        await status_msg.edit_text("❌ فایل دانلود شده پیدا نشد.")
        return
    file_path = os.path.join(tmpdir, files[0])  # usually single file

    # upload with progress
    last_uploaded_bytes = 0
    last_update_time = asyncio.get_event_loop().time()
    async def upload_progress(current, total):
        nonlocal last_uploaded_bytes, last_update_time
        now = asyncio.get_event_loop().time()
        # limit updates to once per 5s
        if now - last_update_time < 5 and current != total:
            return
        
        try:
            percent = (current / total * 100) if total else 0
            # محاسبه سرعت آپلود
            speed = (current - last_uploaded_bytes) / (now - last_update_time) if (now - last_update_time) > 0 else 0
            text = f"⬆️ در حال آپلود: {files[0]}\n{current/(1024*1024):.1f}/{total/(1024*1024):.1f} MB ({percent:.1f}%)\nسرعت: {speed/1024:.1f} KB/s"
            
            await status_msg.edit_text(text)
        except Exception:
            pass
        finally:
            last_uploaded_bytes = current
            last_update_time = now

    try:
        # فایل همیشه به صورت داکیومنت آپلود می‌شود
        await client.send_document(
            chat_id=cq.message.chat.id,
            document=file_path,
            caption=files[0], # فقط نام فایل در کپشن نمایش داده می‌شود
            progress=upload_progress
        )
        await status_msg.edit_text("✅ آپلود کامل شد. فایل ارسال شد.")
    except Exception as e:
        await status_msg.edit_text(f"❌ خطا در آپلود به تلگرام: {e}")
    finally:
        # cleanup
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass
        # optionally remove mapping so callback cannot be reused
        EXTRACTS.pop(key, None)

if __name__ == "__main__":
    print("Bot running...")
    app.run()
