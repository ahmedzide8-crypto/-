import logging
import os
import re
import threading
import time
import requests
from datetime import date as _date, time as _time

import urllib.parse
from flask import Flask, request as http_req, Response
from itsdangerous import URLSafeTimedSerializer

import database as db
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    PicklePersistence,
    filters,
)

# ── إعدادات البيئة (يقبل كلا الاسمين) ───────────────────────
TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
assert TOKEN, "يجب ضبط BOT_TOKEN أو TELEGRAM_BOT_TOKEN"
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])

# ── إعدادات OAuth لإنستغرام (لتدفق الربط الذاتي) ─────────────
# IG_SHOP_ACCOUNT_ID / IG_ACCESS_TOKEN / IG_SHOP_OWNER_TELEGRAM_ID
# تُقرأ من البيئة في seed_ig_shops_from_env() فقط، ثم يعتمد الكود على ig_shops في DB
IG_APP_ID        = os.environ.get("IG_APP_ID", "")
IG_APP_SECRET    = os.environ.get("IG_APP_SECRET", "")
IG_REDIRECT_URI  = os.environ.get("IG_REDIRECT_URI", "")
STATE_SECRET_KEY = os.environ.get("STATE_SECRET_KEY", "change-me-in-prod")

# ── تهيئة قاعدة البيانات ─────────────────────────────────────
logging.basicConfig(level=logging.WARNING)
db.init_db()
db.migrate_from_json("products.json", OWNER_CHAT_ID)  # no-op إن سبق تنفيذها
db.cleanup_admin_shop(OWNER_CHAT_ID)                  # تنظيف لمرة واحدة
db.seed_ig_shops_from_env()                           # no-op إن سبق التنفيذ

# ── تحقق عند بدء التشغيل ─────────────────────────────────────
_env_admin = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
if _env_admin:
    _admin_ok = db.is_admin(int(_env_admin))
    logging.warning("[STARTUP] ADMIN_TELEGRAM_ID=%s → is_admin=%s", _env_admin, _admin_ok)
else:
    logging.error("[STARTUP] ADMIN_TELEGRAM_ID غير مضبوط في البيئة!")

# ── حالات المحادثة ───────────────────────────────────────────
ASK_NAME, ASK_PRICE, ASK_SIZES, CONFIRM_ADD, ASK_DEL_CODE = range(5)

# ── تسميات المدد للعرض ───────────────────────────────────────
PLAN_LABELS = {
    "biweekly": "أسبوعان",
    "monthly":  "شهر",
    "yearly":   "سنة",
}

# ── أوضاع الاختبار (حصرية متبادلة) ─────────────────────────
TEST_SHOP     = "shop"      # /testclient  — يحاكي صاحب المحل
TEST_CUSTOMER = "customer"  # /testcustomer — يحاكي الزبون

# ── أنماط رسائل الزبون ──────────────────────────────────────
_RE_GREETING = re.compile(
    r"^(سلام|مرحبا|مرحباً|هاي|أهلا|أهلاً|hello|hi)\b", re.IGNORECASE
)
_RE_PRODUCT  = re.compile(r"\b([A-Z]{2}-[A-Z0-9]{4})\b")
_RE_PHONE    = re.compile(r"07[3-9]\d{8}|\+9647[3-9]\d{8}")

# ── لوحات المفاتيح ───────────────────────────────────────────
ADMIN_KB = ReplyKeyboardMarkup(
    [["📊 المشتركون", "📈 إحصاءات المنصّة"]],
    resize_keyboard=True,
)
OWNER_KB = ReplyKeyboardMarkup(
    [["➕ إضافة سلعة"], ["📋 عرض السلع", "🗑 حذف سلعة"],
     ["🔔 الإشعارات", "🔗 ربط إنستغرام"]],
    resize_keyboard=True,
)


# ────────────────────────────────────────────────────────────
# مساعدات عامة
# ────────────────────────────────────────────────────────────
def _eff_uid(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    """يُعيد المعرّف الوهمي في وضع محل الاختبار (من قاعدة البيانات)"""
    real_uid = update.effective_user.id
    state = db.get_admin_mode(real_uid)
    if state["mode"] == TEST_SHOP:
        return state["test_shop_id"]
    return real_uid


def _clear_conv(context: ContextTypes.DEFAULT_TYPE) -> None:
    """احذف مفاتيح المحادثة المؤقتة (اسم/سعر/قياسات)"""
    for key in ("name", "price", "sizes"):
        context.user_data.pop(key, None)


def _exit_test_mode(uid: int) -> None:
    """أنهِ وضع الاختبار في قاعدة البيانات"""
    db.clear_admin_mode(uid)


def can_manage(uid: int) -> bool:
    """محل نشط وساري الاشتراك فقط"""
    shop = db.get_shop(uid)
    if shop is None:
        return False
    return db.is_subscription_active(uid)


async def _deny_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = _eff_uid(update, context)
    shop = db.get_shop(uid)
    if shop is None:
        await update.message.reply_text("غير مصرّح.")
    elif shop["status"] == "pending":
        await update.message.reply_text("أرسل كود التفعيل أولاً.")
    elif not db.is_subscription_active(uid):
        await update.message.reply_text("انتهى اشتراكك ⏳ — تواصل مع الإدارة للتجديد.")
    else:
        await update.message.reply_text("غير مصرّح.")


def _duration_kb(shop_id: int) -> InlineKeyboardMarkup:
    """أزرار اختيار المدة مضمّنة"""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("أسبوعان", callback_data=f"dur_biweekly_{shop_id}"),
        InlineKeyboardButton("شهر",    callback_data=f"dur_monthly_{shop_id}"),
        InlineKeyboardButton("سنة",    callback_data=f"dur_yearly_{shop_id}"),
    ]])


async def _register_new_shop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    display_name,
    is_test: bool,
) -> None:
    """سجّل محلاً جديداً (pending) وأرسل الترحيب وإشعار الأدمن"""
    db.add_shop(uid, display_name)
    kb = ReplyKeyboardRemove() if is_test else None
    await update.message.reply_text(
        "أهلاً بك في المنصّة.\n"
        "لتفعيل حسابك أرسل كود التفعيل الذي ستحصل عليه من الإدارة.",
        reply_markup=kb,
    )
    admin_id = db.get_admin_id()
    if admin_id:
        label = "[اختبار] " if is_test else ""
        await context.bot.send_message(
            admin_id,
            f"🏪 {label}محل جديد سجّل\n"
            f"المعرّف: {uid}\n"
            f"اليوزر: @{display_name or 'بدون يوزر'}",
            reply_markup=_duration_kb(uid),
        )


