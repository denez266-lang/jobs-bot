import httpx
import os, json, time, asyncio, logging, hashlib, html, io, threading
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)
for _n in ("httpx", "httpcore", "telegram", "tornado.access"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("jobsbot")

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
H = ParseMode.HTML
e = lambda s: html.escape(str(s or ""))

DEFAULT_DATA = {
    "jobs": {}, "next_id": 1, "users": {}, "views": {},
    "settings": {
        "welcome": "👋 أهلاً بك!\nهنا تلاقي تفاصيل الوظائف المنشورة بالقناة: الشروط والراتب والعمولة.\nاضغط على الوظيفة لعرض تفاصيلها 👇",
        "footer": "", "channel": "",
        "maintenance": False,
    },
}
FIELDS = [("title", "🏷️ عنوان الوظيفة"), ("hours", "⏰ ساعات العمل"), ("contact", "📩 طريقة التقديم / التواصل")]
CFIELDS = [("name", "🌍 اسم الدولة"), ("salary", "💰 الراتب"), ("commission", "🎯 العمولة"), ("terms", "📋 الشروط")]
FIELD_HINT = {
    "title": "اكتب عنوان الوظيفة (مثال: رد على رسائل الصفحات)",
    "hours": "اكتب ساعات العمل (مثال: 4 ساعات يومياً، أو - إذا ماكو)",
    "contact": "اكتب يوزر التواصل (مثال @username) أو رابط، أو اكتب - إذا ماكو",
    "name": "اكتب اسم الدولة (مثال: العراق)",
    "salary": "اكتب الراتب لهذي الدولة (مثال: 150$ شهرياً ثابت)",
    "commission": "اكتب العمولة لهذي الدولة (أو - إذا ماكو)",
    "terms": "اكتب الشروط لهذي الدولة، تقدر تكتب عدة أسطر",
}

lock = threading.Lock()
DB = json.loads(json.dumps(DEFAULT_DATA))
dirty = {"v": False, "last_backup": 0.0}

def load_local():
    global DB
    if os.path.exists(DATA_FILE):
        try:
            d = json.load(open(DATA_FILE, encoding="utf-8"))
            for k, v in DEFAULT_DATA.items():
                d.setdefault(k, json.loads(json.dumps(v)))
            for k, v in DEFAULT_DATA["settings"].items():
                d["settings"].setdefault(k, v)
            DB = d
        except Exception:
            log.exception("load failed")

def save():
    with lock:
        tmp = DATA_FILE + ".tmp"
        json.dump(DB, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    dirty["v"] = True

def is_admin(uid): return uid in ADMIN_IDS

# ---------- backup to admin chat (Render free disk is ephemeral) ----------
async def restore_from_admin(app):
    for aid in ADMIN_IDS:
        try:
            chat = await app.bot.get_chat(aid)
            pm = chat.pinned_message
            if pm and pm.document and pm.document.file_name == "jobsbot-backup.json":
                f = await app.bot.get_file(pm.document.file_id)
                b = await f.download_as_bytearray()
                open(DATA_FILE, "wb").write(bytes(b))
                load_local()
                dirty["last_backup"] = pm.date.timestamp()
                log.info("restored backup from admin %s", aid)
                return True
        except TelegramError as ex:
            log.warning("restore failed %s", ex)
    return False

async def do_backup(app, force=False):
    if not dirty["v"] and not force:
        return
    if not ADMIN_IDS: return
    aid = sorted(ADMIN_IDS)[0]
    try:
        data = json.dumps(DB, ensure_ascii=False).encode()
        bio = io.BytesIO(data); bio.name = "jobsbot-backup.json"
        old = None
        try:
            old = (await app.bot.get_chat(aid)).pinned_message
        except TelegramError: pass
        msg = await app.bot.send_document(aid, bio, caption="🗂️ نسخة البيانات", disable_notification=True)
        await app.bot.pin_chat_message(aid, msg.message_id, disable_notification=True)
        if old and old.document and old.document.file_name == "jobsbot-backup.json":
            try: await app.bot.delete_message(aid, old.message_id)
            except TelegramError: pass
        dirty["v"] = False
        dirty["last_backup"] = time.time()
    except TelegramError as ex:
        log.warning("backup failed %s", ex)

async def backup_loop(app):
    while True:
        await asyncio.sleep(600)
        if dirty["v"] and time.time() - dirty["last_backup"] >= 48 * 3600:
            await do_backup(app)

async def pinned_ts(app):
    for aid in ADMIN_IDS:
        try:
            pm = (await app.bot.get_chat(aid)).pinned_message
            if pm and pm.document and pm.document.file_name == "jobsbot-backup.json":
                dirty["last_backup"] = pm.date.timestamp(); return
        except TelegramError: pass

async def del_pin_notice(update: Update, ctx):
    try: await update.message.delete()
    except TelegramError: pass

# ---------- user side ----------
def jobs_kb():
    rows = [[B(f"💼 {j['title']}", callback_data=f"job:{jid}")] for jid, j in DB["jobs"].items() if j.get("active", True)]
    return M(rows) if rows else None

def contact_url(c):
    c = (c or "").strip()
    if c.startswith("@") and len(c) > 2 and " " not in c: return "https://t.me/" + c[1:]
    if c.startswith("http://") or c.startswith("https://"): return c.split()[0]
    return None

def job_text(j):
    parts = [f"💼 <b>{e(j['title'])}</b>", ""]
    if j.get("hours") and j["hours"] != "-": parts.append(f"⏰ <b>ساعات العمل:</b> {e(j['hours'])}")
    cs = j.get("countries", [])
    if cs: parts += ["", "🌍 <b>اختر دولتك لعرض الشروط والراتب والعمولة:</b>"]
    else: parts += ["", "🕐 التفاصيل قريباً."]
    f = DB["settings"].get("footer")
    if f: parts += ["", e(f)]
    return "\n".join(parts)

def job_kb(jid, j):
    rows = [[B(f"🌍 {c['name']}", callback_data=f"c:{jid}:{i}")] for i, c in enumerate(j.get("countries", []))]
    rows.append([B("📂 باقي الوظائف", callback_data="list")])
    return M(rows)

def country_text(j, c):
    parts = [f"💼 <b>{e(j['title'])}</b>", f"🌍 <b>{e(c['name'])}</b>", ""]
    if j.get("hours") and j["hours"] != "-": parts.append(f"⏰ <b>ساعات العمل:</b> {e(j['hours'])}")
    if c.get("salary") and c["salary"] != "-": parts.append(f"💰 <b>الراتب:</b> {e(c['salary'])}")
    if c.get("commission") and c["commission"] != "-": parts.append(f"🎯 <b>العمولة:</b> {e(c['commission'])}")
    if c.get("terms") and c["terms"] != "-": parts += ["", "📋 <b>الشروط:</b>", e(c["terms"])]
    ct = j.get("contact")
    if ct and ct != "-": parts += ["", f"📩 <b>للتقديم / التواصل:</b> {e(ct)}"]
    f = DB["settings"].get("footer")
    if f: parts += ["", e(f)]
    return "\n".join(parts)

def country_kb(jid, j):
    rows = []
    u = contact_url(j.get("contact"))
    if u: rows.append([B("📩 تقديم / تواصل", url=u)])
    rows.append([B("🌍 دول أخرى", callback_data=f"job:{jid}"), B("📂 الوظائف", callback_data="list")])
    return M(rows)

def track_user(u):
    if not u: return
    k = str(u.id)
    rec = DB["users"].get(k)
    now = int(time.time())
    if not rec:
        DB["users"][k] = {"name": u.first_name or "", "username": u.username or "", "first": now, "last": now}
    else:
        rec["last"] = now
    save()

async def send_job(msg_target, jid, edit=False):
    j = DB["jobs"].get(str(jid))
    if not j or not j.get("active", True):
        txt, kb = "😕 هذه الوظيفة غير متوفرة حالياً.", jobs_kb()
    else:
        txt, kb = job_text(j), job_kb(str(jid), j)
        DB["views"][str(jid)] = DB["views"].get(str(jid), 0) + 1; save()
    if edit:
        try: await msg_target.edit_text(txt, parse_mode=H, reply_markup=kb, disable_web_page_preview=True); return
        except TelegramError: pass
    await msg_target.reply_text(txt, parse_mode=H, reply_markup=kb, disable_web_page_preview=True)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    track_user(u)
    ctx.user_data.pop("state", None)
    if DB["settings"].get("maintenance") and not is_admin(u.id):
        await update.message.reply_text("🛠️ البوت تحت الصيانة حالياً، ارجع بعد شوي."); return
    arg = ctx.args[0] if ctx.args else ""
    if arg.isdigit():
        await send_job(update.message, arg); return
    kb = jobs_kb()
    txt = e(DB["settings"]["welcome"])
    if not kb: txt += "\n\n🕐 ما مضافة وظائف حالياً، تابع القناة."
    else: pass
    rows = list(kb.inline_keyboard) if kb else []
    if is_admin(u.id): rows.append([B("⚙️ لوحة الأدمن", callback_data="adm")])
    await update.message.reply_text(txt, parse_mode=H, reply_markup=M(rows) if rows else None)

async def myid(update: Update, ctx):
    await update.message.reply_text(f"🆔 {update.effective_user.id}")

# ---------- admin ----------
def adm_kb():
    return M([
        [B("➕ إضافة وظيفة", callback_data="a:add"), B("📋 الوظائف", callback_data="a:list")],
        [B("📊 الإحصائيات", callback_data="a:stats"), B("📣 إرسال للجميع", callback_data="a:bc")],
        [B("👋 رسالة الترحيب", callback_data="a:welcome"), B("📝 نص أسفل الوظيفة", callback_data="a:footer")],
        [B("🛠️ وضع الصيانة: " + ("مفعّل ✅" if DB["settings"].get("maintenance") else "مطفي ❌"), callback_data="a:maint")],
        [B("📢 ربط القناة: " + (DB["settings"].get("channel") or "غير محدد"), callback_data="a:chan")],
        [B("💾 نسخة احتياطية الآن", callback_data="a:backup")],
    ])

async def admin_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id): return
    ctx.user_data.pop("state", None)
    await update.message.reply_text("⚙️ <b>لوحة الأدمن</b>", parse_mode=H, reply_markup=adm_kb())

async def show_job_admin(q, jid):
    j = DB["jobs"].get(jid)
    if not j:
        await q.edit_message_text("❌ الوظيفة غير موجودة.", reply_markup=M([[B("🔙", callback_data="a:list")]])); return
    me = (await q.get_bot().get_me()).username
    link = f"https://t.me/{me}?start={jid}"
    cl = "\n".join(f"• {e(c['name'])}" for c in j.get("countries", [])) or "ماكو دول بعد"
    txt = (f"💼 <b>{e(j['title'])}</b>\n⏰ {e(j.get('hours'))}\n📩 {e(j.get('contact'))}\n\n🌍 <b>الدول:</b>\n{cl}"
           f"\n\n🔗 <code>{link}</code>\n👁️ المشاهدات: {DB['views'].get(jid,0)}  {'🟢 ظاهرة' if j.get('active', True) else '🔴 مخفية'}")
    rows = [[B(f"🌍 {c['name']}", callback_data=f"ac:{jid}:{i}")] for i, c in enumerate(j.get("countries", []))]
    rows.append([B("➕ إضافة دولة", callback_data=f"nc:{jid}")])
    rows.append([B("📢 نشر بالقناة", callback_data=f"pub:{jid}")])
    rows.append([B("✏️ العنوان", callback_data=f"e:{jid}:title"), B("✏️ الساعات", callback_data=f"e:{jid}:hours"), B("✏️ التواصل", callback_data=f"e:{jid}:contact")])
    rows.append([B("🙈 إخفاء" if j.get("active", True) else "👁️ إظهار", callback_data=f"t:{jid}"), B("🗑️ حذف", callback_data=f"d:{jid}")])
    rows.append([B("🔙 الوظائف", callback_data="a:list")])
    await q.edit_message_text(txt, parse_mode=H, reply_markup=M(rows), disable_web_page_preview=True)

async def show_country_admin(q, jid, i):
    j = DB["jobs"].get(jid)
    if not j or i >= len(j.get("countries", [])):
        await q.edit_message_text("❌ غير موجودة.", reply_markup=M([[B("🔙", callback_data=f"a:j:{jid}")]])); return
    c = j["countries"][i]
    rows = [[B(f"✏️ {lbl}", callback_data=f"ec:{jid}:{i}:{k}")] for k, lbl in CFIELDS]
    rows.append([B("🗑️ حذف الدولة", callback_data=f"dc:{jid}:{i}")])
    rows.append([B("🔙", callback_data=f"a:j:{jid}")])
    await q.edit_message_text(country_text(j, c), parse_mode=H, reply_markup=M(rows))

async def publish(ctx, jid):
    j = DB["jobs"].get(jid)
    ch = DB["settings"].get("channel")
    if not ch: return "❌ ما محدد قناة. من لوحة الأدمن اضغط 📢 ربط القناة."
    me = (await ctx.bot.get_me()).username
    cs = " • ".join(c["name"] for c in j.get("countries", []))
    txt = f"💼 <b>{e(j['title'])}</b>\n"
    if j.get("hours") and j["hours"] != "-": txt += f"⏰ {e(j['hours'])}\n"
    if cs: txt += f"🌍 {e(cs)}\n"
    txt += "\n👇 اضغط الزر لعرض الشروط والراتب والعمولة"
    kb = M([[B("📋 تفاصيل الوظيفة", url=f"https://t.me/{me}?start={jid}")]])
    try:
        chat = int(ch) if ch.lstrip("-").isdigit() else ch
        m = await ctx.bot.send_message(chat, txt, parse_mode=H, reply_markup=kb)
        j["pub_msg"] = m.message_id; save()
        return "✅ اننشرت بالقناة."
    except TelegramError as ex:
        return f"❌ فشل النشر: {ex}\nتأكد البوت مضاف أدمن بالقناة وعنده صلاحية نشر الرسائل."

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    d = q.data
    await q.answer()
    if d == "list":
        kb = jobs_kb()
        await q.edit_message_text("📂 <b>الوظائف المتوفرة:</b>" if kb else "🕐 ما مضافة وظائف حالياً.", parse_mode=H, reply_markup=kb); return
    if d.startswith("job:"):
        await send_job(q.message, d[4:], edit=True); return
    if d.startswith("c:"):
        _, jid, i = d.split(":")
        j = DB["jobs"].get(jid)
        if not j or not j.get("active", True) or int(i) >= len(j.get("countries", [])):
            await q.edit_message_text("😕 غير متوفرة.", reply_markup=jobs_kb()); return
        await q.edit_message_text(country_text(j, j["countries"][int(i)]), parse_mode=H, reply_markup=country_kb(jid, j)); return
    if not is_admin(uid): return
    if d == "adm":
        await q.message.reply_text("⚙️ <b>لوحة الأدمن</b>", parse_mode=H, reply_markup=adm_kb()); return
    if d == "a:menu":
        ctx.user_data.pop("state", None)
        await q.edit_message_text("⚙️ <b>لوحة الأدمن</b>", parse_mode=H, reply_markup=adm_kb()); return
    if d == "a:add":
        ctx.user_data["state"] = {"t": "add", "step": 0, "job": {}}
        await q.edit_message_text("➕ <b>وظيفة جديدة</b>\n\n" + FIELD_HINT["title"], parse_mode=H,
                                  reply_markup=M([[B("❌ إلغاء", callback_data="a:menu")]])); return
    if d == "a:list":
        rows = [[B(("🟢 " if j.get("active", True) else "🔴 ") + j["title"], callback_data=f"a:j:{jid}")] for jid, j in DB["jobs"].items()]
        rows.append([B("🔙", callback_data="a:menu")])
        await q.edit_message_text("📋 <b>الوظائف</b>" if DB["jobs"] else "ما مضافة وظائف بعد.", parse_mode=H, reply_markup=M(rows)); return
    if d.startswith("a:j:"):
        await show_job_admin(q, d[4:]); return
    if d.startswith("ac:"):
        _, jid, i = d.split(":"); await show_country_admin(q, jid, int(i)); return
    if d.startswith("nc:"):
        jid = d[3:]
        ctx.user_data["state"] = {"t": "addc", "jid": jid, "step": 0, "c": {}}
        await q.edit_message_text("➕ <b>دولة جديدة</b>\n\n" + FIELD_HINT["name"], parse_mode=H,
                                  reply_markup=M([[B("❌ إلغاء", callback_data=f"a:j:{jid}")]])); return
    if d.startswith("ec:"):
        _, jid, i, k = d.split(":")
        ctx.user_data["state"] = {"t": "editc", "jid": jid, "i": int(i), "k": k}
        await q.edit_message_text("✏️ " + FIELD_HINT[k], reply_markup=M([[B("❌ إلغاء", callback_data=f"ac:{jid}:{i}")]])); return
    if d.startswith("dc:"):
        _, jid, i = d.split(":")
        await q.edit_message_text("⚠️ حذف هذي الدولة؟", reply_markup=M([[B("🗑️ نعم", callback_data=f"dcy:{jid}:{i}"), B("❌ لا", callback_data=f"ac:{jid}:{i}")]])); return
    if d.startswith("dcy:"):
        _, jid, i = d.split(":")
        j = DB["jobs"].get(jid)
        if j and int(i) < len(j.get("countries", [])): j["countries"].pop(int(i)); save()
        await show_job_admin(q, jid); return
    if d.startswith("pub:"):
        msg = await publish(ctx, d[4:])
        await q.message.reply_text(msg); return
    if d == "a:chan":
        ctx.user_data["state"] = {"t": "setting", "k": "channel"}
        await q.edit_message_text("📢 أضف البوت أدمن بالقناة (صلاحية نشر الرسائل)، ثم اكتب يوزر القناة (مثال @mychannel) أو رقمها:",
                                  reply_markup=M([[B("❌ إلغاء", callback_data="a:menu")]])); return
    if d == "more":
        st = ctx.user_data.get("last_jid")
        if st:
            ctx.user_data["state"] = {"t": "addc", "jid": st, "step": 0, "c": {}}
            await q.edit_message_text("➕ <b>دولة جديدة</b>\n\n" + FIELD_HINT["name"], parse_mode=H); return
    if d.startswith("e:"):
        _, jid, k = d.split(":")
        ctx.user_data["state"] = {"t": "edit", "jid": jid, "k": k}
        await q.edit_message_text("✏️ " + FIELD_HINT[k], reply_markup=M([[B("❌ إلغاء", callback_data=f"a:j:{jid}")]])); return
    if d.startswith("t:"):
        j = DB["jobs"].get(d[2:])
        if j: j["active"] = not j.get("active", True); save()
        await show_job_admin(q, d[2:]); return
    if d.startswith("d:"):
        jid = d[2:]
        await q.edit_message_text("⚠️ تأكيد حذف الوظيفة؟ ما تتراجع.",
            reply_markup=M([[B("🗑️ نعم احذف", callback_data=f"dy:{jid}"), B("❌ لا", callback_data=f"a:j:{jid}")]])); return
    if d.startswith("dy:"):
        DB["jobs"].pop(d[3:], None); save()
        await q.edit_message_text("✅ انحذفت.", reply_markup=M([[B("🔙 الوظائف", callback_data="a:list")]])); return
    if d == "a:stats":
        n = len(DB["users"]); now = time.time()
        day = sum(1 for u in DB["users"].values() if now - u["last"] < 86400)
        new = sum(1 for u in DB["users"].values() if now - u["first"] < 86400)
        top = sorted(DB["views"].items(), key=lambda x: -x[1])[:10]
        lines = [f"📊 <b>الإحصائيات</b>", f"👥 كل المستخدمين: {n}", f"🆕 جدد خلال 24 ساعة: {new}",
                 f"🔥 نشطين خلال 24 ساعة: {day}", f"💼 الوظائف: {len(DB['jobs'])}", "", "👁️ <b>مشاهدات الوظائف:</b>"]
        for jid, v in top:
            if jid in DB["jobs"]: lines.append(f"• {e(DB['jobs'][jid]['title'])}: {v}")
        if not top: lines.append("ماكو بعد")
        await q.edit_message_text("\n".join(lines), parse_mode=H, reply_markup=M([[B("🔙", callback_data="a:menu")]])); return
    if d == "a:welcome":
        ctx.user_data["state"] = {"t": "setting", "k": "welcome"}
        await q.edit_message_text("👋 الرسالة الحالية:\n\n" + e(DB["settings"]["welcome"]) + "\n\nاكتب الرسالة الجديدة:", parse_mode=H,
                                  reply_markup=M([[B("❌ إلغاء", callback_data="a:menu")]])); return
    if d == "a:footer":
        ctx.user_data["state"] = {"t": "setting", "k": "footer"}
        await q.edit_message_text("📝 نص يظهر أسفل كل وظيفة (الحالي: " + (e(DB["settings"]["footer"]) or "فارغ") + ")\nاكتب النص الجديد، أو اكتب - لمسحه:", parse_mode=H,
                                  reply_markup=M([[B("❌ إلغاء", callback_data="a:menu")]])); return
    if d == "a:maint":
        DB["settings"]["maintenance"] = not DB["settings"].get("maintenance"); save()
        await q.edit_message_text("⚙️ <b>لوحة الأدمن</b>", parse_mode=H, reply_markup=adm_kb()); return
    if d == "a:backup":
        await do_backup(ctx.application, force=True)
        await q.message.reply_text("✅ تم حفظ النسخة."); return
    if d == "a:bc":
        ctx.user_data["state"] = {"t": "bc"}
        await q.edit_message_text(f"📣 اكتب الرسالة اللي تبي ترسلها لكل المستخدمين ({len(DB['users'])} مستخدم). راح أعرضها لك للتأكيد قبل الإرسال.",
                                  reply_markup=M([[B("❌ إلغاء", callback_data="a:menu")]])); return
    if d == "bcy":
        text = ctx.user_data.pop("bc_text", None)
        if not text: return
        await q.edit_message_text("⏳ جاري الإرسال...")
        asyncio.create_task(broadcast(ctx.application, q.message.chat_id, text)); return
    if d == "bcn":
        ctx.user_data.pop("bc_text", None)
        await q.edit_message_text("تم الإلغاء.", reply_markup=M([[B("🔙", callback_data="a:menu")]])); return

async def broadcast(app, admin_chat, text):
    ok = fail = 0
    for uid in list(DB["users"].keys()):
        try:
            await app.bot.send_message(int(uid), text, disable_web_page_preview=True); ok += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)
    await app.bot.send_message(admin_chat, f"✅ انتهى الإرسال\nوصلت: {ok}\nفشلت: {fail}")

