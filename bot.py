import logging
import os
import re
import threading
from datetime import date as _date, time as _time

from flask import Flask, request as http_req

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

# ── تهيئة قاعدة البيانات ─────────────────────────────────────
logging.basicConfig(level=logging.WARNING)
db.init_db()
db.migrate_from_json("products.json", OWNER_CHAT_ID)  # no-op إن سبق تنفيذها
db.cleanup_admin_shop(OWNER_CHAT_ID)                  # تنظيف لمرة واحدة

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
    [["➕ إضافة سلعة"], ["📋 عرض السلع", "🗑 حذف سلعة"], ["🔔 الإشعارات"]],
    resize_keyboard=True,
)


# ────────────────────────────────────────────────────────────
# مساعدات عامة
# ────────────────────────────────────────────────────────────
def _eff_uid(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    """يُعيد المعرّف الوهمي في وضع محل الاختبار (من قاعدة البيانات)"""
    real_uid = update.effective_chat.id
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


async def _post_init(application) -> None:
    jq = application.job_queue
    jq.run_daily(job_expire_shops,  _time(0, 5))
    jq.run_daily(job_expiring_soon, _time(0, 10))


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
app.add_handler(MessageHandler(filters.Regex(r"^📊 المشتركون$"),       show_subscribers))
app.add_handler(MessageHandler(filters.Regex(r"^📈 إحصاءات المنصّة$"), show_stats))
# كود التفعيل يُعالَج قبل echo
app.add_handler(MessageHandler(filters.Regex(r"^ACT-[A-Z0-9]{5}$"),    handle_activation_code))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,         echo))

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
                else:
                    # صورة أو إعجاب أو نوع آخر
                    logging.info("[IG] حدث غير نصي من %s (mid=%s)", sender_id, msg.get("mid", "—"))
    except Exception as e:
        logging.error("[IG] خطأ في تحليل الحدث: %s", e)
    return "OK", 200


def _run_web_server() -> None:
    port = int(os.environ.get("PORT", 8080))
    logging.warning("[Webhook] خادم الويب يعمل على المنفذ %d", port)
    _flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


threading.Thread(target=_run_web_server, daemon=True).start()

print("Bot is running...")
app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