# ────────────────────────────────────────────────────────────
# /whoami
# ────────────────────────────────────────────────────────────
async def whoami(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    real_uid  = update.effective_chat.id
    _is_admin = db.is_admin(real_uid)
    state     = db.get_admin_mode(real_uid)
    mode      = state["mode"] or ""
    mode_label = {"shop": "محل 🏪", "customer": "زبون 🛍"}.get(mode, "غير نشط")
    await update.message.reply_text(
        f"🆔 معرّفك: {real_uid}\n"
        f"👑 أدمن: {'نعم ✅' if _is_admin else 'لا ❌'}\n"
        f"🧪 وضع الاختبار: {mode_label}"
    )


# ────────────────────────────────────────────────────────────
# أوضاع الاختبار
# ────────────────────────────────────────────────────────────
async def testclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testclient — وضع محاكاة صاحب المحل"""
    uid = update.effective_chat.id
    if not db.is_admin(uid):
        return
    test_id  = -uid
    username = update.effective_user.username
    _exit_test_mode(uid)
    db.set_admin_mode(uid, TEST_SHOP, test_id)
    shop = db.get_shop(test_id)
    if shop is None:
        display_name = f"test_{username or uid}"
        await _register_new_shop(update, context, test_id, display_name, is_test=True)
    else:
        await update.message.reply_text(
            f"🏪 وضع محل الاختبار نشط (المعرّف: {test_id})\n"
            f"الحالة: {shop['status']} — استعمل /exittest للخروج.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def testcustomer(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """/testcustomer — وضع محاكاة الزبون"""
    uid = update.effective_chat.id
    if not db.is_admin(uid):
        return
    _exit_test_mode(uid)
    db.set_admin_mode(uid, TEST_CUSTOMER, -uid)
    await update.message.reply_text(
        f"🛍 وضع محاكاة الزبون نشط — رسائلك تصل للمحل الوهمي ({-uid})\n"
        "استعمل /exittest للخروج.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def exittest(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """/exittest — خروج من أي وضع اختبار بلا حذف بيانات"""
    if not db.is_admin(update.effective_chat.id):
        return
    _exit_test_mode(update.effective_chat.id)
    await update.message.reply_text("خرجت من وضع الاختبار.", reply_markup=ADMIN_KB)


async def deleteinfo(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """/deleteinfo — حذف يدوي لكل بيانات محل الاختبار"""
    uid = update.effective_chat.id
    if not db.is_admin(uid):
        return
    db.clear_test_shop(-uid)  # يحذف admin_state أيضاً داخلياً
    await update.message.reply_text("🧹 حُذفت بيانات الاختبار.")


async def ig_connect(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """🔗 ربط إنستغرام — يولّد رابط OAuth ويرسله لصاحب المحل"""
    real_uid = _eff_uid(update, _context)
    if not can_manage(real_uid):
        await _deny_pending(update, _context)
        return
    if not IG_APP_ID or not IG_REDIRECT_URI:
        await update.message.reply_text(
            "❌ ربط إنستغرام غير متاح حالياً — تواصل مع الإدارة لإعداد التطبيق."
        )
        return
    s = URLSafeTimedSerializer(STATE_SECRET_KEY)
    signed_state = s.dumps(real_uid, salt="ig-oauth")
    params = urllib.parse.urlencode({
        "client_id":     IG_APP_ID,
        "redirect_uri":  IG_REDIRECT_URI,
        "response_type": "code",
        "scope":         "instagram_business_basic,instagram_business_manage_messages",
        "state":         signed_state,
    })
    oauth_url = f"https://www.instagram.com/oauth/authorize?{params}"
    await update.message.reply_text(
        f"🔗 لربط حساب إنستغرام افتح الرابط التالي (صالح 15 دقيقة):\n\n{oauth_url}"
    )


# ────────────────────────────────────────────────────────────
# /start
# ────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    real_uid     = update.effective_chat.id
    username     = update.effective_user.username
    state        = db.get_admin_mode(real_uid)
    in_shop_test = state["mode"] == TEST_SHOP

    # أدمن خارج وضع محل الاختبار → كيبورد الأدمن
    if db.is_admin(real_uid) and not in_shop_test:
        await update.message.reply_text(
            "مرحباً أيها الأدمن.\n"
            "تصلك هنا إشعارات تسجيل المحلات.\n"
            "استعمل /testclient لتجربة واجهة المحل.",
            reply_markup=ADMIN_KB,
        )
        return

    uid  = _eff_uid(update, context)
    shop = db.get_shop(uid)

    if shop is None:
        display_name = f"test_{username or real_uid}" if in_shop_test else username
        await _register_new_shop(update, context, uid, display_name, in_shop_test)
        return

    if shop["status"] == "active":
        await update.message.reply_text("مرحباً 👋", reply_markup=OWNER_KB)
    else:
        await update.message.reply_text(
            "حسابك قيد الانتظار.\n"
            "أرسل كود التفعيل الذي ستحصل عليه من الإدارة."
        )


# ────────────────────────────────────────────────────────────
# Callback: تدفّق التفعيل (dur_ / gen_ / back_)
# ────────────────────────────────────────────────────────────
async def handle_activation_cb(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_admin(query.from_user.id):
        return
    data = query.data

    if data.startswith("dur_"):
        _, plan, shop_id_str = data.split("_", 2)
        shop_id  = int(shop_id_str)
        shop     = db.get_shop(shop_id)
        username = (shop["username"] if shop else None) or "بدون يوزر"
        plan_ar  = PLAN_LABELS.get(plan, plan)
        await query.edit_message_text(
            f"المحل: @{username} ({shop_id})\nالمدة: {plan_ar}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ توليد الكود", callback_data=f"gen_{plan}_{shop_id}"),
                InlineKeyboardButton("↩️ رجوع",       callback_data=f"back_{shop_id}"),
            ]]),
        )

    elif data.startswith("gen_"):
        _, plan, shop_id_str = data.split("_", 2)
        shop_id  = int(shop_id_str)
        shop     = db.get_shop(shop_id)
        username = (shop["username"] if shop else None) or "بدون يوزر"
        code     = db.create_activation_code(shop_id, plan)
        await query.edit_message_text(
            f"✅ كود التفعيل للمحل @{username} ({shop_id}):\n\n"
            f"{code}\n\nأرسل هذا الكود لصاحب المحل.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 إعادة التوليد بمدة أخرى", callback_data=f"back_{shop_id}"),
            ]]),
        )

    elif data.startswith("back_"):
        shop_id  = int(data[5:])
        shop     = db.get_shop(shop_id)
        username = (shop["username"] if shop else None) or "بدون يوزر"
        label    = "[اختبار] " if shop_id < 0 else ""
        await query.edit_message_text(
            f"🏪 {label}محل جديد سجّل\nالمعرّف: {shop_id}\nاليوزر: @{username}",
            reply_markup=_duration_kb(shop_id),
        )


# ────────────────────────────────────────────────────────────
# Callback: تدفّق التجديد (renew_ / rnwdur_)
# ────────────────────────────────────────────────────────────
async def handle_renew_cb(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_admin(query.from_user.id):
        return
    data = query.data

    if data.startswith("renew_"):
        shop_id  = int(data[6:])
        shop     = db.get_shop(shop_id)
        username = (shop["username"] if shop else None) or "بدون يوزر"
        await query.edit_message_text(
            f"🔄 تجديد: @{username} ({shop_id})\nاختر المدة:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("أسبوعان", callback_data=f"rnwdur_biweekly_{shop_id}"),
                InlineKeyboardButton("شهر",    callback_data=f"rnwdur_monthly_{shop_id}"),
                InlineKeyboardButton("سنة",    callback_data=f"rnwdur_yearly_{shop_id}"),
            ]]),
        )

    elif data.startswith("rnwdur_"):
        _, plan, shop_id_str = data.split("_", 2)
        shop_id  = int(shop_id_str)
        shop     = db.get_shop(shop_id)
        username = (shop["username"] if shop else None) or "بدون يوزر"
        plan_ar  = PLAN_LABELS.get(plan, plan)
        code     = db.create_activation_code(shop_id, plan)
        await query.edit_message_text(
            f"✅ كود تجديد @{username} ({shop_id}) — {plan_ar}:\n\n"
            f"{code}\n\nأرسل هذا الكود للمحل ليجدّد به.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 مدة أخرى", callback_data=f"renew_{shop_id}"),
            ]]),
        )


# ────────────────────────────────────────────────────────────
# Callback: قبول الطلب (accept_)
# ────────────────────────────────────────────────────────────
async def handle_accept_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    order_id = int(query.data.split("_")[1])
    order    = db.get_order(order_id)
    if order is None:
        await query.answer("الطلب غير موجود.", show_alert=True)
        return
    presser = query.from_user.id
    # تحقق أن الضاغط صاحب المحل أو الأدمن
    if presser != abs(order["shop_id"]) and not db.is_admin(presser):
        return
    if order["status"] == "accepted":
        await query.answer("الطلب مقبول مسبقاً.", show_alert=True)
        return
    db.mark_order_accepted(order_id)
    # عدّل رسالة صاحب المحل وأزل الزر
    try:
        await query.edit_message_text(query.message.text + "\n\n✅ تم قبول الطلب")
    except Exception:
        pass
    # أبلغ الزبون
    customer_chat = order.get("customer_chat_id")
    if customer_chat:
        try:
            await context.bot.send_message(
                customer_chat,
                "تمت رؤية طلبك من قبل المحل ✅ وسيتم التواصل معك قريباً."
            )
        except Exception:
            pass


# ────────────────────────────────────────────────────────────
# كود التفعيل من المحل
# ────────────────────────────────────────────────────────────
async def handle_activation_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = _eff_uid(update, context)
    shop = db.get_shop(uid)
    if shop is None or shop["status"] not in ("pending", "expired"):
        await update.message.reply_text(f"أنت كتبت: {update.message.text}")
        return
    code = update.message.text.strip().upper()
    plan = db.redeem_activation_code(code, uid)
    if plan is None:
        await update.message.reply_text("❌ كود غير صالح.")
        return
    shop    = db.get_shop(uid)
    plan_ar = PLAN_LABELS.get(plan, plan)
    await update.message.reply_text(
        f"✅ تم تفعيل اشتراكك ({plan_ar} — ينتهي {shop['end_date']})",
        reply_markup=OWNER_KB,
    )


# ────────────────────────────────────────────────────────────
# إشعارات صاحب المحل
# ────────────────────────────────────────────────────────────
async def show_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _eff_uid(update, context)
    if not can_manage(uid):
        await _deny_pending(update, context)
        return
    notifs = db.get_shop_notifications(uid)
    if not notifs:
        await update.message.reply_text("لا توجد إشعارات بعد.", reply_markup=OWNER_KB)
        return
    icon = {"order": "🛒", "inquiry": "❓"}
    lines = [
        f"{icon.get(n['kind'], '📌')} {n['content']}\n🕐 {n['created_at']}"
        for n in notifs
    ]
    # تقسيم الرسالة إن تجاوزت حد تيليجرام
    chunk, chunks = [], []
    for line in lines:
        if sum(len(l) for l in chunk) + len(line) > 3800:
            chunks.append(chunk)
            chunk = []
        chunk.append(line)
    if chunk:
        chunks.append(chunk)
    for i, ch in enumerate(chunks):
        kb = OWNER_KB if i == len(chunks) - 1 else None
        await update.message.reply_text("\n\n".join(ch), reply_markup=kb)


# ────────────────────────────────────────────────────────────
# لوحة الأدمن
# ────────────────────────────────────────────────────────────
async def show_subscribers(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_chat.id
    state = db.get_admin_mode(uid)
    if not db.is_admin(uid) or state["mode"]:
        await update.message.reply_text(f"أنت كتبت: {update.message.text}")
        return
    shops = db.get_all_shops()
    if not shops:
        await update.message.reply_text("لا توجد محلات مسجّلة بعد.", reply_markup=ADMIN_KB)
        return
    MAX_SHOPS = 20
    shown     = shops[:MAX_SHOPS]
    today     = _date.today().isoformat()
    await update.message.reply_text(
        f"📊 المشتركون ({len(shops)} محل — يُعرض {len(shown)}):",
        reply_markup=ADMIN_KB,
    )
    for s in shown:
        end        = s["end_date"] or ""
        is_expired = s["status"] == "expired" or (
            s["status"] == "active" and end and end < today
        )
        badge    = "❌ منتهٍ" if is_expired else ("✅ نشط" if s["status"] == "active" else "⏳ منتظر")
        plan_ar  = PLAN_LABELS.get(s["plan"] or "", s["plan"] or "—")
        username = s["username"] or "بدون يوزر"
        text = (
            f"{badge} @{username} ({s['telegram_id']})\n"
            f"الخطة: {plan_ar}  |  ينتهي: {end or '—'}  |  رسائل: {s['message_count']}"
        )
        kb = None if s["status"] == "pending" else InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 تجديد", callback_data=f"renew_{s['telegram_id']}")
        ]])
        await update.message.reply_text(text, reply_markup=kb)


async def show_stats(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_chat.id
    state = db.get_admin_mode(uid)
    if not db.is_admin(uid) or state["mode"]:
        await update.message.reply_text(f"أنت كتبت: {update.message.text}")
        return
    s = db.get_platform_stats()
    await update.message.reply_text(
        f"📈 إحصاءات المنصّة\n\n"
        f"✅ نشطة:      {s['active']}\n"
        f"❌ منتهية:    {s['expired']}\n"
        f"⏳ منتظرة:    {s['pending']}\n"
        f"👥 الإجمالي:  {s['total']}\n\n"
        f"📦 إجمالي السلع: {s['products']}",
        reply_markup=ADMIN_KB,
    )


# ────────────────────────────────────────────────────────────
# عرض السلع
# ────────────────────────────────────────────────────────────
async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _eff_uid(update, context)
    if not can_manage(uid):
        await _deny_pending(update, context)
        return
    products = db.get_shop_products(uid)
    if not products:
        await update.message.reply_text("لا توجد سلع مسجّلة بعد.", reply_markup=OWNER_KB)
        return
    lines = []
    for p in products:
        sizes = ", ".join(p["sizes"])
        lines.append(f"🏷 {p['code']}\n📦 {p['name']}\n💰 {p['price']}\n📐 {sizes}")
    await update.message.reply_text("\n\n".join(lines), reply_markup=OWNER_KB)


# ────────────────────────────────────────────────────────────
# إضافة سلعة — ConversationHandler
# ────────────────────────────────────────────────────────────
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _eff_uid(update, context)
    if not can_manage(uid):
        await _deny_pending(update, context)
        return ConversationHandler.END
    await update.message.reply_text("اسم السلعة:", reply_markup=ReplyKeyboardRemove())
    return ASK_NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("السعر (رقم فقط):")
    return ASK_PRICE


async def got_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        price = float(text)
    except ValueError:
        await update.message.reply_text("❌ السعر يجب أن يكون رقماً. أعد الإدخال:")
        return ASK_PRICE
    context.user_data["price"] = price
    await update.message.reply_text("القياسات مفصولة بفاصلة (مثال: S,M,L,XL):")
    return ASK_SIZES


async def got_sizes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sizes = [s.strip() for s in update.message.text.split(",") if s.strip()]
    context.user_data["sizes"] = sizes
    name  = context.user_data["name"]
    price = context.user_data["price"]
    summary = (
        f"📋 ملخص السلعة:\n"
        f"📦 الاسم: {name}\n"
        f"💰 السعر: {price}\n"
        f"📐 القياسات: {', '.join(sizes)}\n\n"
        "تأكيد الحفظ؟"
    )
    confirm_kb = ReplyKeyboardMarkup(
        [["✅ نعم", "❌ لا"]], resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(summary, reply_markup=confirm_kb)
    return CONFIRM_ADD


async def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "نعم" not in update.message.text.strip():
        await update.message.reply_text("❌ تم الإلغاء.", reply_markup=OWNER_KB)
        _clear_conv(context)
        return ConversationHandler.END
    uid  = _eff_uid(update, context)
    code = db.generate_unique_code()
    db.add_product(
        code, uid,
        context.user_data["name"],
        context.user_data["price"],
        context.user_data["sizes"],
    )
    _clear_conv(context)
    await update.message.reply_text(
        f"تمت الإضافة ✅ — ضع هذا الكود في آخر كابشن منشور السلعة على إنستغرام: {code}",
        reply_markup=OWNER_KB,
    )
    return ConversationHandler.END


# ────────────────────────────────────────────────────────────
# حذف سلعة — ConversationHandler
# ────────────────────────────────────────────────────────────
async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _eff_uid(update, context)
    if not can_manage(uid):
        await _deny_pending(update, context)
        return ConversationHandler.END
    await update.message.reply_text("أرسل كود السلعة المراد حذفها:", reply_markup=ReplyKeyboardRemove())
    return ASK_DEL_CODE


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = _eff_uid(update, context)
    code    = update.message.text.strip().upper()
    deleted = db.delete_product(code, uid)
    if not deleted:
        await update.message.reply_text("❌ الكود غير موجود أو لا يخصّك.", reply_markup=OWNER_KB)
        return ConversationHandler.END
    await update.message.reply_text(f"تم حذف السلعة {code} ✅", reply_markup=OWNER_KB)
    return ConversationHandler.END


# ────────────────────────────────────────────────────────────
# /cancel
# ────────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_conv(context)
    await update.message.reply_text("تم الإلغاء.", reply_markup=OWNER_KB)
    return ConversationHandler.END


# ────────────────────────────────────────────────────────────
# echo (زبائن/محلات غير مفعّلة)
# ────────────────────────────────────────────────────────────
async def echo(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"أنت كتبت: {update.message.text}")


# ────────────────────────────────────────────────────────────
# منطق الزبون — دوال مستقلة عن مصدر الرسالة
# ────────────────────────────────────────────────────────────
def _parse_order(text: str):
    """استخرج الاسم والهاتف والعنوان من نص الطلب"""
    phone_m = _RE_PHONE.search(text)
    phone   = phone_m.group(0) if phone_m else ""
    if "/" in text:
        parts   = [p.strip() for p in text.split("/")]
        name    = ""
        address = ""
        for part in parts:
            if _RE_PHONE.search(part):
                continue
            if not name:
                name = part
            else:
                address = (address + " " + part).strip()
    else:
        rest    = text.replace(phone, "").strip(" ,-/")
        name    = rest
        address = ""
    return name, phone, address


async def _cust_greet(update: Update, _context: ContextTypes.DEFAULT_TYPE, _shop_id: int) -> None:
    await update.message.reply_text(
        "أهلاً وسهلاً 👋\nأرسل كود السلعة التي تريد الاستفسار عنها."
    )


async def _cust_product(
    update: Update, _context: ContextTypes.DEFAULT_TYPE, code: str, shop_id: int
) -> None:
    product = db.get_product(code)
    if product is None or product["shop_id"] != shop_id:
        await update.message.reply_text("لم أجد هذا الكود، تأكّد منه.")
        return
    # احفظ آخر سلعة في قاعدة البيانات بدل الذاكرة المؤقتة
    db.set_admin_last_product(update.effective_chat.id, code)
    sizes = ", ".join(product["sizes"])
    await update.message.reply_text(
        f"📦 {product['name']}\n"
        f"💰 السعر: {product['price']}\n"
        f"📐 القياسات: {sizes}\n"
        f"📌 الحالة: متوفر"
    )
    await update.message.reply_text(
        "لو حابب تطلب، أرسل:\nالاسم / رقم الهاتف / العنوان"
    )


async def _cust_order(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, shop_id: int
) -> None:
    name, phone, address = _parse_order(text)
    real_uid         = update.effective_chat.id
    state            = db.get_admin_mode(real_uid)
    product_code     = state.get("last_product") or ""
    customer_chat_id = real_uid
    order_id = db.add_order(shop_id, product_code, name, phone, address, customer_chat_id)
    # حفظ إشعار دائم في قاعدة البيانات
    notif_content = (
        f"الاسم: {name or '—'} | الهاتف: {phone or '—'} | "
        f"العنوان: {address or '—'} | السلعة: {product_code or '—'}"
    )
    db.add_notification(shop_id, "order", notif_content, customer_chat_id)
    # إشعار فوري لصاحب المحل مع زر القبول
    real_chat = abs(shop_id)
    accept_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept_{order_id}")
    ]])
    try:
        await context.bot.send_message(
            real_chat,
            f"🛒 طلب جديد\n"
            f"السلعة: {product_code or '—'}\n"
            f"الاسم: {name or '—'}\n"
            f"الهاتف: {phone or '—'}\n"
            f"العنوان: {address or '—'}",
            reply_markup=accept_kb,
        )
    except Exception as e:
        logging.error("[_cust_order] فشل إرسال إشعار المحل %s: %s", real_chat, e)
    # إشعار فوري للأدمن
    admin_id = db.get_admin_id()
    if admin_id:
        shop  = db.get_shop(shop_id)
        uname = (shop["username"] if shop else None) or str(shop_id)
        try:
            await context.bot.send_message(
                admin_id,
                f"📩 محل @{uname} ({shop_id}) تلقّى طلباً جديداً من زبون."
            )
        except Exception as e:
            logging.error("[_cust_order] فشل إرسال إشعار الأدمن: %s", e)
    await update.message.reply_text("تم استلام طلبك ✅ سيتواصل معك المحل قريباً.")


async def _cust_inquiry(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, shop_id: int
) -> None:
    real_chat   = abs(shop_id)
    customer_id = update.effective_chat.id
    # حفظ إشعار دائم في قاعدة البيانات
    db.add_notification(shop_id, "inquiry", text, customer_id)
    # إشعار فوري لصاحب المحل
    try:
        await context.bot.send_message(
            real_chat,
            f"❓ استفسار من زبون\n{text}\nالمعرّف: {customer_id}"
        )
    except Exception as e:
        logging.error("[_cust_inquiry] فشل إرسال الاستفسار للمحل %s: %s", real_chat, e)
    await update.message.reply_text("تم إرسال سؤالك للمحل.")


async def handle_customer_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """نقطة دخول موحّدة لرسائل الزبون — تقرأ الحالة من قاعدة البيانات"""
    real_uid = update.effective_chat.id
    state    = db.get_admin_mode(real_uid)
    shop_id  = state["test_shop_id"]
    if not shop_id:
        return
    text    = update.message.text.strip()
    text_up = text.upper()
    db.increment_message_count(shop_id)
    if _RE_GREETING.match(text):
        await _cust_greet(update, context, shop_id)
    elif m := _RE_PRODUCT.search(text_up):
        await _cust_product(update, context, m.group(1), shop_id)
    elif _RE_PHONE.search(text):
        await _cust_order(update, context, text, shop_id)
    else:
        await _cust_inquiry(update, context, text, shop_id)


async def _customer_interceptor(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """اعترض رسائل وضع الزبون — يتحقق من قاعدة البيانات لا الذاكرة"""
    real_uid = update.effective_chat.id
    state    = db.get_admin_mode(real_uid)
    if state["mode"] != TEST_CUSTOMER:
        return
    await handle_customer_message(update, context)
    raise ApplicationHandlerStop


# ────────────────────────────────────────────────────────────
# المهام الدورية (JobQueue)
# ────────────────────────────────────────────────────────────
async def job_expire_shops(context: ContextTypes.DEFAULT_TYPE) -> None:
    expired_ids = db.expire_overdue_shops()
    if not expired_ids:
        return
    for shop_id in expired_ids:
        try:
            await context.bot.send_message(
                shop_id, "انتهى اشتراكك ⏳ — تواصل مع الإدارة للتجديد."
            )
        except Exception:
            pass
    admin_id = db.get_admin_id()
    if not admin_id:
        return
    lines = []
    for shop_id in expired_ids:
        shop  = db.get_shop(shop_id)
        uname = (shop["username"] if shop else None) or "بدون يوزر"
        lines.append(f"@{uname} ({shop_id})")
    await context.bot.send_message(
        admin_id, f"🔴 أُقفل {len(expired_ids)} محل اليوم:\n" + "\n".join(lines)
    )


async def job_expiring_soon(context: ContextTypes.DEFAULT_TYPE) -> None:
    today = _date.today().isoformat()
    if context.bot_data.get("expiring_notified") == today:
        return
    admin_id = db.get_admin_id()
    if not admin_id:
        return
    shops = db.get_expiring_soon(3)
    if not shops:
        context.bot_data["expiring_notified"] = today
        return
    lines = [
        f"@{s['username'] or 'بدون يوزر'} ({s['telegram_id']}) — ينتهي {s['end_date']}"
        for s in shops
    ]
    await context.bot.send_message(
        admin_id,
        f"⚠️ {len(shops)} محل ينتهي اشتراكه خلال 3 أيام:\n\n" + "\n".join(lines)
    )
    context.bot_data["expiring_notified"] = today


async def job_refresh_ig_tokens(_context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh Instagram tokens expiring within the next 7 days."""
    threshold = int(time.time()) + 7 * 86400
    shops = db.list_ig_shops_expiring_before(threshold)
    for shop in shops:
        wid = shop["webhook_account_id"]
        try:
            r = requests.get(
                "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": shop["access_token"]},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                new_token   = data["access_token"]
                expires_in  = data.get("expires_in", 5_184_000)
                db.update_ig_shop_token(wid, new_token, int(time.time()) + expires_in)
                logging.warning("[IG-REFRESH] ✅ Token refreshed for shop %s", wid)
            else:
                logging.error("[IG-REFRESH] ❌ Failed for %s: %s %s",
                              wid, r.status_code, r.text[:200])
        except Exception as e:
            logging.error("[IG-REFRESH] ❌ Exception for %s: %s", wid, e)


async def _post_init(application) -> None:
    jq = application.job_queue
    jq.run_daily(job_expire_shops,      _time(0, 5))
    jq.run_daily(job_expiring_soon,     _time(0, 10))
    jq.run_daily(job_refresh_ig_tokens, _time(3, 0))


# ────────────────────────────────────────────────────────────
# تجميع البوت
# ────────────────────────────────────────────────────────────
# PicklePersistence للـ ConversationHandler (اسم/سعر/قياسات) فقط
persistence = PicklePersistence(filepath="bot_persistence")
app = ApplicationBuilder().token(TOKEN).persistence(persistence).post_init(_post_init).build()

add_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex(r"^➕ إضافة سلعة$"), add_start)],
    states={
        ASK_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
        ASK_PRICE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_price)],
        ASK_SIZES:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_sizes)],
        CONFIRM_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_save)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