async def text_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    track_user(u)
    st = ctx.user_data.get("state")
    if not st or not is_admin(u.id):
        if DB["settings"].get("maintenance") and not is_admin(u.id):
            await update.message.reply_text("🛠️ البوت تحت الصيانة حالياً."); return
        kb = jobs_kb()
        await update.message.reply_text("اختر وظيفة من القائمة 👇" if kb else "تابع القناة للوظائف الجديدة 📢", reply_markup=kb); return
    text = update.message.text
    if st["t"] == "add":
        key = FIELDS[st["step"]][0]
        st["job"][key] = text.strip()
        st["step"] += 1
        if st["step"] < len(FIELDS):
            nk = FIELDS[st["step"]][0]
            await update.message.reply_text(FIELD_HINT[nk], reply_markup=M([[B("❌ إلغاء", callback_data="a:menu")]])); return
        jid = str(DB["next_id"]); DB["next_id"] += 1
        st["job"]["active"] = True; st["job"]["countries"] = []
        DB["jobs"][jid] = st["job"]; save()
        ctx.user_data["state"] = {"t": "addc", "jid": jid, "step": 0, "c": {}}
        await update.message.reply_text("✅ انحفظت الوظيفة. هسه نضيف الدول.\n\n🌍 " + FIELD_HINT["name"]); return
    if st["t"] == "addc":
        key = CFIELDS[st["step"]][0]
        st["c"][key] = text.strip(); st["step"] += 1
        if st["step"] < len(CFIELDS):
            await update.message.reply_text(FIELD_HINT[CFIELDS[st["step"]][0]], reply_markup=M([[B("❌ إلغاء", callback_data=f"a:j:{st['jid']}")]])); return
        j = DB["jobs"].get(st["jid"])
        if j: j.setdefault("countries", []).append(st["c"]); save()
        ctx.user_data.pop("state"); ctx.user_data["last_jid"] = st["jid"]
        await update.message.reply_text("✅ انضافت الدولة.", reply_markup=M([
            [B("➕ دولة ثانية", callback_data="more")],
            [B("📢 نشر بالقناة", callback_data=f"pub:{st['jid']}")],
            [B("📄 الوظيفة", callback_data=f"a:j:{st['jid']}")]])); return
    if st["t"] == "editc":
        j = DB["jobs"].get(st["jid"])
        if j and st["i"] < len(j.get("countries", [])): j["countries"][st["i"]][st["k"]] = text.strip(); save()
        ctx.user_data.pop("state")
        await update.message.reply_text("✅ تم التعديل.", reply_markup=M([[B("🌍 الدولة", callback_data=f"ac:{st['jid']}:{st['i']}")]])); return
    if st["t"] == "edit":
        j = DB["jobs"].get(st["jid"])
        if j: j[st["k"]] = text.strip(); save()
        ctx.user_data.pop("state")
        await update.message.reply_text("✅ تم التعديل.", reply_markup=M([[B("📄 الوظيفة", callback_data=f"a:j:{st['jid']}")]])); return
    if st["t"] == "setting":
        v = "" if text.strip() == "-" else (text.strip() if st["k"] == "channel" else text)
        DB["settings"][st["k"]] = v; save()
        ctx.user_data.pop("state")
        await update.message.reply_text("✅ تم الحفظ.", reply_markup=M([[B("🔙", callback_data="a:menu")]])); return
    if st["t"] == "bc":
        ctx.user_data["bc_text"] = text
        ctx.user_data.pop("state")
        await update.message.reply_text("👀 هذي المعاينة:\n\n" + text + "\n\n— — —\nأرسلها لـ " + str(len(DB["users"])) + " مستخدم؟",
            reply_markup=M([[B("✅ إرسال", callback_data="bcy"), B("❌ إلغاء", callback_data="bcn")]]), disable_web_page_preview=True); return

async def ping_loop():
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if not base: return
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            await asyncio.sleep(540)
            try: await c.get(base + "/health")
            except Exception: pass

async def on_error(update, ctx):
    log.error("handler error: %s", ctx.error)

async def post_init(app):
    if not os.path.exists(DATA_FILE):
        await restore_from_admin(app)
    else:
        load_local()
        await pinned_ts(app)
    asyncio.create_task(backup_loop(app))
    asyncio.create_task(ping_loop())

def main():
    load_local()
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_error_handler(on_error)
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, del_pin_notice))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))
    port = int(os.environ.get("PORT", "10000"))
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if base:
        secret = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
        app.run_webhook(listen="0.0.0.0", port=port, url_path=secret,
                        webhook_url=f"{base}/{secret}", drop_pending_updates=False,
                        allowed_updates=Update.ALL_TYPES)
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
