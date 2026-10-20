"""Hammasi bir botda:
  - Instagram/TikTok havolasidan musiqa (mp3) yoki video yuklash
  - Mavzular bo'yicha qo'llanma va shablonlar do'koni (chek orqali to'lov)
  - Premium obuna (kunlik bepul limit, undan keyin pullik)
  - Admin panel: statistika, e'lon yuborish, bloklash, majburiy kanal, mahsulot va tariflar

GitHub'da allbot.py nomi bilan saqlang. requirements.txt: python-telegram-bot==21.6, yt-dlp, imageio-ffmpeg
O'zgaruvchilar (Railway Variables):
  BOT_TOKEN, ADMIN_IDS (birinchisi - egasi), CARD, HOLDER, DB_PATH (ixtiyoriy),
  FREE_DAILY (kunlik bepul yuklash, standart 3), HOURLY_LIMIT (soatiga, standart 20), MAX_SEC (standart 600)
"""
import os, re, glob, asyncio, tempfile, time, logging, sqlite3, shutil, secrets
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M, ReplyKeyboardMarkup as RK
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (Application, ApplicationHandlerStop, CommandHandler, CallbackQueryHandler,
                          MessageHandler, TypeHandler, filters, ContextTypes)
import yt_dlp
import imageio_ffmpeg

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)   # loglarda token ko'rinmasligi uchun

TOKEN = os.environ["BOT_TOKEN"]
ENV_ADMINS = [int(x) for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).replace(" ", "").split(",") if x]
assert ENV_ADMINS, "ADMIN_IDS yoki ADMIN_ID kiriting"
OWNER = ENV_ADMINS[0]
CARD = os.getenv("CARD", "8600 0000 0000 0000")
HOLDER = os.getenv("HOLDER", "ISM FAMILIYA")
FREE_DAILY = int(os.getenv("FREE_DAILY", "3"))
HOURLY_LIMIT = int(os.getenv("HOURLY_LIMIT", "20"))
MAX_SEC = int(os.getenv("MAX_SEC", "600"))
MAX_BYTES = 49 * 1024 * 1024
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
URL_RE = re.compile(r"https?://\S+")
SITES = ("instagram.com", "instagr.am", "tiktok.com")
TZ = 5 * 3600   # O'zbekiston vaqti (UTC+5)