del_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex(r"^🗑 حذف سلعة$"), delete_start)],
    states={
        ASK_DEL_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delete)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

# ── المجموعة -1: اعتراض وضع الزبون قبل أي معالج آخر ─────────
app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, _customer_interceptor),
    group=-1,
)

# ── المجموعة 0: المعالجات العادية ───────────────────────────
app.add_handler(CommandHandler("start",        start))
app.add_handler(CommandHandler("list",         list_products))
app.add_handler(CommandHandler("testclient",   testclient))
app.add_handler(CommandHandler("testcustomer", testcustomer))
app.add_handler(CommandHandler("exittest",     exittest))
app.add_handler(CommandHandler("deleteinfo",   deleteinfo))
app.add_handler(CommandHandler("whoami",       whoami))
# callbacks التفعيل: dur_ / gen_ / back_
app.add_handler(CallbackQueryHandler(handle_activation_cb, pattern=r"^(dur|gen|back)_"))
# callbacks التجديد: renew_ / rnwdur_
app.add_handler(CallbackQueryHandler(handle_renew_cb,      pattern=r"^(renew|rnwdur)_"))
# callback قبول الطلب
app.add_handler(CallbackQueryHandler(handle_accept_cb,     pattern=r"^accept_\d+$"))
app.add_handler(add_conv)
app.add_handler(del_conv)
app.add_handler(MessageHandler(filters.Regex(r"^📋 عرض السلع$"),             list_products))
app.add_handler(MessageHandler(filters.Regex(r"^🔔 الإشعارات$"),        show_notifications))
app.add_handler(MessageHandler(filters.Regex(r"^🔗 ربط إنستغرام$"),       ig_connect))
app.add_handler(MessageHandler(filters.Regex(r"^📊 المشتركون$"),       show_subscribers))
app.add_handler(MessageHandler(filters.Regex(r"^📈 إحصاءات المنصّة$"), show_stats))
# كود التفعيل يُعالَج قبل echo
app.add_handler(MessageHandler(filters.Regex(r"^ACT-[A-Z0-9]{5}$"),    handle_activation_code))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,         echo))

