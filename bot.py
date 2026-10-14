"""AkkauntBozor — Telegram bot.
Ishga tushirish:
  pip install "python-telegram-bot==21.*"
  BOT_TOKEN=... ADMIN_ID=... CARD="8600 ..." HOLDER="ISM FAMILIYA" python bot.py
"""
import os, re, sqlite3, time, logging
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters, ContextTypes)

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
ADMIN = int(os.environ["ADMIN_ID"])
CARD = os.getenv("CARD", "8600 0000 0000 0000")
HOLDER = os.getenv("HOLDER", "ISM FAMILIYA")

db = sqlite3.connect(os.getenv("DB_PATH", "shop.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists accounts(id integer primary key autoincrement,
  u text, niche text, fol int, age text, price int, sold int default 0);
create table if not exists orders(id text primary key, uid int, uname text,
  acc_id int, new_u text, pm text, status text, at int);
""")


def money(n): return f"{int(n):,}".replace(",", " ") + " so'm"
def mask(u): return u[:3] + "•" * max(3, len(u) - 3)
def valid_u(v): return bool(re.fullmatch(r"[a-z0-9._]{1,30}", v)) and ".." not in v and not v.endswith(".")
def is_admin(update): return update.effective_user.id == ADMIN


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "AkkauntBozor — tayyor Instagram akkauntlar.\nAkkauntlarni ko'rish uchun /akkauntlar ni bosing.")


async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.execute("select * from accounts where sold=0 order by id desc limit 20").fetchall()
    if not rows:
        await update.message.reply_text("Hozircha sotuvda akkaunt yo'q.")
        return
    for a in rows:
        text = (f"@{mask(a['u'])}\nYo'nalish: {a['niche']}\nObunachi: {a['fol']:,}\n"
                f"Yoshi: {a['age']}\nNarx: {money(a['price'])}").replace(",", " ")
        await update.message.reply_text(text, reply_markup=M([[B("Sotib olish", callback_data=f"buy:{a['id']}")]]))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kind, _, val = q.data.partition(":")
    ud = ctx.user_data

    if kind == "buy":
        a = db.execute("select * from accounts where id=? and sold=0", (val,)).fetchone()
        if not a:
            await q.message.reply_text("Bu akkaunt sotilgan yoki topilmadi.")
            return
        ud.clear()
        ud.update(acc=a["id"], state="new_u")
        await q.message.reply_text(
            "Topshirilgandan keyin qo'yiladigan yangi username'ni yozing "
            "(lotin harflari, raqam, nuqta, _).\nO'zgartirmaslik uchun - belgisini yuboring.")

    elif kind == "pm" and ud.get("state") == "pm":
        a = db.execute("select * from accounts where id=?", (ud["acc"],)).fetchone()
        if not a or a["sold"]:
            await q.message.reply_text("Afsuski, bu akkaunt sotilib bo'ldi.")
            ud.clear()
            return
        oid = f"AB{int(time.time()) % 10**7}"
        db.execute("insert into orders values(?,?,?,?,?,?,?,?)",
                   (oid, q.from_user.id, q.from_user.username or q.from_user.full_name,
                    a["id"], ud["new_u"], val, "chek kutilmoqda", int(time.time())))
        db.commit()
        ud.update(state="receipt", oid=oid)
        await q.message.reply_text(
            f"Buyurtma: {oid}\nSumma: {money(a['price'])}\nTo'lov usuli: {val}\n"
            f"Karta: {CARD}\nQabul qiluvchi: {HOLDER}\n\n"
            f"To'lov izohiga {oid} kodini yozing. To'lagach, chek rasmini shu yerga yuboring.")

    elif kind in ("ok", "no") and q.from_user.id == ADMIN:
        o = db.execute("select * from orders where id=?", (val,)).fetchone()
        if not o or o["status"] in ("tasdiqlandi", "rad etildi"):
            return
        status = "tasdiqlandi" if kind == "ok" else "rad etildi"
        db.execute("update orders set status=? where id=?", (status, val))
        if kind == "ok":
            db.execute("update accounts set sold=1 where id=?", (o["acc_id"],))
            msg = (f"Buyurtma {val}: to'lov tasdiqlandi. "
                   "Akkaunt ma'lumotlarini tez orada admin yuboradi.")
        else:
            msg = f"Buyurtma {val}: to'lov tasdiqlanmadi. Savol bo'lsa, admin bilan bog'laning."
        db.commit()
        await ctx.bot.send_message(o["uid"], msg)
        await q.edit_message_caption(f"{q.message.caption}\n\n→ {status.upper()}")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    if ud.get("state") != "new_u":
        return
    v = update.message.text.strip().lower().lstrip("@")
    if v != "-" and not valid_u(v):
        await update.message.reply_text("Format noto'g'ri. Faqat a-z, 0-9, nuqta va _ (30 belgigacha). Qaytadan yozing.")
        return
    ud["new_u"] = "" if v == "-" else v
    ud["state"] = "pm"
    await update.message.reply_text(
        "To'lov usulini tanlang:",
        reply_markup=M([[B(x, callback_data=f"pm:{x}") for x in ("Click", "Payme", "Karta")]]))


async def on_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    if ud.get("state") != "receipt":
        return
    oid = ud["oid"]
    o = db.execute("select o.*, a.u as acc_u, a.price from orders o join accounts a on a.id=o.acc_id where o.id=?",
                   (oid,)).fetchone()
    db.execute("update orders set status='chek yuborildi' where id=?", (oid,))
    db.commit()
    cap = (f"Buyurtma {oid}\nXaridor: @{o['uname']} (id {o['uid']})\nAkkaunt: @{o['acc_u']}\n"
           f"Summa: {money(o['price'])}\nTo'lov: {o['pm']}\nYangi username: {o['new_u'] or 'o`zgarmaydi'}")
    kb = M([[B("Tasdiqlash", callback_data=f"ok:{oid}"), B("Rad etish", callback_data=f"no:{oid}")]])
    m = update.message
    if m.photo:
        await ctx.bot.send_photo(ADMIN, m.photo[-1].file_id, caption=cap, reply_markup=kb)
    else:
        await ctx.bot.send_document(ADMIN, m.document.file_id, caption=cap, reply_markup=kb)
    ud.clear()
    await m.reply_text(f"Chek qabul qilindi. Buyurtma {oid} tekshirilmoqda, kuting.")


# ---------- Admin buyruqlari ----------
async def add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        u, niche, fol, age, price = [x.strip() for x in " ".join(ctx.args).split("|")]
        u = u.lower().lstrip("@")
        assert valid_u(u)
        db.execute("insert into accounts(u,niche,fol,age,price) values(?,?,?,?,?)",
                   (u, niche, int(fol), age, int(price)))
        db.commit()
        await update.message.reply_text(f"Qo'shildi: @{u}")
    except Exception:
        await update.message.reply_text("Format:\n/add username | yo'nalish | obunachi | yoshi | narx\n"
                                        "Masalan:\n/add fit.life.uz | Fitnes | 12400 | 3 yil | 450000")


async def all_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    rows = db.execute("select * from accounts order by id").fetchall()
    text = "\n".join(f"#{a['id']} @{a['u']} · {money(a['price'])} · {'SOTILGAN' if a['sold'] else 'sotuvda'}"
                     for a in rows) or "Bo'sh."
    await update.message.reply_text(text + "\n\n/sold ID · /unsold ID · /del ID")


def _by_id(sql):
    async def h(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update): return
        if not ctx.args or not ctx.args[0].isdigit():
            await update.message.reply_text("ID kiriting. Masalan: /sold 3")
            return
        db.execute(sql, (int(ctx.args[0]),))
        db.commit()
        await update.message.reply_text("Bajarildi. /all")
    return h


async def orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    rows = db.execute("select * from orders order by at desc limit 15").fetchall()
    await update.message.reply_text(
        "\n".join(f"{o['id']} · @{o['uname']} · {o['status']}" for o in rows) or "Buyurtma yo'q.")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("akkauntlar", show_list))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("all", all_accounts))
    app.add_handler(CommandHandler("sold", _by_id("update accounts set sold=1 where id=?")))
    app.add_handler(CommandHandler("unsold", _by_id("update accounts set sold=0 where id=?")))
    app.add_handler(CommandHandler("del", _by_id("delete from accounts where id=?")))
    app.add_handler(CommandHandler("orders", orders))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_receipt))
    app.run_polling()


if __name__ == "__main__":
    main()