db = sqlite3.connect(os.getenv("DB_PATH", "bot.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists users(uid int primary key, name text, username text, joined int, blocked int default 0);
create table if not exists banned(uid int primary key);
create table if not exists settings(k text primary key, v text);
create table if not exists premium(uid int primary key, until int);
create table if not exists admins(uid int primary key);
create table if not exists categories(id integer primary key autoincrement, name text);
create table if not exists products(id integer primary key autoincrement, name text, descr text,
  price int, kind text, content text, active int default 1, cat_id int default 0);
create table if not exists items(id integer primary key autoincrement, prod_id int, kind text, content text, pos int);
create table if not exists sales(id text primary key, uid int, uname text, prod_id int, pm text, status text, at int);
create table if not exists downloads(uid int, at int, ok int, site text);
""")
for _sql in ("alter table products add column cat_id int default 0",
             "alter table downloads add column mode text"):
    try:
        db.execute(_sql)
    except sqlite3.OperationalError:
        pass
if not db.execute("select count(*) from categories").fetchone()[0]:
    for _n in ("Huquq", "Biznes va soliq", "Ta'lim", "Karyera", "Moliya va byudjet", "Kontent va marketing"):
        db.execute("insert into categories(name) values(?)", (_n,))
db.commit()

recent = {}
sem = asyncio.Semaphore(2)


class TooLong(Exception):
    pass


# ------------------------------------------------------------------ yordamchilar
def money(n): return f"{int(n):,}".replace(",", " ") + " so'm"
def admins(): return set(ENV_ADMINS) | {r["uid"] for r in db.execute("select uid from admins")}
def fmt_date(ts): return time.strftime("%d.%m.%Y", time.gmtime(ts + TZ))
def day_start():
    now = int(time.time())
    return (now + TZ) // 86400 * 86400 - TZ


def get_setting(k, default=""):
    r = db.execute("select v from settings where k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_setting(k, v):
    db.execute("insert or replace into settings values(?,?)", (k, v))
    db.commit()


def premium_until(uid):
    r = db.execute("select until from premium where uid=?", (uid,)).fetchone()
    return r["until"] if r else 0


def is_premium(uid): return uid in admins() or premium_until(uid) > time.time()


def used_today(uid):
    return db.execute("select count(*) from downloads where uid=? and ok=1 and at>=?",
                      (uid, day_start())).fetchone()[0]


def site_of(url):
    host = (urlparse(url).hostname or "").lower()
    for d in SITES:
        if host == d or host.endswith("." + d):
            return d
    return None


def hourly_limited(uid):
    now = time.time()
    r = [t for t in recent.get(uid, []) if now - t < 3600]
    recent[uid] = r
    if len(r) >= HOURLY_LIMIT:
        return True
    r.append(now)
    return False


def cat_name(cid):
    if cid == -1:
        return "Obuna"
    if not cid:
        return "Boshqa"
    r = db.execute("select name from categories where id=?", (cid,)).fetchone()
    return r["name"] if r else "Boshqa"


def main_kb(uid):
    rows = [["Yuklash", "Mavzular"], ["Kutubxona", "Obuna"], ["Aloqa"]]
    if uid in admins():
        rows.append(["Admin panel"])
    return RK(rows, resize_keyboard=True)


def admin_kb():
    return RK([["Mahsulot qo'shish", "To'plam qo'shish"],
               ["Hamma mahsulotlar", "Mavzu qo'shish"],
               ["Mavzularni boshqarish", "Obuna tariflari"],
               ["Premium berish", "Bloklash"],
               ["Blokdan chiqarish", "Majburiy kanal"],
               ["Buyurtmalar", "Statistika"],
               ["E'lon yuborish", "Adminlar"],
               ["Orqaga"]], resize_keyboard=True)


# ------------------------------------------------------------------ himoya va majburiy obuna
async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u:
        return
    db.execute("insert or ignore into users(uid,name,username,joined) values(?,?,?,?)",
               (u.id, u.full_name, u.username or "", int(time.time())))
    db.commit()
    if u.id not in admins() and db.execute("select 1 from banned where uid=?", (u.id,)).fetchone():
        if update.callback_query:
            await update.callback_query.answer("Siz bloklangansiz.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Siz bloklangansiz.")
        raise ApplicationHandlerStop


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Majburiy kanalga obuna tekshiruvi. True - davom etish mumkin."""
    uid = update.effective_user.id
    ch = get_setting("channel")
    if not ch or uid in admins():
        return True
    try:
        mem = await ctx.bot.get_chat_member(ch, uid)
        if mem.status in ("member", "administrator", "creator"):
            return True
    except Exception:
        logging.warning("Kanal tekshiruvi ishlamadi (%s). Bot kanalda admin ekanini tekshiring.", ch)
        return True
    await ctx.bot.send_message(
        update.effective_chat.id, "Botdan foydalanish uchun avval kanalimizga obuna bo'ling:",
        reply_markup=M([[B("Kanalga obuna bo'lish", url=f"https://t.me/{ch.lstrip('@')}")],
                        [B("Tekshirish", callback_data="chk")]]))
    return False


# ------------------------------------------------------------------ yuklash (musiqa / video)
def fetch(url, outdir, mode):
    opts = {"outtmpl": f"{outdir}/%(id)s.%(ext)s", "noplaylist": True, "playlist_items": "1",
            "quiet": True, "no_warnings": True, "socket_timeout": 30, "retries": 2, "ffmpeg_location": FFMPEG}
    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        opts["format"] = "bv*[height<=720]+ba/b[height<=720]/b"
        opts["merge_output_format"] = "mp4"
        opts["max_filesize"] = MAX_BYTES
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("entries"):
            info = next(iter(info["entries"]))
        if (info.get("duration") or 0) > MAX_SEC:
            raise TooLong()
        ydl.process_ie_result(info, download=True)
    if mode == "audio":
        files = glob.glob(f"{outdir}/*.mp3")
    else:
        files = [f for f in glob.glob(f"{outdir}/*") if f.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))]
    meta = {"title": (info.get("track") or info.get("title") or "Media")[:64],
            "artist": (info.get("artist") or info.get("uploader") or "")[:64],
            "duration": int(info["duration"]) if info.get("duration") else None}
    return (files[0] if files else None), meta


async def open_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    if is_premium(uid):
        st = "Premium faol: cheklovsiz foydalanasiz."
    else:
        st = f"Bugun qolgan bepul yuklash: {max(0, FREE_DAILY - used_today(uid))} / {FREE_DAILY}"
    await update.message.reply_text(
        f"Instagram yoki TikTok video havolasini shu yerga yuboring, musiqa (mp3) yoki videoni yuklab beraman.\n\n{st}\n\n"
        "Faqat ommaviy videolar ishlaydi. Kontent mualliflik huquqi bilan himoyalangan bo'lishi mumkin: "
        "uni faqat shaxsiy foydalanish uchun ishlating.")


async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url):
    m = update.message
    if not site_of(url):
        await m.reply_text("Hozircha faqat Instagram va TikTok havolalari qo'llab-quvvatlanadi.")
        return
    if not await gate(update, ctx):
        return
    links = ctx.application.bot_data.setdefault("links", {})
    now = time.time()
    for k in [k for k, (_, t) in links.items() if now - t > 3600]:
        del links[k]
    tok = secrets.token_urlsafe(6)
    links[tok] = (url, now)
    await m.reply_text("Nimani yuklab beray?",
                       reply_markup=M([[B("Musiqa (mp3)", callback_data=f"a:{tok}"),
                                        B("Video", callback_data=f"v:{tok}")]]))