# ────────────────────────────────────────────────────────────
# Instagram auto-reply — DB-backed multi-tenant sending
# ────────────────────────────────────────────────────────────
def _send_instagram_message_raw(
    send_account_id: str, access_token: str, recipient_id: str, text: str
) -> bool:
    """Send a message via the Instagram API with explicit account/token params."""
    url = f"https://graph.instagram.com/v25.0/{send_account_id}/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message":   {"text": text},
        "access_token": access_token,
    }
    try:
        r = requests.post(url, json=payload, headers={"Content-Type": "application/json"},
                          timeout=10)
        if r.status_code == 200:
            logging.warning("[IG-SEND] ✅ رسالة أُرسلت إلى %s", recipient_id)
            return True
        logging.error("[IG-SEND] ❌ فشل (%s): %s", r.status_code, r.text[:300])
        return False
    except Exception as e:
        logging.error("[IG-SEND] ❌ استثناء: %s", e)
        return False


def send_telegram_message_http(chat_id: int, text: str) -> bool:
    """إرسال رسالة تيليجرام عبر HTTP مباشرة — يعمل من خيط Flask."""
    if not TOKEN:
        logging.error("[TG-HTTP] TOKEN غير مضبوط")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            logging.warning("[TG-HTTP] ✅ إشعار أُرسل لـ %s", chat_id)
            return True
        logging.error("[TG-HTTP] ❌ فشل (%s): %s", r.status_code, r.text[:300])
        return False
    except Exception as e:
        logging.error("[TG-HTTP] ❌ استثناء: %s", e)
        return False


