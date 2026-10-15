"""Instagram / TikTok havolasidan musiqa (mp3) ajratib beradigan Telegram bot.
GitHub'da bot.py nomi bilan saqlang.
requirements.txt: python-telegram-bot==21.6, yt-dlp, imageio-ffmpeg
O'zgaruvchilar: BOT_TOKEN, ADMIN_IDS (ixtiyoriy), HOURLY_LIMIT (standart 10),
                MAX_SEC (video davomiyligi chegarasi, standart 600), DB_PATH (ixtiyoriy)
"""
import os, re, glob, asyncio, tempfile, time, logging, sqlite3, shutil
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp
import imageio_ffmpeg

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).replace(" ", "").split(",") if x}
HOURLY_LIMIT = int(os.getenv("HOURLY_LIMIT", "10"))
MAX_SEC = int(os.getenv("MAX_SEC", "600"))
MAX_BYTES = 49 * 1024 * 1024
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()          # ffmpeg alohida o'rnatish shart emas
URL_RE = re.compile(r"https?://\S+")
SITES = ("instagram.com", "instagr.am", "tiktok.com")

db = sqlite3.connect(os.getenv("DB_PATH", "music.db"), check_same_thread=False)
db.execute("create table if not exists downloads(uid int, at int, ok int, site text)")
db.commit()

recent = {}
sem = asyncio.Semaphore(2)


class TooLong(Exception):
    pass


def site_of(url):
    host = (urlparse(url).hostname or "").lower()
    for d in SITES:
        if host == d or host.endswith("." + d):
            return d
    return None


def limited(uid):
    now = time.time()
    r = [t for t in recent.get(uid, []) if now - t < 3600]
    recent[uid] = r
    if len(r) >= HOURLY_LIMIT:
        return True
    r.append(now)
    return False


def fetch(url, outdir):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{outdir}/%(id)s.%(ext)s",
        "noplaylist": True, "playlist_items": "1",
        "quiet": True, "no_warnings": True,
        "socket_timeout": 30, "retries": 2,
        "ffmpeg_location": FFMPEG,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("entries"):
            info = next(iter(info["entries"]))
        if (info.get("duration") or 0) > MAX_SEC:
            raise TooLong()
        ydl.process_ie_result(info, download=True)
    files = glob.glob(f"{outdir}/*.mp3")
    meta = {"title": (info.get("track") or info.get("title") or "Audio")[:64],
            "artist": (info.get("artist") or info.get("uploader") or "")[:64],
            "duration": int(info["duration"]) if info.get("duration") else None}
    return (files[0] if files else None), meta


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Instagram yoki TikTok video havolasini yuboring, men undan musiqani (mp3) ajratib beraman.\n\n"
        "Faqat ommaviy videolar ishlaydi. Musiqa mualliflik huquqi bilan himoyalangan bo'lishi mumkin: "
        "uni faqat shaxsiy tinglash uchun ishlating va ruxsatsiz tarqatmang.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    found = URL_RE.search(m.text or "")
    if not found:
        await m.reply_text("Instagram yoki TikTok video havolasini yuboring (https://...).")
        return
    url = found.group(0)
    site = site_of(url)
    if not site:
        await m.reply_text("Hozircha faqat Instagram va TikTok havolalari qo'llab-quvvatlanadi.")
        return
    uid = update.effective_user.id
    if limited(uid):
        await m.reply_text(f"Soatiga {HOURLY_LIMIT} tagacha so'rov mumkin. Keyinroq urinib ko'ring.")
        return

    status = await m.reply_text("Video topilmoqda, musiqa ajratilmoqda...")
    tmp = tempfile.mkdtemp()
    ok = 0
    try:
        async with sem:
            path, meta = await asyncio.to_thread(fetch, url, tmp)
        if not path:
            await status.edit_text("Musiqani ajratib bo'lmadi.")
        elif os.path.getsize(path) > MAX_BYTES:
            await status.edit_text("Fayl Telegram orqali yuborish uchun juda katta.")
        else:
            with open(path, "rb") as f:
                await m.reply_audio(f, title=meta["title"], performer=meta["artist"] or None,
                                    duration=meta["duration"], read_timeout=180, write_timeout=180,
                                    connect_timeout=30, pool_timeout=30)
            ok = 1
            await status.delete()
    except TooLong:
        await status.edit_text(f"Video juda uzun (chegara {MAX_SEC // 60} daqiqa).")
    except yt_dlp.utils.DownloadError:
        logging.exception("Yuklab bo'lmadi: %s", url)
        await status.edit_text("Videoni olib bo'lmadi. U yopiq yoki o'chirilgan bo'lishi mumkin, yoki platforma "
                               "so'rovni cheklagan (Instagram bunday holatlar ko'p). Keyinroq yoki boshqa havola bilan urinib ko'ring.")
    except Exception:
        logging.exception("Xato")
        await status.edit_text("Xatolik yuz berdi. Keyinroq qayta urinib ko'ring.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        db.execute("insert into downloads values(?,?,?,?)", (uid, int(time.time()), ok, site))
        db.commit()


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    day = int(time.time()) - 86400
    one = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    tt = one("select count(*) from downloads where site='tiktok.com'")
    ig = one("select count(*) from downloads where site like 'insta%'")
    await update.message.reply_text(
        f"Jami so'rovlar: {one('select count(*) from downloads')}\n"
        f"Foydalanuvchilar: {one('select count(distinct uid) from downloads')}\n"
        f"Oxirgi 24 soatda: {one('select count(*) from downloads where at>?', day)} "
        f"(muvaffaqiyatli: {one('select count(*) from downloads where at>? and ok=1', day)})\n"
        f"TikTok: {tt} · Instagram: {ig}")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