async def run_download(q, ctx, kind, tok):
    item = ctx.application.bot_data.get("links", {}).get(tok)
    if not item:
        await q.answer("Havola eskirgan, uni qayta yuboring.", show_alert=True)
        return
    await q.answer()
    url = item[0]
    mode = "audio" if kind == "a" else "video"
    uid = q.from_user.id
    site = site_of(url)
    if not is_premium(uid) and used_today(uid) >= FREE_DAILY:
        await q.message.reply_text(
            f"Bugungi bepul limit ({FREE_DAILY} ta) tugadi. Cheklovsiz foydalanish uchun \"Obuna\" bo'limidan "
            "Premium oling yoki ertaga qayta urinib ko'ring.")
        return
    if hourly_limited(uid):
        await q.message.reply_text("Juda tez-tez so'rov yuboryapsiz. Birozdan keyin urinib ko'ring.")
        return

    status = await q.edit_message_text("Yuklanmoqda, biroz kuting...")
    tmp = tempfile.mkdtemp()
    ok = 0
    try:
        async with sem:
            path, meta = await asyncio.to_thread(fetch, url, tmp, mode)
        if not path:
            await status.edit_text("Yuklab bo'lmadi yoki fayl juda katta (chegara 49 MB).")
        elif os.path.getsize(path) > MAX_BYTES:
            await status.edit_text("Fayl Telegram orqali yuborish uchun juda katta (chegara 49 MB).")
        else:
            with open(path, "rb") as f:
                kw = dict(read_timeout=180, write_timeout=180, connect_timeout=30, pool_timeout=30)
                if mode == "audio":
                    await q.message.reply_audio(f, title=meta["title"], performer=meta["artist"] or None,
                                                duration=meta["duration"], **kw)
                else:
                    await q.message.reply_video(f, caption=meta["title"], duration=meta["duration"],
                                                supports_streaming=True, **kw)
            ok = 1
            await status.delete()
    except TooLong:
        await status.edit_text(f"Video juda uzun (chegara {MAX_SEC // 60} daqiqa).")
    except yt_dlp.utils.DownloadError:
        logging.exception("Yuklab bo'lmadi")
        await status.edit_text("Videoni olib bo'lmadi. U yopiq yoki o'chirilgan bo'lishi mumkin, yoki platforma "
                               "so'rovni cheklagan (Instagramda bu ko'p). Keyinroq yoki boshqa havola bilan urinib ko'ring.")
    except Exception:
        logging.exception("Xato")
        await status.edit_text("Xatolik yuz berdi. Keyinroq qayta urinib ko'ring.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        db.execute("insert into downloads(uid,at,ok,site,mode) values(?,?,?,?,?)",
                   (uid, int(time.time()), ok, site, mode))
        db.commit()


# ------------------------------------------------------------------ do'kon (mavzular) va obuna
def cat_buttons(bundle):
    cond = "kind='bundle'" if bundle else "kind not in ('bundle','premium')"
    pre = "bcat" if bundle else "cat"
    btns = []
    for c in db.execute("select * from categories order by id").fetchall():
        n = db.execute(f"select count(*) from products where active=1 and cat_id=? and {cond}", (c["id"],)).fetchone()[0]
        if n:
            btns.append(B(f"{c['name']} ({n})", callback_data=f"{pre}:{c['id']}"))
    n0 = db.execute(f"select count(*) from products where active=1 and cat_id=0 and {cond}").fetchone()[0]
    if n0:
        btns.append(B(f"Boshqa ({n0})", callback_data=f"{pre}:0"))
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


async def show_cats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    kb = cat_buttons(False)
    if not kb:
        await update.message.reply_text("Hozircha mahsulot yo'q.")
        return
    await update.message.reply_text("Mavzuni tanlang:", reply_markup=M(kb))