# تتبّع الزبائن الذين طلبوا التحدث مع إنسان لإسكات البوت لمدة 24 ساعة
# المفتاح: sender_id (str)، القيمة: timestamp عند الطلب (int)
_HUMAN_HANDOFF_TS: dict = {}
_HUMAN_HANDOFF_DURATION = 24 * 60 * 60  # 24 ساعة بالثواني


def handle_instagram_message(sender_id: str, recipient_id: str, text: str) -> None:
    """نقطة دخول رسائل إنستغرام — تطبّق منطق الزبون وترد عبر إنستغرام."""
    # البحث عن المحل في DB بواسطة webhook_account_id
    shop = db.get_ig_shop_by_webhook_id(recipient_id)
    if shop is None:
        # self-healing: قد يختلف webhook_account_id عن send_account_id
        shop = db.get_ig_shop_by_send_account_id(recipient_id)
        if shop is not None:
            logging.warning(
                "[IG-LOOKUP] self-heal: تحديث webhook_id من %s إلى %s",
                shop["webhook_account_id"], recipient_id
            )
            db.update_ig_shop_webhook_id(shop["webhook_account_id"], recipient_id)
        else:
            logging.warning("[IG] رسالة لمستلم غير معروف: %s", recipient_id)
            return
    send_account_id = shop["send_account_id"]
    ig_token        = shop["access_token"]
    shop_id         = shop["owner_telegram_id"]  # قد يكون سالباً في بيئة الاختبار

    # فحص: هل هذا الزبون في وضع الإسكات (طلب التحدث مع إنسان خلال 24 ساعة الماضية)؟
    _now_ts = int(time.time())
    _handoff_ts = _HUMAN_HANDOFF_TS.get(sender_id)
    if _handoff_ts is not None:
        _elapsed = _now_ts - _handoff_ts
        if _elapsed < _HUMAN_HANDOFF_DURATION:
            _remaining_hrs = (_HUMAN_HANDOFF_DURATION - _elapsed) // 3600
            logging.warning(
                "[IG-SILENCE] تجاهل رسالة من %s (تبقى %s ساعة على انتهاء الإسكات): %s",
                sender_id, _remaining_hrs, text.strip()[:100]
            )
            db.add_notification(
                shop_id,
                "inquiry",
                f"📩 [أثناء الإسكات — الزبون طلب التحدث مع إنسان] {text.strip()}",
                None
            )
            send_telegram_message_http(
                abs(shop_id),
                f"📩 رسالة جديدة من زبون في وضع التحدث مع إنسان:\n{text.strip()}\n"
                f"معرّف الزبون (IG): {sender_id}"
            )
            return
        else:
            _HUMAN_HANDOFF_TS.pop(sender_id, None)
            logging.warning("[IG-SILENCE] انتهت مدة الإسكات للزبون %s، عودة للرد التلقائي", sender_id)

    text_stripped = text.strip()
    text_up = text_stripped.upper()

    # تحية
    if _RE_GREETING.match(text_stripped):
        _send_instagram_message_raw(send_account_id, ig_token, sender_id,
                                    "أهلاً وسهلاً 👋\nأرسل كود السلعة التي تريد الاستفسار عنها.")
        return

    # كود سلعة
    m = _RE_PRODUCT.search(text_up)
    if m:
        code = m.group(1)
        logging.warning("code=%s", code)

        product = db.get_product(code)
        logging.warning("product=%s", product)

        logging.warning("current_shop=%s", shop_id)

        if product:
            logging.warning("product_shop=%s", product["shop_id"])

        if product is None or product["shop_id"] != shop_id:
            _send_instagram_message_raw(
                send_account_id,
                ig_token,
                sender_id,
                "لم أجد هذا الكود، تأكد منه."
            )
            return

        # احفظ آخر سلعة
        try:
            fake_uid = -int(sender_id) if sender_id.isdigit() else None
        except Exception:
            fake_uid = None

        if fake_uid is not None:
            db.set_admin_last_product(fake_uid, code)

        sizes = ", ".join(product["sizes"])
        _send_instagram_message_raw(
            send_account_id,
            ig_token,
            sender_id,
            f"📦 {product['name']}\n"
            f"💰 السعر: {product['price']}\n"
            f"📐 القياسات: {sizes}\n"
            f"📌 الحالة: متوفر"
        )

        _send_instagram_message_raw(
            send_account_id,
            ig_token,
            sender_id,
            "لو حابب تطلب، أرسل:\nالاسم / رقم الهاتف / العنوان"
        )
        return

    # رقم هاتف = طلب
    if _RE_PHONE.search(text_stripped):
        name, phone, address = _parse_order(text_stripped)
        product_code = ""
        try:
            fake_uid = -int(sender_id) if sender_id.isdigit() else None
            if fake_uid is not None:
                st = db.get_admin_mode(fake_uid)
                product_code = st.get("last_product") or ""
        except Exception:
            pass
        order_id = db.add_order(shop_id, product_code, name, phone, address, None)
        notif = (
            f"الاسم: {name or '—'} | الهاتف: {phone or '—'} | "
            f"العنوان: {address or '—'} | السلعة: {product_code or '—'} | "
            f"المصدر: إنستغرام ({sender_id})"
        )
        logging.warning("[IG-NOTIF] أحفظ إشعار لـ shop_id=%s kind=order", shop_id)
        db.add_notification(shop_id, "order", notif, None)
        logging.warning("[IG-NOTIF] ✅ حُفظ الإشعار")
        send_telegram_message_http(
            abs(shop_id),
            f"🛒 طلب جديد من إنستغرام\n"
            f"السلعة: {product_code or '—'}\n"
            f"الاسم: {name or '—'}\n"
            f"الهاتف: {phone or '—'}\n"
            f"العنوان: {address or '—'}\n"
            f"معرّف الزبون (IG): {sender_id}"
        )
        _send_instagram_message_raw(send_account_id, ig_token, sender_id,
                                    "تم استلام طلبك ✅ سيتواصل معك المحل قريباً.")
        logging.warning("[IG] طلب جديد محفوظ: order_id=%s", order_id)
        return

    # كشف طلب التحدث مع إنسان
    human_keywords = ["تحدث مع إنسان", "اتكلم مع انسان", "اريد انسان", "أريد إنسان",
                      "مع انسان", "مع إنسان", "human", "agent", "صاحب المحل",
                      "كلم صاحب", "اكلم صاحب"]
    text_lower = text_stripped.lower()
    if any(kw.lower() in text_lower for kw in human_keywords):
        send_telegram_message_http(
            abs(shop_id),
            f"🆘 طلب تحدث مع إنسان\n"
            f"الزبون يطلب التحدث معك مباشرة عبر إنستغرام.\n"
            f"معرّف الزبون (IG): {sender_id}\n"
            f"الرسالة: {text_stripped}"
        )
        db.add_notification(shop_id, "inquiry", f"🆘 [طلب إنسان] {text_stripped}", None)
        _HUMAN_HANDOFF_TS[sender_id] = int(time.time())
        logging.warning("[IG-SILENCE] تم إسكات البوت للزبون %s لمدة 24 ساعة", sender_id)
        _send_instagram_message_raw(send_account_id, ig_token, sender_id,
            "تم تنبيه صاحب المحل وسيتواصل معك مباشرة في أقرب وقت ممكن 🙏\n"
            "شكراً لصبرك."
        )
        return

    # استفسار عام
    logging.warning("[IG-NOTIF] أحفظ إشعار لـ shop_id=%s kind=inquiry", shop_id)
    db.add_notification(shop_id, "inquiry", f"[إنستغرام {sender_id}] {text_stripped}", None)
    logging.warning("[IG-NOTIF] ✅ حُفظ الإشعار")
    send_telegram_message_http(
        abs(shop_id),
        f"❓ استفسار من إنستغرام\n{text_stripped}\nمعرّف الزبون (IG): {sender_id}"
    )
    _send_instagram_message_raw(send_account_id, ig_token, sender_id,
        "شكراً لتواصلك معنا 🙏\n"
        "سؤالك وصل لصاحب المحل وسيرد عليك في أقرب وقت.\n\n"
        "في هذي الأثناء يمكنك:\n"
        "• إرسال كود السلعة لمعرفة تفاصيلها وسعرها (مثل: AB-1234)\n"
        "• كتابة (تحدث مع إنسان) للتواصل المباشر مع صاحب المحل"
    )


# ────────────────────────────────────────────────────────────
# خادم الويب — للتحقق من webhook إنستغرام
# ────────────────────────────────────────────────────────────
_IG_VERIFY_TOKEN = os.environ.get("IG_VERIFY_TOKEN", "")
_flask_app = Flask(__name__)


@_flask_app.route("/webhook", methods=["GET"])
def _ig_verify():
    """تحقق Meta من صحة نقطة النهاية"""
    mode      = http_req.args.get("hub.mode")
    token     = http_req.args.get("hub.verify_token")
    challenge = http_req.args.get("hub.challenge", "")
    if mode == "subscribe" and token == _IG_VERIFY_TOKEN:
        logging.warning("[Webhook] تحقق Meta ناجح ✅")
        return challenge, 200
    logging.warning("[Webhook] تحقق Meta فاشل — رمز خاطئ أو وضع غير صحيح")
    return "Forbidden", 403


@_flask_app.route("/webhook", methods=["POST"])
def _ig_event():
    """استقبال أحداث إنستغرام — تحليل رسائل DM وتسجيلها"""
    logging.warning("[IG-RAW] %s", http_req.get_data(as_text=True)[:3000])
    try:
        payload = http_req.get_json(force=True, silent=True) or {}
        for entry in payload.get("entry", []):
            for msg_event in entry.get("messaging", []):
                msg       = msg_event.get("message", {})
                # تجاهل أحداث الصدى (رسائل أرسلها الحساب نفسه)
                if msg.get("is_echo"):
                    continue
                sender_id    = msg_event.get("sender",    {}).get("id", "—")
                recipient_id = msg_event.get("recipient", {}).get("id", "—")
                text = msg.get("text")
                if text:
                    logging.warning("[IG] رسالة من %s إلى %s: %s", sender_id, recipient_id, text)
                    handle_instagram_message(sender_id, recipient_id, text)
                else:
                    # صورة أو إعجاب أو نوع آخر
                    logging.info("[IG] حدث غير نصي من %s (mid=%s)", sender_id, msg.get("mid", "—"))
    except Exception as e:
        logging.error("[IG] خطأ في تحليل الحدث: %s", e)
    return "OK", 200