async def show_lib(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    kb = cat_buttons(True)
    if not kb:
        await update.message.reply_text("Kutubxona hozircha bo'sh.")
        return
    await update.message.reply_text(
        "Kutubxona: bitta tugma orqali bir nechta material (kitoblar, darsliklar, rasmlar, fayllar) olasiz.\n"
        "Mavzuni tanlang:", reply_markup=M(kb))


def item_count(pid):
    return db.execute("select count(*) from items where prod_id=?", (pid,)).fetchone()[0]


async def send_products(msg, cat_id, bundle=False):
    cond = "kind='bundle'" if bundle else "kind not in ('bundle','premium')"
    rows = db.execute(f"select * from products where active=1 and cat_id=? and {cond} order by id desc limit 20",
                      (cat_id,)).fetchall()
    if not rows:
        await msg.reply_text("Bu bo'limda hozircha mahsulot yo'q.")
        return
    for p in rows:
        price = "Bepul" if p["price"] == 0 else money(p["price"])
        extra = f"\nIchida: {item_count(p['id'])} ta material" if p["kind"] == "bundle" else ""
        btn = (B("Bepul olish", callback_data=f"free:{p['id']}") if p["price"] == 0
               else B("Sotib olish", callback_data=f"buy:{p['id']}"))
        await msg.reply_text(f"{p['name']}\n\n{p['descr']}{extra}\n\nNarx: {price}", reply_markup=M([[btn]]))


async def sub_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    until = premium_until(uid)
    if uid in admins():
        head = "Siz adminsiz: barcha imkoniyatlar cheklovsiz."
    elif until > time.time():
        head = f"Premium faol, {fmt_date(until)} gacha."
    else:
        head = (f"Bepul tarifda kuniga {FREE_DAILY} ta yuklash mumkin. "
                "Premium bilan cheklovsiz yuklaysiz.")
    await update.message.reply_text(head)
    plans = db.execute("select * from products where kind='premium' and active=1 order by price").fetchall()
    if not plans:
        await update.message.reply_text("Premium tariflari hali belgilanmagan.")
        return
    for p in plans:
        await update.message.reply_text(f"{p['name']}\nNarx: {money(p['price'])}",
                                        reply_markup=M([[B("Sotib olish", callback_data=f"buy:{p['id']}")]]))


async def send_item(bot, uid, kind, content):
    if kind == "doc":
        await bot.send_document(uid, content)
    elif kind == "photo":
        await bot.send_photo(uid, content)
    elif kind == "video":
        await bot.send_video(uid, content)
    elif kind == "audio":
        await bot.send_audio(uid, content)
    else:
        await bot.send_message(uid, content)


async def deliver(bot, uid, p):
    if p["kind"] == "bundle":
        rows = db.execute("select * from items where prod_id=? order by pos, id", (p["id"],)).fetchall()
        await bot.send_message(uid, f"{p['name']}: {len(rows)} ta material yuborilmoqda...")
        failed = 0
        for r in rows:
            try:
                await send_item(bot, uid, r["kind"], r["content"])
            except Exception:
                logging.exception("To'plam elementini yuborib bo'lmadi")
                failed += 1
            await asyncio.sleep(0.4)
        await bot.send_message(uid, "Hammasi yuborildi. Rahmat!" if not failed else
                               f"{len(rows) - failed} ta material yuborildi, {failed} tasini yuborib bo'lmadi. Admin bilan bog'laning.")
        return
    if p["kind"] == "premium":
        days = int(p["content"])
        until = max(int(time.time()), premium_until(uid)) + days * 86400
        db.execute("insert or replace into premium values(?,?)", (uid, until))
        db.commit()
        await bot.send_message(uid, f"Premium faollashdi: {days} kun. Amal qilish muddati: {fmt_date(until)}. "
                                    "Endi cheklovsiz foydalanishingiz mumkin.")
        return
    cap = f"{p['name']}\n\nXaridingiz uchun rahmat!"
    if p["kind"] == "doc":
        await bot.send_document(uid, p["content"], caption=cap)
    elif p["kind"] == "photo":
        await bot.send_photo(uid, p["content"], caption=cap)
    else:
        await bot.send_message(uid, f"{cap}\n\n{p['content']}")


def prod_card(p):
    extra = f" · {item_count(p['id'])} ta material" if p["kind"] == "bundle" else ""
    return (f"#{p['id']} [{cat_name(p['cat_id'] if p['kind'] != 'premium' else -1)}] {p['name']} · "
            f"{'Bepul' if p['price'] == 0 else money(p['price'])}{extra} · {'sotuvda' if p['active'] else 'yashirin'}")


def prod_kb(p):
    return M([[B("Yashirish" if p["active"] else "Ko'rsatish", callback_data=f"tg:{p['id']}"),
               B("O'chirish", callback_data=f"rm:{p['id']}")]])


# ------------------------------------------------------------------ tugmalar (callback)
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    kind, _, val = q.data.partition(":")
    ud = ctx.user_data
    uid = q.from_user.id
    is_adm = uid in admins()

    if kind in ("a", "v"):
        await run_download(q, ctx, kind, val)
        return
    await q.answer()

    if kind == "chk":
        if await gate(update, ctx):
            await q.edit_message_text("Rahmat! Endi botdan foydalanishingiz mumkin.")

    elif kind == "cat":
        if await gate(update, ctx):
            await send_products(q.message, int(val))

    elif kind == "bcat":
        if await gate(update, ctx):
            await send_products(q.message, int(val), bundle=True)

    elif kind == "free":
        p = db.execute("select * from products where id=? and active=1 and price=0", (val,)).fetchone()
        if not p:
            await q.message.reply_text("Bu material mavjud emas.")
            return
        if await gate(update, ctx):
            db.execute("insert into sales values(?,?,?,?,?,?,?)",
                       ("F" + secrets.token_hex(3).upper(), uid, q.from_user.username or q.from_user.full_name,
                        p["id"], "bepul", "bepul", int(time.time())))
            db.commit()
            await deliver(ctx.bot, uid, p)

    elif kind == "buy":
        p = db.execute("select * from products where id=? and active=1", (val,)).fetchone()
        if not p:
            await q.message.reply_text("Bu mahsulot mavjud emas.")
            return
        await q.message.reply_text(
            f"{p['name']} — {money(p['price'])}\nTo'lov usulini tanlang:",
            reply_markup=M([[B(x, callback_data=f"pm:{p['id']}:{x}") for x in ("Click", "Payme", "Karta")]]))

    elif kind == "pm":
        pid, _, method = val.partition(":")
        p = db.execute("select * from products where id=? and active=1", (pid,)).fetchone()
        if not p:
            await q.message.reply_text("Bu mahsulot mavjud emas.")
            return
        sid = "M" + secrets.token_hex(3).upper()
        db.execute("insert into sales values(?,?,?,?,?,?,?)",
                   (sid, uid, q.from_user.username or q.from_user.full_name, p["id"], method,
                    "chek kutilmoqda", int(time.time())))
        db.commit()
        ud.clear()
        ud.update(state="receipt", sid=sid)
        await q.message.reply_text(
            f"Buyurtma: {sid}\nMahsulot: {p['name']}\nSumma: {money(p['price'])}\nTo'lov usuli: {method}\n"
            f"Karta: {CARD}\nQabul qiluvchi: {HOLDER}\n\n"
            f"To'lov izohiga {sid} kodini yozing. To'lagach, chek rasmini shu yerga yuboring.")

    elif kind == "pc" and is_adm and ud.get("state") == "m_cat":
        ud["new"]["cat"] = int(val)
        ud["state"] = "m_c"
        await q.message.reply_text(
            f"Mavzu: {cat_name(int(val))}\nEndi mahsulotning o'zini yuboring: fayl (PDF, ZIP va h.k.) yoki rasm, "
            "yoki matn/havola.")

    elif kind == "pc" and is_adm and ud.get("state") == "b_cat":
        ud["new"]["cat"] = int(val)
        ud["state"] = "b_f"
        await q.message.reply_text(
            f"Mavzu: {cat_name(int(val))}\nEndi to'plamdagi materiallarni birin-ketin yuboring: fayllar (PDF, ZIP, APK...), "
            "rasmlar, video, audio yoki havola/matn. Hammasini yuborib bo'lgach, \"Tayyor\" deb yozing.")

    elif kind == "dc" and is_adm:
        db.execute("update products set cat_id=0 where cat_id=?", (val,))
        db.execute("delete from categories where id=?", (val,))
        db.commit()
        await q.edit_message_text("Mavzu o'chirildi (mahsulotlari \"Boshqa\" ga o'tdi).")

    elif kind in ("tg", "rm") and is_adm:
        if kind == "tg":
            db.execute("update products set active=1-active where id=?", (val,))
            db.commit()
            p = db.execute("select * from products where id=?", (val,)).fetchone()
            if p:
                await q.edit_message_text(prod_card(p), reply_markup=prod_kb(p))
        else:
            db.execute("delete from products where id=?", (val,))
            db.execute("delete from items where prod_id=?", (val,))
            db.commit()
            await q.edit_message_text(f"#{val} o'chirildi.")

    elif kind in ("ok", "no") and is_adm:
        o = db.execute("select * from sales where id=?", (val,)).fetchone()
        if not o:
            return
        if o["status"] in ("tasdiqlandi", "rad etildi"):
            await q.edit_message_caption(f"{q.message.caption}\n\n→ allaqachon ko'rib chiqilgan: {o['status'].upper()}")
            return
        status = "tasdiqlandi" if kind == "ok" else "rad etildi"
        db.execute("update sales set status=? where id=?", (status, val))
        db.commit()
        note = ""
        if kind == "ok":
            p = db.execute("select * from products where id=?", (o["prod_id"],)).fetchone()
            try:
                await ctx.bot.send_message(o["uid"], f"Buyurtma {val}: to'lov tasdiqlandi!")
                await deliver(ctx.bot, o["uid"], p)
            except Exception:
                logging.exception("Yetkazib bo'lmadi")
                note = "\n(Xaridorga yuborib bo'lmadi — qo'lda yuboring)"
        else:
            await ctx.bot.send_message(o["uid"], f"Buyurtma {val}: to'lov tasdiqlanmadi. Savol bo'lsa, Aloqa tugmasini bosing.")
        await q.edit_message_caption(f"{q.message.caption}\n\n→ {status.upper()}{note}")

    elif kind == "bcgo" and is_adm and ud.get("bc"):
        src_chat, src_msg = ud.pop("bc")
        ud.clear()
        await q.edit_message_text("E'lon yuborilmoqda... Tugagach natijani yozaman.")
        ctx.application.create_task(bc_send(ctx.bot, uid, src_chat, src_msg))

    elif kind == "bcno" and is_adm:
        ud.clear()
        await q.edit_message_text("E'lon bekor qilindi.")


# ------------------------------------------------------------------ admin bo'limlari
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in admins():
        return
    await update.message.reply_text("Admin panel:", reply_markup=admin_kb())


async def back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Asosiy menyu.", reply_markup=main_kb(update.effective_user.id))


def admin_only(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id in admins():
            await fn(update, ctx)
    return w


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    one = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    ds = day_start()
    now = int(time.time())
    users = one("select count(*) from users")
    new_users = one("select count(*) from users where joined>=?", ds)
    banned = one("select count(*) from banned")
    prem = one("select count(*) from premium where until>?", now)
    dl_all = one("select count(*) from downloads")
    dl_today = one("select count(*) from downloads where at>=?", ds)
    tt = one("select count(*) from downloads where site='tiktok.com'")
    ig = one("select count(*) from downloads where site like 'insta%'")
    sales = one("select count(*) from sales where status='tasdiqlandi'")
    pending = one("select count(*) from sales where status='chek yuborildi'")
    revenue = one("select coalesce(sum(p.price),0) from sales s join products p on p.id=s.prod_id "
                  "where s.status='tasdiqlandi'")
    await update.message.reply_text(
        f"Foydalanuvchilar: {users} (bugun yangi: {new_users})\n"
        f"Bloklanganlar: {banned}\n"
        f"Premium faol: {prem}\n"
        f"Yuklashlar: jami {dl_all}, bugun {dl_today}\n"
        f"  TikTok: {tt} · Instagram: {ig}\n"
        f"Sotuvlar: {sales} ta, tushum: {money(revenue)}\n"
        f"Chek kutayotgan: {pending}")


async def ask_state(update, ctx, state, text, **extra):
    ctx.user_data.clear()
    ctx.user_data.update(state=state, **extra)
    await update.message.reply_text(text)


async def bc_start(update, ctx):
    await ask_state(update, ctx, "bc", "E'lon xabarini yuboring (matn, rasm, video yoki fayl bo'lishi mumkin). "
                                       "Bekor qilish uchun menyudan boshqa tugmani bosing.")


async def ban_start(update, ctx):
    await ask_state(update, ctx, "ban", "Bloklanadigan foydalanuvchi ID raqamini yozing:")


async def unban_start(update, ctx):
    await ask_state(update, ctx, "unban", "Blokdan chiqariladigan foydalanuvchi ID raqamini yozing:")


async def pgive_start(update, ctx):
    await ask_state(update, ctx, "pgive", "Premium berish: ID va kunlar sonini yozing. Masalan: 123456789 30")


async def channel_start(update, ctx):
    cur = get_setting("channel") or "o'rnatilmagan"
    await ask_state(update, ctx, "setch",
                    f"Joriy majburiy kanal: {cur}\n\nYangi kanalni @kanal_nomi ko'rinishida yozing, "
                    "o'chirish uchun - yozing.\nMuhim: bot shu kanalda admin bo'lishi kerak.")


async def wiz_start(update, ctx):
    await ask_state(update, ctx, "m_n", "Yangi mahsulot. Nomini yozing:", new={})


async def bundle_start(update, ctx):
    await ask_state(update, ctx, "b_n", "Yangi to'plam (kitoblar, darsliklar, rasmlar, ilova fayllari...). Nomini yozing:",
                    new={"items": []})


async def cat_add_start(update, ctx):
    await ask_state(update, ctx, "c_n", "Yangi mavzu nomini yozing:")


async def cat_manage(update, ctx):
    for c in db.execute("select * from categories order by id").fetchall():
        await update.message.reply_text(c["name"], reply_markup=M([[B("O'chirish", callback_data=f"dc:{c['id']}")]]))


async def all_products(update, ctx):
    rows = db.execute("select * from products order by id desc limit 30").fetchall()
    if not rows:
        await update.message.reply_text("Mahsulot yo'q. \"Mahsulot qo'shish\" ni bosing.")
        return
    for p in rows:
        await update.message.reply_text(prod_card(p), reply_markup=prod_kb(p))


async def plans_manage(update, ctx):
    for p in db.execute("select * from products where kind='premium' order by price").fetchall():
        await update.message.reply_text(prod_card(p), reply_markup=prod_kb(p))
    await ask_state(update, ctx, "plan_d", "Yangi Premium tarif qo'shish: necha kunlik? (raqam, masalan 30)")


async def orders(update, ctx):
    rows = db.execute("select s.*, p.name from sales s left join products p on p.id=s.prod_id "
                      "order by at desc limit 15").fetchall()
    await update.message.reply_text(
        "\n".join(f"{o['id']} · @{o['uname']} · {o['name']} · {o['status']}" for o in rows) or "Buyurtma yo'q.")


async def list_admins(update, ctx):
    if update.effective_user.id != OWNER:
        return
    await update.message.reply_text("Adminlar:\n" + "\n".join(
        f"{a}{' (egasi)' if a == OWNER else ''}" for a in sorted(admins())) +
        "\n\nQo'shish: /addadmin ID\nOlib tashlash: /deladmin ID")


async def ask_support(update, ctx):
    ctx.user_data["state"] = "support"
    await update.message.reply_text("Savolingizni yozing, adminga yuboraman.")


async def add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Format: /addadmin 123456789\n(U avval botga /start yozgan bo'lishi kerak.)")
        return
    db.execute("insert or ignore into admins values(?)", (int(ctx.args[0]),))
    db.commit()
    await update.message.reply_text(f"Admin qo'shildi: {ctx.args[0]}")


async def del_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Format: /deladmin 123456789")
        return
    db.execute("delete from admins where uid=?", (int(ctx.args[0]),))
    db.commit()
    await update.message.reply_text("Admin olib tashlandi (agar ro'yxatda bo'lsa).")


async def bc_send(bot, admin_id, src_chat, src_msg):
    ok = fail = 0
    for r in db.execute("select uid from users where blocked=0 and uid not in (select uid from banned)").fetchall():
        uid = r["uid"]
        for attempt in range(2):
            try:
                await bot.copy_message(uid, src_chat, src_msg)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                db.execute("update users set blocked=1 where uid=?", (uid,))
                fail += 1
                break
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.06)
    db.commit()
    await bot.send_message(admin_id, f"E'lon yuborildi: {ok} ta muvaffaqiyatli, {fail} ta xato.")


async def bc_prepare(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    n = db.execute("select count(*) from users where blocked=0").fetchone()[0]
    ctx.user_data["bc"] = (m.chat_id, m.message_id)
    ctx.user_data["state"] = "bc_ok"
    await m.reply_text(f"Shu xabar {n} ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
                       reply_markup=M([[B("Yuborish", callback_data="bcgo"), B("Bekor qilish", callback_data="bcno")]]))


def save_product(ud, kind, content, name=None):
    n = ud["new"]
    db.execute("insert into products(name,descr,price,kind,content,cat_id) values(?,?,?,?,?,?)",
               (name or n["name"], n.get("descr", ""), n["price"], kind, content, n.get("cat", 0)))
    db.commit()


# ------------------------------------------------------------------ xabarlar
MENU = {}   # pastda to'ldiriladi


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if not m:
        return
    ud = ctx.user_data
    uid = update.effective_user.id
    t = (m.text or "").strip()
    is_adm = uid in admins()

    if t in MENU:
        ud.clear()
        await MENU[t](update, ctx)
        return
    st = ud.get("state")

    if st == "bc" and is_adm:
        await bc_prepare(update, ctx)
        return
    if m.photo or m.document or m.video or m.audio:
        await on_file(update, ctx)
        return
    if not m.text:
        return

    if st == "support" and not is_adm:
        u = update.effective_user
        for adm in admins():
            try:
                await ctx.bot.send_message(adm, f"Savol: {u.full_name} (@{u.username or '-'}, id {u.id}):\n{t}")
            except Exception:
                logging.exception("Adminga yuborib bo'lmadi")
        ud.clear()
        await m.reply_text("Yuborildi. Admin tez orada javob beradi.")
        return

    if is_adm and st:
        if await admin_state(update, ctx, st, t):
            return

    found = URL_RE.search(t)
    if found and not st:
        await handle_link(update, ctx, found.group(0))
    elif not st:
        await m.reply_text("Instagram yoki TikTok havolasini yuboring yoki menyudan foydalaning.")


async def admin_state(update, ctx, st, t):
    """Admin holatlari. True - xabar ishlandi."""
    m, ud = update.message, ctx.user_data
    if st in ("ban", "unban"):
        if not t.lstrip("-").isdigit():
            await m.reply_text("Faqat ID raqamini yozing:")
            return True
        if st == "ban":
            db.execute("insert or ignore into banned values(?)", (int(t),))
            msg = f"{t} bloklandi."
        else:
            db.execute("delete from banned where uid=?", (int(t),))
            msg = f"{t} blokdan chiqarildi."
        db.commit()
        ud.clear()
        await m.reply_text(msg)
    elif st == "pgive":
        parts = t.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await m.reply_text("Format: ID kun (masalan: 123456789 30)")
            return True
        target, days = int(parts[0]), int(parts[1])
        until = max(int(time.time()), premium_until(target)) + days * 86400
        db.execute("insert or replace into premium values(?,?)", (target, until))
        db.commit()
        ud.clear()
        await m.reply_text(f"{target} ga {days} kunlik Premium berildi ({fmt_date(until)} gacha).")
        try:
            await ctx.bot.send_message(target, f"Sizga {days} kunlik Premium berildi. Muddati: {fmt_date(until)}.")
        except Exception:
            pass
    elif st == "setch":
        if t == "-":
            set_setting("channel", "")
            await m.reply_text("Majburiy kanal o'chirildi.")
        elif t.startswith("@") and len(t) > 2:
            set_setting("channel", t)
            await m.reply_text(f"Majburiy kanal: {t}\nBot shu kanalda admin ekanini tekshiring.")
        else:
            await m.reply_text("Format: @kanal_nomi (yoki o'chirish uchun -)")
            return True
        ud.clear()
    elif st == "c_n":
        db.execute("insert into categories(name) values(?)", (t[:40],))
        db.commit()
        ud.clear()
        await m.reply_text(f"Mavzu qo'shildi: {t[:40]}")
    elif st == "plan_d":
        if not t.isdigit() or int(t) < 1:
            await m.reply_text("Kunlar sonini raqam bilan yozing (masalan 30):")
            return True
        ud["days"] = int(t)
        ud["state"] = "plan_p"
        await m.reply_text("Narxi, so'mda (raqam):")
    elif st == "plan_p":
        if not t.isdigit():
            await m.reply_text("Faqat raqam yozing:")
            return True
        d = ud["days"]
        db.execute("insert into products(name,descr,price,kind,content,cat_id) values(?,?,?,?,?,-1)",
                   (f"Premium {d} kun", "", int(t), "premium", str(d)))
        db.commit()
        ud.clear()
        await m.reply_text(f"Tarif qo'shildi: Premium {d} kun, {money(int(t))}.")
    elif st in ("b_n", "b_d", "b_p", "b_f"):
        n = ud.setdefault("new", {"items": []})
        if st == "b_n":
            n["name"] = t; ud["state"] = "b_d"
            await m.reply_text("Qisqacha tavsifini yozing:")
        elif st == "b_d":
            n["descr"] = t; ud["state"] = "b_p"
            await m.reply_text("Narxi, so'mda (raqam). Bepul bo'lsa 0 yozing:")
        elif st == "b_p":
            if not t.isdigit():
                await m.reply_text("Faqat raqam yozing (bepul uchun 0):")
                return True
            n["price"] = int(t); ud["state"] = "b_cat"
            cats = db.execute("select * from categories order by id").fetchall()
            btns = [B(c["name"], callback_data=f"pc:{c['id']}") for c in cats] + [B("Boshqa", callback_data="pc:0")]
            await m.reply_text("Qaysi mavzuga qo'shamiz?", reply_markup=M([btns[i:i + 2] for i in range(0, len(btns), 2)]))
        else:   # b_f
            if t.lower().strip("!. ") == "tayyor":
                items = sorted(n["items"])
                if not items:
                    await m.reply_text("Hali birorta material yubormadingiz. Fayl, rasm yoki havola yuboring.")
                    return True
                pid = db.execute("insert into products(name,descr,price,kind,content,cat_id) values(?,?,?,?,?,?)",
                                 (n["name"], n.get("descr", ""), n["price"], "bundle", "", n.get("cat", 0))).lastrowid
                for pos, (_, k, c) in enumerate(items):
                    db.execute("insert into items(prod_id,kind,content,pos) values(?,?,?,?)", (pid, k, c, pos))
                db.commit()
                ud.clear()
                await m.reply_text(f"To'plam qo'shildi: {n['name']} ({len(items)} ta material).")
            else:
                n["items"].append((m.message_id, "text", t))
                await m.reply_text(f"Havola/matn qabul qilindi ({len(n['items'])}). Yana yuboring yoki \"Tayyor\" deb yozing.")
    elif st.startswith("m_"):
        n = ud.get("new", {})
        if st == "m_n":
            n["name"] = t; ud["state"] = "m_d"; ud["new"] = n
            await m.reply_text("Qisqacha tavsifini yozing:")
        elif st == "m_d":
            n["descr"] = t; ud["state"] = "m_p"
            await m.reply_text("Narxi, so'mda (raqam):")
        elif st == "m_p":
            if not t.isdigit():
                await m.reply_text("Faqat raqam yozing:")
                return True
            n["price"] = int(t); ud["state"] = "m_cat"
            cats = db.execute("select * from categories order by id").fetchall()
            btns = [B(c["name"], callback_data=f"pc:{c['id']}") for c in cats] + [B("Boshqa", callback_data="pc:0")]
            await m.reply_text("Qaysi mavzuga qo'shamiz?", reply_markup=M([btns[i:i + 2] for i in range(0, len(btns), 2)]))
        elif st == "m_c":
            save_product(ud, "text", t)
            ud.clear()
            await m.reply_text("Mahsulot qo'shildi (havola/matn ko'rinishida).")
        else:
            return False
    else:
        return False
    return True


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    m = update.message
    st = ud.get("state")
    uid = update.effective_user.id

    if st == "b_f" and uid in admins():
        if m.document:
            kind, fid = "doc", m.document.file_id
        elif m.photo:
            kind, fid = "photo", m.photo[-1].file_id
        elif m.video:
            kind, fid = "video", m.video.file_id
        else:
            kind, fid = "audio", m.audio.file_id
        ud["new"]["items"].append((m.message_id, kind, fid))
        await m.reply_text(f"Qabul qilindi ({len(ud['new']['items'])}). Yana yuboring yoki \"Tayyor\" deb yozing.")
        return

    if st == "m_c" and uid in admins():
        if m.document:
            save_product(ud, "doc", m.document.file_id)
        else:
            save_product(ud, "photo", m.photo[-1].file_id)
        ud.clear()
        await m.reply_text("Mahsulot qo'shildi.")
        return

    if st == "receipt":
        sid = ud["sid"]
        o = db.execute("select s.*, p.name, p.price from sales s join products p on p.id=s.prod_id where s.id=?",
                       (sid,)).fetchone()
        db.execute("update sales set status='chek yuborildi' where id=?", (sid,))
        db.commit()
        cap = (f"Buyurtma {sid}\nXaridor: @{o['uname']} (id {o['uid']})\nMahsulot: {o['name']}\n"
               f"Summa: {money(o['price'])}\nTo'lov: {o['pm']}")
        kb = M([[B("Tasdiqlash", callback_data=f"ok:{sid}"), B("Rad etish", callback_data=f"no:{sid}")]])
        for adm in admins():
            try:
                if m.photo:
                    await ctx.bot.send_photo(adm, m.photo[-1].file_id, caption=cap, reply_markup=kb)
                else:
                    await ctx.bot.send_document(adm, m.document.file_id, caption=cap, reply_markup=kb)
            except Exception:
                logging.exception("Adminga yuborib bo'lmadi: %s", adm)
        ud.clear()
        await m.reply_text(f"Chek qabul qilindi. Buyurtma {sid} tekshirilmoqda, kuting.")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "Salom! Bu bot:\n"
        "- Instagram va TikTok videolaridan musiqa (mp3) yoki video yuklaydi (\"Yuklash\")\n"
        "- Turli mavzulardagi tayyor qo'llanma va shablonlarni sotadi (\"Mavzular\")\n"
        "- Kutubxona: bitta tugma orqali bir nechta kitob, darslik va fayl beradi (\"Kutubxona\")\n"
        "- Premium obuna beradi (\"Obuna\")\n\n"
        "Materiallar umumiy ma'lumot uchun; huquqiy, moliyaviy va boshqa muhim masalalarda mutaxassis bilan "
        "maslahatlashing. Yuklangan kontentni faqat shaxsiy foydalanish uchun ishlating.",
        reply_markup=main_kb(update.effective_user.id))


MENU.update({
    "Yuklash": open_download, "Mavzular": show_cats, "Kutubxona": show_lib, "Obuna": sub_menu, "Aloqa": ask_support,
    "Admin panel": admin_panel, "Orqaga": back_main,
    "Statistika": admin_only(stats), "E'lon yuborish": admin_only(bc_start),
    "Mahsulot qo'shish": admin_only(wiz_start), "To'plam qo'shish": admin_only(bundle_start), "Hamma mahsulotlar": admin_only(all_products),
    "Mavzu qo'shish": admin_only(cat_add_start), "Mavzularni boshqarish": admin_only(cat_manage),
    "Obuna tariflari": admin_only(plans_manage), "Premium berish": admin_only(pgive_start),
    "Bloklash": admin_only(ban_start), "Blokdan chiqarish": admin_only(unban_start),
    "Majburiy kanal": admin_only(channel_start), "Buyurtmalar": admin_only(orders),
    "Adminlar": list_admins,
})


def main():
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("stats", admin_only(stats)))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("deladmin", del_admin))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()