@_flask_app.route("/privacy", methods=["GET"])
def _privacy_page():
    html = '''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>سياسة الخصوصية — محلي | Privacy Policy — Mahalli</title>
<style>
  body { font-family: -apple-system, Segoe UI, Tahoma, Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 24px; line-height: 1.8; color: #1a1a1a; background: #fff; }
  h1 { font-size: 1.6em; border-bottom: 2px solid #2563eb; padding-bottom: 8px; }
  h2 { font-size: 1.2em; color: #2563eb; margin-top: 28px; }
  .en { direction: ltr; text-align: left; border-top: 3px solid #e5e7eb; margin-top: 48px; padding-top: 24px; }
  .updated { color: #666; font-size: 0.9em; }
  a { color: #2563eb; }
</style>
</head>
<body>

<h1>سياسة الخصوصية — محلي</h1>
<p class="updated">آخر تحديث: يونيو 2026</p>

<p>تطبيق "محلي" (المشار إليه بـ "الخدمة" أو "نحن") هو أداة تساعد المتاجر الصغيرة على إدارة محادثات عملائها على إنستغرام والرد عليها تلقائياً. نحترم خصوصيتك، وهذه السياسة توضّح البيانات التي نجمعها وكيف نستخدمها ونحميها.</p>

<h2>البيانات التي نجمعها</h2>
<p>عند استخدام الخدمة، قد نعالج: معرّف حساب إنستغرام الخاص بالمتجر والعميل، ونص الرسائل المُرسَلة من العميل إلى المتجر، واسم المستخدم العام، وتفاصيل الطلب التي يقدّمها العميل طوعاً (مثل الاسم ورقم الهاتف والعنوان لغرض إتمام الطلب). لا نجمع كلمات المرور أو بيانات الدفع أو أي معلومات حسّاسة أخرى.</p>

<h2>كيف نستخدم البيانات</h2>
<p>نستخدم البيانات حصراً لتشغيل الخدمة: استقبال رسائل العملاء، والرد عليها نيابةً عن المتجر، وإشعار صاحب المتجر بالطلبات والاستفسارات، وحفظ سجلّ الطلبات لمساعدة المتجر على متابعتها. لا نبيع بياناتك ولا نشاركها مع أطراف ثالثة لأغراض تسويقية.</p>

<h2>مشاركة البيانات</h2>
<p>لا نشارك بياناتك إلا مع: منصّة ميتا/إنستغرام (لاستقبال وإرسال الرسائل عبر واجهتها الرسمية)، ومزوّد الاستضافة الذي نشغّل عليه الخدمة. هذه الأطراف ملزمة بحماية البيانات.</p>

<h2>الاحتفاظ بالبيانات وحذفها</h2>
<p>نحتفظ بالبيانات طالما كان حساب المتجر نشطاً في الخدمة. يمكن لأي مستخدم طلب حذف بياناته بالكامل في أي وقت عبر زيارة صفحة حذف البيانات الخاصة بنا أو مراسلتنا. تُحذف البيانات خلال مدة معقولة من تلقّي الطلب.</p>

<h2>أمن البيانات</h2>
<p>نتّخذ إجراءات معقولة لحماية بياناتك من الوصول غير المصرّح به. نعتمد على الواجهات الرسمية المعتمدة من ميتا فقط، ولا نستخدم أي طرق غير رسمية قد تعرّض الحسابات للخطر.</p>

<h2>حقوقك</h2>
<p>لك الحق في الوصول إلى بياناتك أو تصحيحها أو طلب حذفها. للتواصل بخصوص أي من هذه الحقوق، راسلنا على البريد أدناه.</p>

<h2>التواصل معنا</h2>
<p>لأي استفسار حول هذه السياسة أو بياناتك: <a href="mailto:mahalliapp26@gmail.com">mahalliapp26@gmail.com</a></p>

<div class="en">
<h1>Privacy Policy — Mahalli</h1>
<p class="updated">Last updated: June 2026</p>

<p>Mahalli ("the Service", "we") is a tool that helps small shops manage and automatically respond to their customer conversations on Instagram. We respect your privacy. This policy explains what data we collect, and how we use and protect it.</p>

<h2>Data We Collect</h2>
<p>When you use the Service, we may process: the Instagram account IDs of the shop and the customer, the text of messages sent from the customer to the shop, public usernames, and order details voluntarily provided by the customer (such as name, phone number, and address for the purpose of completing an order). We do not collect passwords, payment data, or other sensitive information.</p>

<h2>How We Use Data</h2>
<p>We use data solely to operate the Service: receiving customer messages, replying on behalf of the shop, notifying the shop owner of orders and inquiries, and storing an order log to help the shop follow up. We do not sell your data or share it with third parties for marketing purposes.</p>

<h2>Data Sharing</h2>
<p>We only share data with: the Meta/Instagram platform (to receive and send messages via its official API), and the hosting provider on which we run the Service. These parties are obligated to protect the data.</p>

<h2>Data Retention and Deletion</h2>
<p>We retain data as long as the shop account is active in the Service. Any user may request complete deletion of their data at any time by visiting our Data Deletion page or contacting us. Data is deleted within a reasonable period of receiving the request.</p>

<h2>Data Security</h2>
<p>We take reasonable measures to protect your data from unauthorized access. We rely only on official Meta-approved APIs and do not use any unofficial methods that could put accounts at risk.</p>

<h2>Your Rights</h2>
<p>You have the right to access, correct, or request deletion of your data. To exercise any of these rights, contact us at the email below.</p>

<h2>Contact Us</h2>
<p>For any questions about this policy or your data: <a href="mailto:mahalliapp26@gmail.com">mahalliapp26@gmail.com</a></p>
</div>

</body>
</html>'''
    return Response(html, mimetype="text/html")


@_flask_app.route("/data-deletion", methods=["GET"])
def _data_deletion_page():
    html = '''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>حذف البيانات — محلي | Data Deletion — Mahalli</title>
<style>
  body { font-family: -apple-system, Segoe UI, Tahoma, Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 24px; line-height: 1.8; color: #1a1a1a; background: #fff; }
  h1 { font-size: 1.6em; border-bottom: 2px solid #2563eb; padding-bottom: 8px; }
  h2 { font-size: 1.2em; color: #2563eb; margin-top: 28px; }
  .en { direction: ltr; text-align: left; border-top: 3px solid #e5e7eb; margin-top: 48px; padding-top: 24px; }
  .updated { color: #666; font-size: 0.9em; }
  .box { background: #f1f5f9; border-right: 4px solid #2563eb; padding: 12px 18px; margin: 16px 0; border-radius: 6px; }
  .en .box { border-right: none; border-left: 4px solid #2563eb; }
  a { color: #2563eb; }
</style>
</head>
<body>

<h1>حذف البيانات — محلي</h1>
<p class="updated">آخر تحديث: يونيو 2026</p>

<p>نحن في "محلي" نحترم حقّك في التحكّم ببياناتك. يمكنك طلب حذف جميع بياناتك المرتبطة بالخدمة في أي وقت، وبشكل مجاني.</p>

<h2>ما الذي يُحذَف</h2>
<p>عند تقديم طلب الحذف، نحذف جميع البيانات المرتبطة بحسابك، بما في ذلك: معرّفات حساب إنستغرام، ونصوص الرسائل المحفوظة، وسجلّ الطلبات والاستفسارات، وأي معلومات أخرى جمعناها أثناء تشغيل الخدمة.</p>

<h2>كيف تطلب حذف بياناتك</h2>
<div class="box">
أرسل بريداً إلكترونياً إلى <a href="mailto:mahalliapp26@gmail.com">mahalliapp26@gmail.com</a> من البريد المرتبط بحسابك، واكتب في العنوان: "طلب حذف البيانات"، مع ذكر اسم المستخدم أو معرّف الحساب على إنستغرام.
</div>
<p>بعد استلام طلبك، نؤكّد هويتك، ثم نحذف بياناتك بالكامل خلال مدة لا تتجاوز 30 يوماً، ونرسل لك تأكيداً عند اكتمال الحذف.</p>

<h2>ملاحظة</h2>
<p>حذف بياناتك يعني توقّف الخدمة عن العمل لحسابك، إذ لا يمكننا تشغيل الخدمة دون البيانات الأساسية اللازمة لها.</p>

<h2>التواصل معنا</h2>
<p>لأي استفسار حول حذف البيانات: <a href="mailto:mahalliapp26@gmail.com">mahalliapp26@gmail.com</a></p>

<div class="en">
<h1>Data Deletion — Mahalli</h1>
<p class="updated">Last updated: June 2026</p>

<p>At Mahalli, we respect your right to control your data. You can request deletion of all your data associated with the Service at any time, free of charge.</p>

<h2>What Gets Deleted</h2>
<p>When you submit a deletion request, we delete all data associated with your account, including: Instagram account IDs, stored message texts, the log of orders and inquiries, and any other information we collected while operating the Service.</p>

<h2>How to Request Deletion</h2>
<div class="box">
Send an email to <a href="mailto:mahalliapp26@gmail.com">mahalliapp26@gmail.com</a> from the email associated with your account, with the subject: "Data Deletion Request", mentioning your Instagram username or account ID.
</div>
<p>After receiving your request, we verify your identity, then completely delete your data within a period not exceeding 30 days, and send you a confirmation once deletion is complete.</p>

<h2>Note</h2>
<p>Deleting your data means the Service will stop working for your account, as we cannot operate the Service without the essential data it requires.</p>

<h2>Contact Us</h2>
<p>For any questions about data deletion: <a href="mailto:mahalliapp26@gmail.com">mahalliapp26@gmail.com</a></p>
</div>

</body>
</html>'''
    return Response(html, mimetype="text/html")


@_flask_app.route("/instagram/callback", methods=["GET"])
def _ig_oauth_callback():
    """استقبال OAuth redirect من Meta بعد منح الصلاحيات."""
    code  = http_req.args.get("code", "")
    state = http_req.args.get("state", "")
    error = http_req.args.get("error", "")
    if error:
        logging.warning("[IG-OAUTH] خطأ من Meta: %s", error)
        return Response("<h1>❌ فشل الربط — أغلق هذه الصفحة وحاول مجدداً.</h1>",
                        mimetype="text/html", status=400)
    if not code or not state:
        return Response("<h1>❌ طلب غير صالح.</h1>", mimetype="text/html", status=400)

    # التحقق من state وفكّ تشفيره
    try:
        s = URLSafeTimedSerializer(STATE_SECRET_KEY)
        owner_telegram_id = int(s.loads(state, salt="ig-oauth", max_age=900))
    except Exception as e:
        logging.warning("[IG-OAUTH] state غير صالح: %s", e)
        return Response("<h1>❌ انتهت صلاحية الرابط (15 دقيقة). أعد المحاولة.</h1>",
                        mimetype="text/html", status=400)

    # استبدال code بـ short-lived token
    try:
        r1 = requests.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id":     IG_APP_ID,
                "client_secret": IG_APP_SECRET,
                "grant_type":    "authorization_code",
                "redirect_uri":  IG_REDIRECT_URI,
                "code":          code,
            },
            timeout=15,
        )
        logging.warning("[IG-OAUTH] رد Meta لاستبدال code: status=%s body=%s", r1.status_code, r1.text)
        r1.raise_for_status()
        short_data  = r1.json()
        short_token = short_data["access_token"]
        send_account_id = str(short_data["user_id"])
    except Exception as e:
        logging.error("[IG-OAUTH] فشل استبدال code: %s", e)
        return Response("<h1>❌ فشل الاتصال بـ Meta. أعد المحاولة.</h1>",
                        mimetype="text/html", status=502)

    # تحويل إلى long-lived token (60 يوم)
    try:
        r2 = requests.get(
            "https://graph.instagram.com/access_token",
            params={
                "grant_type":    "ig_exchange_token",
                "client_secret": IG_APP_SECRET,
                "access_token":  short_token,
            },
            timeout=15,
        )
        r2.raise_for_status()
        ll_data     = r2.json()
        long_token  = ll_data["access_token"]
        expires_in  = ll_data.get("expires_in", 5_184_000)
        expires_at  = int(time.time()) + expires_in
    except Exception as e:
        logging.error("[IG-OAUTH] فشل تحويل long-lived token: %s", e)
        return Response("<h1>❌ فشل الحصول على token طويل الأمد.</h1>",
                        mimetype="text/html", status=502)

    # الحصول على username الحقيقي عبر /me
    try:
        r3 = requests.get(
            f"https://graph.instagram.com/v25.0/{send_account_id}",
            params={"fields": "username", "access_token": long_token},
            timeout=15,
        )
        username = r3.json().get("username", "") if r3.status_code == 200 else ""
    except Exception:
        username = ""

    # حفظ في DB (webhook_account_id = send_account_id مبدئياً؛ يُصحَّح تلقائياً عند أول webhook)
    db.add_ig_shop(
        webhook_account_id=send_account_id,
        send_account_id=send_account_id,
        access_token=long_token,
        token_expires_at=expires_at,
        owner_telegram_id=owner_telegram_id,
        username=username,
    )
    logging.warning("[IG-OAUTH] ✅ محل إنستغرام @%s مربوط لـ telegram_id=%s",
                    username, owner_telegram_id)

    # إشعار صاحب المحل عبر Telegram
    send_telegram_message_http(
        abs(owner_telegram_id),
        f"✅ تم ربط حساب إنستغرام بنجاح!\n"
        f"الحساب: @{username}\n"
        f"معرّف الحساب: {send_account_id}\n"
        f"صلاحية التوكن حتى: {int(expires_in // 86400)} يوماً"
    )

    return Response(
        "<h1 style='font-family:sans-serif;text-align:center;margin-top:60px'>"
        "✅ تم ربط حساب إنستغرام بنجاح! يمكنك إغلاق هذه الصفحة.</h1>",
        mimetype="text/html"
    )


def _run_web_server() -> None:
    port = int(os.environ.get("PORT", 8080))
    logging.warning("[Webhook] خادم الويب يعمل على المنفذ %d", port)
    _flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


threading.Thread(target=_run_web_server, daemon=True).start()

print("Bot is running...")
app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


