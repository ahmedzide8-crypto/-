import os
import json
import logging
import sqlite3
import random
import string
import shutil
from contextlib import contextmanager
from typing import Optional

# ── مسار قاعدة البيانات ─────────────────────────────────────
# إذا كان القرص الدائم /data موجوداً (على Railway)، نحفظ فيه لضمان
# بقاء البيانات بين كل تحديث. غير ذلك (محلياً)، نستخدم ملفاً عادياً.
_PERSIST_DIR = "/data"
if os.path.isdir(_PERSIST_DIR):
    DB_FILE = os.path.join(_PERSIST_DIR, "bot.db")
    # ترحيل لمرة واحدة: انقل bot.db القديم (من المكان المؤقت) إلى القرص الدائم
    # فقط إذا كان القديم موجوداً والجديد لم يُنشأ بعد — يمنع فقدان البيانات الحالية.
    _OLD_DB = "bot.db"
    if os.path.exists(_OLD_DB) and not os.path.exists(DB_FILE):
        try:
            shutil.copy2(_OLD_DB, DB_FILE)
            logging.warning("[DB-MIGRATE] ✅ نُقلت قاعدة البيانات القديمة إلى القرص الدائم: %s", DB_FILE)
        except Exception as e:
            logging.error("[DB-MIGRATE] ❌ فشل نقل قاعدة البيانات القديمة: %s", e)
else:
    DB_FILE = "bot.db"

# ── مدد الاشتراك بالأيام — عدّلها هنا فقط ────────────────────
PLAN_DAYS = {
    "biweekly": 14,
    "monthly":  30,
    "yearly":   365,
}


@contextmanager
def _conn():
    """مدير سياق للاتصال بقاعدة البيانات"""
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ────────────────────────────────────────────────────────────
# تهيئة الجداول
# ────────────────────────────────────────────────────────────
def init_db() -> None:
    """أنشئ الجداول عند أول تشغيل وسجّل الأدمن"""
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS shops (
                telegram_id   INTEGER PRIMARY KEY,
                username      TEXT,
                status        TEXT    NOT NULL DEFAULT 'pending',
                plan          TEXT,
                start_date    TEXT,
                end_date      TEXT,
                message_count INTEGER NOT NULL DEFAULT 0,
                joined_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS products (
                code    TEXT    PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(telegram_id),
                name    TEXT    NOT NULL,
                price   REAL    NOT NULL,
                sizes   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS retired_codes (
                code TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS admin (
                telegram_id INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS activation_codes (
                code    TEXT    PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(telegram_id),
                plan    TEXT    NOT NULL,
                used    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS orders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id          INTEGER NOT NULL REFERENCES shops(telegram_id),
                product_code     TEXT,
                customer_name    TEXT,
                customer_phone   TEXT,
                customer_address TEXT,
                customer_chat_id INTEGER,
                status           TEXT NOT NULL DEFAULT 'new',
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id     INTEGER NOT NULL,
                kind        TEXT    NOT NULL,
                content     TEXT    NOT NULL,
                customer_id INTEGER,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS admin_state (
                admin_id     INTEGER PRIMARY KEY,
                mode         TEXT,
                test_shop_id INTEGER,
                last_product TEXT
            );

            CREATE TABLE IF NOT EXISTS ig_shops (
                webhook_account_id  TEXT    PRIMARY KEY,
                send_account_id     TEXT    NOT NULL,
                access_token        TEXT    NOT NULL,
                token_expires_at    INTEGER NOT NULL,
                owner_telegram_id   INTEGER NOT NULL,
                username            TEXT    DEFAULT '',
                created_at          INTEGER NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'active'
            );

            -- متغيّرات السلعة: كل صف = (كود السلعة + لون + قياس) وله كميته الخاصة.
            CREATE TABLE IF NOT EXISTS variants (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                code     TEXT    NOT NULL REFERENCES products(code),
                color    TEXT    NOT NULL DEFAULT '',
                size     TEXT    NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 0,
                UNIQUE(code, color, size)
            );

            -- حالة الطلب الجاري لكل زبون إنستغرام (تفاعل خطوة بخطوة).
            -- المفتاح: sender_id (معرّف الزبون على إنستغرام).
            CREATE TABLE IF NOT EXISTS order_flow (
                sender_id  TEXT    PRIMARY KEY,
                shop_id    INTEGER NOT NULL,
                code       TEXT,
                color      TEXT,
                size       TEXT,
                quantity   INTEGER,
                step       TEXT    NOT NULL DEFAULT 'idle',
                updated_at INTEGER NOT NULL DEFAULT 0
            );

            -- تعطيل الرد الآلي لزبون معيّن (لكل محل+زبون على حدة).
            -- disabled_until = الوقت (epoch) الذي يعود بعده الرد الآلي تلقائياً.
            CREATE TABLE IF NOT EXISTS auto_reply_state (
                shop_id        INTEGER NOT NULL,
                customer_ig_id TEXT    NOT NULL,
                disabled_until INTEGER NOT NULL,
                PRIMARY KEY (shop_id, customer_ig_id)
            );
        """)

        # أضف الأعمدة الجديدة آمناً على قواعد بيانات قائمة
        existing = {row["name"] for row in con.execute("PRAGMA table_info(orders)").fetchall()}
        if "customer_chat_id" not in existing:
            con.execute("ALTER TABLE orders ADD COLUMN customer_chat_id INTEGER")
        if "status" not in existing:
            con.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")
        # أعمدة تفاصيل الطلب (لون/قياس/كمية/معرّف إنستغرام) للخصم الدقيق عند القبول
        if "order_color" not in existing:
            con.execute("ALTER TABLE orders ADD COLUMN order_color TEXT DEFAULT ''")
        if "order_size" not in existing:
            con.execute("ALTER TABLE orders ADD COLUMN order_size TEXT DEFAULT ''")
        if "order_qty" not in existing:
            con.execute("ALTER TABLE orders ADD COLUMN order_qty INTEGER DEFAULT 1")
        if "ig_sender_id" not in existing:
            con.execute("ALTER TABLE orders ADD COLUMN ig_sender_id TEXT DEFAULT ''")
        # أعمدة بيانات الزبون المؤقتة في order_flow (تُستخدم أثناء تأكيد الطلب)
        of_cols = {row["name"] for row in con.execute("PRAGMA table_info(order_flow)").fetchall()}
        if "cust_name" not in of_cols:
            con.execute("ALTER TABLE order_flow ADD COLUMN cust_name TEXT DEFAULT ''")
        if "cust_phone" not in of_cols:
            con.execute("ALTER TABLE order_flow ADD COLUMN cust_phone TEXT DEFAULT ''")
        if "cust_address" not in of_cols:
            con.execute("ALTER TABLE order_flow ADD COLUMN cust_address TEXT DEFAULT ''")

        admin_id = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
        if admin_id:
            con.execute(
                "INSERT OR IGNORE INTO admin (telegram_id) VALUES (?)",
                (int(admin_id),)
            )
            # تحقق فوري من التسجيل
            registered = con.execute(
                "SELECT telegram_id FROM admin WHERE telegram_id = ?", (int(admin_id),)
            ).fetchone()
            if registered:
                logging.warning("[ADMIN] ✅ ADMIN_TELEGRAM_ID=%s مسجّل في جدول admin", admin_id)
            else:
                logging.error("[ADMIN] ❌ ADMIN_TELEGRAM_ID=%s فشل التسجيل!", admin_id)
        else:
            logging.error("[ADMIN] ❌ ADMIN_TELEGRAM_ID غير مضبوط في متغيرات البيئة!")


# ────────────────────────────────────────────────────────────
# الأدمن
# ────────────────────────────────────────────────────────────
def is_admin(telegram_id: int) -> bool:
    """هل المعرّف في جدول الأدمن؟"""
    with _conn() as con:
        return con.execute(
            "SELECT 1 FROM admin WHERE telegram_id = ?", (telegram_id,)
        ).fetchone() is not None


def get_admin_id() -> Optional[int]:
    """جلب معرّف الأدمن الأول"""
    with _conn() as con:
        row = con.execute("SELECT telegram_id FROM admin LIMIT 1").fetchone()
        return row[0] if row else None


# ────────────────────────────────────────────────────────────
# المحلات
# ────────────────────────────────────────────────────────────
def add_shop(telegram_id: int, username: Optional[str] = None) -> None:
    """أضف محلاً أو تجاهل إن كان موجوداً"""
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO shops (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username)
        )


def get_shop(telegram_id: int) -> Optional[dict]:
    """اجلب بيانات محل بمعرّفه"""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM shops WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def cleanup_admin_shop(admin_id: int) -> None:
    """احذف بيانات المحل القديمة للأدمن (تنظيف لمرة واحدة عند التحويل)"""
    with _conn() as con:
        # انقل أكواد سلع الأدمن إلى المتقاعدة
        rows = con.execute(
            "SELECT code FROM products WHERE shop_id = ?", (admin_id,)
        ).fetchall()
        for row in rows:
            con.execute(
                "INSERT OR IGNORE INTO retired_codes (code) VALUES (?)", (row[0],)
            )
        con.execute("DELETE FROM products WHERE shop_id = ?", (admin_id,))
        con.execute("DELETE FROM shops WHERE telegram_id = ?", (admin_id,))


def clear_test_shop(test_id: int) -> None:
    """امسح بيانات محل الاختبار يدوياً عبر /deleteinfo"""
    admin_id = -test_id  # test_id سالب = -admin_id
    with _conn() as con:
        con.execute("DELETE FROM admin_state WHERE admin_id = ?", (admin_id,))
        con.execute("DELETE FROM notifications WHERE shop_id = ?", (test_id,))
        con.execute("DELETE FROM orders WHERE shop_id = ?", (test_id,))
        con.execute("DELETE FROM products WHERE shop_id = ?", (test_id,))
        con.execute("DELETE FROM activation_codes WHERE shop_id = ?", (test_id,))
        con.execute("DELETE FROM shops WHERE telegram_id = ?", (test_id,))


def set_shop_active_unlimited(telegram_id: int) -> None:
    """فعّل محلاً بلا تاريخ انتهاء (احتياطي)"""
    with _conn() as con:
        con.execute(
            "UPDATE shops SET status='active', plan='admin', start_date=date('now') "
            "WHERE telegram_id = ?",
            (telegram_id,)
        )


def increment_message_count(shop_id: int) -> None:
    """زد عدّاد رسائل الزبائن"""
    with _conn() as con:
        con.execute(
            "UPDATE shops SET message_count = message_count + 1 WHERE telegram_id = ?",
            (shop_id,)
        )


# ────────────────────────────────────────────────────────────
# أكواد التفعيل
# ────────────────────────────────────────────────────────────
def create_activation_code(shop_id: int, plan: str) -> str:
    """ولّد كود تفعيل فريد (ACT-XXXXX) — يُبطل أي كود قديم غير مستخدم لنفس المحل أولاً"""
    with _conn() as con:
        # كود واحد صالح فقط لكل محل في أي وقت
        con.execute(
            "DELETE FROM activation_codes WHERE shop_id = ? AND used = 0",
            (shop_id,)
        )
        while True:
            suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
            code = f"ACT-{suffix}"
            exists = con.execute(
                "SELECT 1 FROM activation_codes WHERE code = ?", (code,)
            ).fetchone()
            if not exists:
                con.execute(
                    "INSERT INTO activation_codes (code, shop_id, plan) VALUES (?, ?, ?)",
                    (code, shop_id, plan)
                )
                return code


def redeem_activation_code(code: str, shop_id: int) -> Optional[str]:
    """تحقق من كود التفعيل وفعّل المحل. يُعيد اسم الخطة أو None إن فشل."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM activation_codes WHERE code = ? AND shop_id = ? AND used = 0",
            (code, shop_id)
        ).fetchone()
        if not row:
            return None
        plan = row["plan"]
        days = PLAN_DAYS.get(plan, 30)
        con.execute(
            """UPDATE shops
               SET status='active', plan=?,
                   start_date=date('now'),
                   end_date=date('now', ?)
               WHERE telegram_id = ?""",
            (plan, f"+{days} days", shop_id)
        )
        con.execute("UPDATE activation_codes SET used=1 WHERE code=?", (code,))
        return plan


# ────────────────────────────────────────────────────────────
# السلع
# ────────────────────────────────────────────────────────────
def add_product(code: str, shop_id: int, name: str, price: float, sizes: list) -> None:
    """أضف سلعة جديدة"""
    with _conn() as con:
        con.execute(
            "INSERT INTO products (code, shop_id, name, price, sizes) VALUES (?, ?, ?, ?, ?)",
            (code, shop_id, name, price, ",".join(sizes))
        )


def get_product(code: str) -> Optional[dict]:
    """اجلب سلعة بكودها"""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM products WHERE code = ?", (code,)
        ).fetchone()
        if not row:
            return None
        p = dict(row)
        p["sizes"] = p["sizes"].split(",")
        return p


def get_shop_products(shop_id: int) -> list:
    """اجلب كل سلع محل معيّن"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM products WHERE shop_id = ?", (shop_id,)
        ).fetchall()
        result = []
        for row in rows:
            p = dict(row)
            p["sizes"] = p["sizes"].split(",")
            result.append(p)
        return result


def delete_product(code: str, shop_id: int) -> bool:
    """احذف السلعة إن كانت تخص هذا المحل، وانقل كودها للمتقاعدة"""
    with _conn() as con:
        affected = con.execute(
            "DELETE FROM products WHERE code = ? AND shop_id = ?", (code, shop_id)
        ).rowcount
        if affected:
            con.execute(
                "INSERT OR IGNORE INTO retired_codes (code) VALUES (?)", (code,)
            )
            con.execute("DELETE FROM variants WHERE code = ?", (code,))
        return bool(affected)


# ────────────────────────────────────────────────────────────
# متغيّرات السلعة (لون + قياس + كمية)
# ────────────────────────────────────────────────────────────
def add_variant(code: str, color: str, quantity: int, size: str = "") -> None:
    """أضف تركيبة (لون+قياس) بكميتها. إن وُجدت، اجمع الكمية عليها."""
    with _conn() as con:
        con.execute(
            """INSERT INTO variants (code, color, size, quantity)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(code, color, size)
               DO UPDATE SET quantity = quantity + excluded.quantity""",
            (code, color, size, quantity)
        )


def get_variants(code: str, in_stock_only: bool = False) -> list:
    """اجلب كل تركيبات سلعة. مع in_stock_only=True يرجّع المتوفر فقط (كمية>0)."""
    with _conn() as con:
        q = "SELECT color, size, quantity FROM variants WHERE code = ?"
        if in_stock_only:
            q += " AND quantity > 0"
        q += " ORDER BY id"
        return [dict(r) for r in con.execute(q, (code,)).fetchall()]


def get_variant(code: str, color: str, size: str = "") -> Optional[dict]:
    """اجلب تركيبة محددة (مطابقة غير حسّاسة لحالة الأحرف على اللون والقياس)."""
    with _conn() as con:
        row = con.execute(
            """SELECT color, size, quantity FROM variants
               WHERE code = ? AND LOWER(color) = LOWER(?) AND LOWER(size) = LOWER(?)""",
            (code, color, size)
        ).fetchone()
        return dict(row) if row else None


def decrement_variant(code: str, color: str, amount: int, size: str = "") -> bool:
    """أنقص كمية تركيبة عند قبول الطلب. يرجّع False إن كانت الكمية غير كافية."""
    with _conn() as con:
        row = con.execute(
            """SELECT quantity FROM variants
               WHERE code = ? AND LOWER(color) = LOWER(?) AND LOWER(size) = LOWER(?)""",
            (code, color, size)
        ).fetchone()
        if row is None or row["quantity"] < amount:
            return False
        con.execute(
            """UPDATE variants SET quantity = quantity - ?
               WHERE code = ? AND LOWER(color) = LOWER(?) AND LOWER(size) = LOWER(?)""",
            (amount, code, color, size)
        )
        return True


def has_variants(code: str) -> bool:
    """هل للسلعة تركيبات مسجّلة؟ (لتمييز السلع القديمة بلا مخزون)."""
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM variants WHERE code = ? LIMIT 1", (code,)
        ).fetchone()
        return row is not None


def get_available_colors(code: str) -> list:
    """ألوان السلعة التي بها قياس واحد على الأقل متوفر (كمية>0)، دون تكرار."""
    with _conn() as con:
        rows = con.execute(
            """SELECT DISTINCT color FROM variants
               WHERE code = ? AND quantity > 0 AND color <> ''
               ORDER BY id""",
            (code,)
        ).fetchall()
        return [r["color"] for r in rows]


def get_available_sizes_for_color(code: str, color: str) -> list:
    """قياسات لون معيّن المتوفرة فعلياً (كمية>0)."""
    with _conn() as con:
        rows = con.execute(
            """SELECT size, quantity FROM variants
               WHERE code = ? AND LOWER(color) = LOWER(?) AND quantity > 0
               ORDER BY id""",
            (code, color)
        ).fetchall()
        return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────
# حالة الطلب الجاري لزبون إنستغرام
# ────────────────────────────────────────────────────────────
def get_order_flow(sender_id: str) -> Optional[dict]:
    """اجلب حالة الطلب الجاري لزبون، أو None إن لم توجد."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM order_flow WHERE sender_id = ?", (sender_id,)
        ).fetchone()
        return dict(row) if row else None


def set_order_flow(sender_id: str, shop_id: int, step: str,
                   code: str = None, color: str = None,
                   size: str = None, quantity: int = None) -> None:
    """احفظ/حدّث حالة الطلب الجاري لزبون."""
    import time as _t
    with _conn() as con:
        con.execute(
            """INSERT INTO order_flow
                   (sender_id, shop_id, code, color, size, quantity, step, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sender_id) DO UPDATE SET
                   shop_id=excluded.shop_id, code=excluded.code, color=excluded.color,
                   size=excluded.size, quantity=excluded.quantity,
                   step=excluded.step, updated_at=excluded.updated_at""",
            (sender_id, shop_id, code, color, size, quantity, step, int(_t.time()))
        )


def clear_order_flow(sender_id: str) -> None:
    """احذف حالة الطلب الجاري لزبون (بعد الإكمال أو الإلغاء)."""
    with _conn() as con:
        con.execute("DELETE FROM order_flow WHERE sender_id = ?", (sender_id,))


def set_order_customer(sender_id: str, name: str, phone: str, address: str) -> None:
    """خزّن بيانات الزبون مؤقتاً في order_flow أثناء انتظار تأكيد الطلب."""
    with _conn() as con:
        con.execute(
            """UPDATE order_flow
               SET cust_name = ?, cust_phone = ?, cust_address = ?
               WHERE sender_id = ?""",
            (name or "", phone or "", address or "", sender_id)
        )


def get_order_customer(sender_id: str) -> Optional[dict]:
    """اجلب بيانات الزبون المخزّنة مؤقتاً (اسم/هاتف/عنوان)."""
    with _conn() as con:
        row = con.execute(
            "SELECT cust_name, cust_phone, cust_address FROM order_flow WHERE sender_id = ?",
            (sender_id,)
        ).fetchone()
        if row is None:
            return None
        return {"name": row["cust_name"], "phone": row["cust_phone"],
                "address": row["cust_address"]}


# ────────────────────────────────────────────────────────────
# تعطيل/تفعيل الرد الآلي لزبون معيّن (لكل محل+زبون)
# ────────────────────────────────────────────────────────────
def disable_auto_reply(shop_id: int, customer_ig_id: str, hours: int = 24) -> None:
    """عطّل الرد الآلي لزبون معيّن لمدة (افتراضياً 24 ساعة)."""
    import time as _t
    until = int(_t.time()) + hours * 3600
    with _conn() as con:
        con.execute(
            """INSERT INTO auto_reply_state (shop_id, customer_ig_id, disabled_until)
               VALUES (?, ?, ?)
               ON CONFLICT(shop_id, customer_ig_id)
               DO UPDATE SET disabled_until = excluded.disabled_until""",
            (shop_id, str(customer_ig_id), until)
        )


def enable_auto_reply(shop_id: int, customer_ig_id: str) -> None:
    """أعد تفعيل الرد الآلي لزبون (احذف حالة التعطيل)."""
    with _conn() as con:
        con.execute(
            "DELETE FROM auto_reply_state WHERE shop_id = ? AND customer_ig_id = ?",
            (shop_id, str(customer_ig_id))
        )


def is_auto_reply_disabled(shop_id: int, customer_ig_id: str) -> bool:
    """هل الرد الآلي معطّل لهذا الزبون الآن؟ (يعود تلقائياً بعد انتهاء المدة)."""
    import time as _t
    with _conn() as con:
        row = con.execute(
            "SELECT disabled_until FROM auto_reply_state "
            "WHERE shop_id = ? AND customer_ig_id = ?",
            (shop_id, str(customer_ig_id))
        ).fetchone()
        if row is None:
            return False
        if int(_t.time()) >= row["disabled_until"]:
            # انتهت المدة → احذف الحالة وأعد التفعيل تلقائياً
            con.execute(
                "DELETE FROM auto_reply_state WHERE shop_id = ? AND customer_ig_id = ?",
                (shop_id, str(customer_ig_id))
            )
            return False
        return True


def pop_expired_handoffs() -> list:
    """
    يجلب الحالات المنتهية (مضى وقتها) ويحذفها، لإرسال نقطة تنبيه للزبون.
    يُعيد قائمة [(shop_id, customer_ig_id)] للحالات التي انتهت الآن.
    """
    import time as _t
    now = int(_t.time())
    with _conn() as con:
        rows = con.execute(
            "SELECT shop_id, customer_ig_id FROM auto_reply_state WHERE disabled_until <= ?",
            (now,)
        ).fetchall()
        expired = [(r["shop_id"], r["customer_ig_id"]) for r in rows]
        if expired:
            con.execute("DELETE FROM auto_reply_state WHERE disabled_until <= ?", (now,))
        return expired


# ────────────────────────────────────────────────────────────
# توليد كود السلعة
# ────────────────────────────────────────────────────────────
def generate_unique_code() -> str:
    """ولّد كوداً فريداً عبر كل المنصّة (يتحقق من products و retired_codes)"""
    with _conn() as con:
        while True:
            prefix = "".join(random.choices(string.ascii_uppercase, k=2))
            suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
            code = f"{prefix}-{suffix}"
            exists = con.execute(
                "SELECT 1 FROM products WHERE code = ? "
                "UNION SELECT 1 FROM retired_codes WHERE code = ?",
                (code, code)
            ).fetchone()
            if not exists:
                return code


# ────────────────────────────────────────────────────────────
# استعلامات لوحة الأدمن
# ────────────────────────────────────────────────────────────
def get_all_shops() -> list:
    """كل المحلات مرتّبة: النشطة أولاً ثم المنتظرة ثم المنتهية، باستثناء الوهمية (ID سالب)"""
    with _conn() as con:
        rows = con.execute("""
            SELECT telegram_id, username, status, plan,
                   start_date, end_date, message_count, joined_at
            FROM shops
            WHERE telegram_id > 0
            ORDER BY
                CASE
                    WHEN status = 'active'
                         AND (end_date IS NULL OR end_date >= date('now')) THEN 0
                    WHEN status = 'pending' THEN 1
                    ELSE 2
                END,
                CASE WHEN end_date IS NULL THEN 1 ELSE 0 END,
                end_date ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_platform_stats() -> dict:
    """إحصاءات المنصّة: أعداد المحلات حسب الحالة وإجمالي السلع"""
    with _conn() as con:
        active = con.execute(
            "SELECT COUNT(*) FROM shops WHERE telegram_id > 0 "
            "AND status = 'active' AND (end_date IS NULL OR end_date >= date('now'))"
        ).fetchone()[0]
        expired = con.execute(
            "SELECT COUNT(*) FROM shops WHERE telegram_id > 0 "
            "AND (status = 'expired' OR "
            "(status = 'active' AND end_date IS NOT NULL AND end_date < date('now')))"
        ).fetchone()[0]
        pending = con.execute(
            "SELECT COUNT(*) FROM shops WHERE telegram_id > 0 AND status = 'pending'"
        ).fetchone()[0]
        products = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        return {
            "active":   active,
            "expired":  expired,
            "pending":  pending,
            "total":    active + expired + pending,
            "products": products,
        }


def get_expiring_soon(days: int = 3) -> list:
    """المحلات النشطة التي ينتهي اشتراكها خلال عدد الأيام المحدّد"""
    with _conn() as con:
        rows = con.execute("""
            SELECT telegram_id, username, end_date
            FROM shops
            WHERE telegram_id > 0
              AND status = 'active'
              AND end_date IS NOT NULL
              AND date(end_date) >= date('now')
              AND date(end_date) <= date('now', ?)
            ORDER BY end_date ASC
        """, (f"+{days} days",)).fetchall()
        return [dict(r) for r in rows]


def is_subscription_active(shop_id: int) -> bool:
    """هل اشتراك المحل ساري؟ (نشط وتاريخ انتهائه اليوم أو مستقبلاً أو بلا تاريخ)"""
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM shops WHERE telegram_id = ? AND status = 'active' "
            "AND (end_date IS NULL OR end_date >= date('now'))",
            (shop_id,)
        ).fetchone()
        return row is not None


def expire_overdue_shops() -> list:
    """حدّث المحلات المنتهية إلى expired وأعد قائمة معرّفاتها (يُستثنى الوهمي)"""
    with _conn() as con:
        rows = con.execute(
            "SELECT telegram_id FROM shops "
            "WHERE telegram_id > 0 AND status = 'active' "
            "AND end_date IS NOT NULL AND end_date < date('now')"
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            con.execute(
                f"UPDATE shops SET status = 'expired' WHERE telegram_id IN ({placeholders})",
                ids
            )
        return ids


# ────────────────────────────────────────────────────────────
# ترحيل products.json
# ────────────────────────────────────────────────────────────
def migrate_from_json(json_path: str, owner_id: int) -> None:
    """رحّل products.json إلى DB إن وُجد الملف، ثم أعد تسميته"""
    if not os.path.exists(json_path):
        return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    add_shop(owner_id)

    for code, p in data.get("products", {}).items():
        sizes = (
            p["sizes"] if isinstance(p["sizes"], list)
            else [s.strip() for s in p["sizes"].split(",")]
        )
        try:
            add_product(code, owner_id, p["name"], float(p["price"]), sizes)
        except Exception:
            pass

    with _conn() as con:
        for code in data.get("retired_codes", []):
            con.execute(
                "INSERT OR IGNORE INTO retired_codes (code) VALUES (?)", (code,)
            )

    os.rename(json_path, json_path + ".migrated")


# ────────────────────────────────────────────────────────────
# الطلبات
# ────────────────────────────────────────────────────────────
def add_order(
    shop_id: int,
    product_code: str,
    customer_name: str,
    customer_phone: str,
    customer_address: str,
    customer_chat_id: int = 0,
    color: str = "",
    size: str = "",
    qty: int = 1,
    ig_sender_id: str = "",
) -> int:
    """سجّل طلب زبون جديد ويُعيد id الطلب"""
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO orders
               (shop_id, product_code, customer_name, customer_phone,
                customer_address, customer_chat_id,
                order_color, order_size, order_qty, ig_sender_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (shop_id, product_code, customer_name, customer_phone,
             customer_address, customer_chat_id,
             color, size, qty, ig_sender_id),
        )
        return cur.lastrowid


def get_order(order_id: int) -> Optional[dict]:
    """اجلب طلباً بمعرّفه"""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def count_pending_orders_by_shop() -> dict:
    """عدد الطلبات المعلّقة (لم تُقبل ولم تُرفض) لكل محل. يُعيد {shop_id: count}."""
    with _conn() as con:
        rows = con.execute(
            "SELECT shop_id, COUNT(*) AS n FROM orders "
            "WHERE status = 'new' GROUP BY shop_id"
        ).fetchall()
        return {r["shop_id"]: r["n"] for r in rows}


def get_pending_orders_for_shop(shop_id: int) -> list:
    """اجلب الطلبات المعلّقة لمحل معيّن (الأحدث أولاً)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE shop_id = ? AND status = 'new' "
            "ORDER BY id DESC", (shop_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def accept_order(order_id: int) -> dict:
    """
    اقبل الطلب: تحقّق من المخزون، اخصم الكمية ذرّياً، علّم الطلب مقبولاً.
    يُعيد dict فيه: ok (bool)، reason (str)، والطلب.
    """
    with _conn() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            return {"ok": False, "reason": "not_found", "order": None}
        order = dict(order)
        if order["status"] == "accepted":
            return {"ok": False, "reason": "already_accepted", "order": order}
        if order["status"] == "rejected":
            return {"ok": False, "reason": "already_rejected", "order": order}

        code  = order["product_code"]
        color = order["order_color"] or ""
        size  = order["order_size"] or ""
        qty   = order["order_qty"] or 1

        # لو السلعة لها مخزون (variants)، تحقّق واخصم ذرّياً
        has_var = con.execute(
            "SELECT 1 FROM variants WHERE code = ? LIMIT 1", (code,)
        ).fetchone() is not None

        if has_var:
            row = con.execute(
                """SELECT quantity FROM variants
                   WHERE code = ? AND LOWER(color)=LOWER(?) AND LOWER(size)=LOWER(?)""",
                (code, color, size)
            ).fetchone()
            avail = row["quantity"] if row else 0
            if avail < qty:
                # نفد المخزون بين الطلب والقبول — علّمه كحالة خاصة كي لا يبقى معلّقاً للأبد
                con.execute("UPDATE orders SET status='out_of_stock' WHERE id = ?", (order_id,))
                return {"ok": False, "reason": "out_of_stock",
                        "available": avail, "order": order}
            con.execute(
                """UPDATE variants SET quantity = quantity - ?
                   WHERE code = ? AND LOWER(color)=LOWER(?) AND LOWER(size)=LOWER(?)""",
                (qty, code, color, size)
            )

        con.execute("UPDATE orders SET status='accepted' WHERE id = ?", (order_id,))
        return {"ok": True, "reason": "accepted", "order": order}


def reject_order(order_id: int) -> Optional[dict]:
    """ارفض الطلب (لا يمسّ المخزون). يُعيد الطلب أو None."""
    with _conn() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            return None
        order = dict(order)
        if order["status"] in ("accepted", "rejected"):
            return order  # لا نغيّر حالة نهائية
        con.execute("UPDATE orders SET status='rejected' WHERE id = ?", (order_id,))
        return order


def mark_order_accepted(order_id: int) -> None:
    """(قديمة — للتوافق) علّم الطلب مقبولاً"""
    with _conn() as con:
        con.execute(
            "UPDATE orders SET status = 'accepted' WHERE id = ?", (order_id,)
        )


def add_notification(shop_id: int, kind: str, content: str, customer_id: int) -> None:
    """خزّن إشعار زبون (طلب أو استفسار)"""
    with _conn() as con:
        con.execute(
            "INSERT INTO notifications (shop_id, kind, content, customer_id) VALUES (?, ?, ?, ?)",
            (shop_id, kind, content, customer_id),
        )


def get_shop_notifications(shop_id: int, limit: int = 20) -> list:
    """اجلب آخر إشعارات محل معيّن بالأحدث أولاً"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM notifications WHERE shop_id = ? ORDER BY created_at DESC LIMIT ?",
            (shop_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────
# حالة الأدمن (مشتركة بين النسخ وثابتة عبر الإعادة)
# ────────────────────────────────────────────────────────────
def set_admin_mode(admin_id: int, mode: str, test_shop_id: int = None) -> None:
    """احفظ وضع الاختبار للأدمن (UPSERT) — يصفّر last_product عند التبديل"""
    with _conn() as con:
        con.execute(
            """INSERT INTO admin_state (admin_id, mode, test_shop_id, last_product)
               VALUES (?, ?, ?, NULL)
               ON CONFLICT(admin_id) DO UPDATE SET
                   mode         = excluded.mode,
                   test_shop_id = excluded.test_shop_id,
                   last_product = NULL""",
            (admin_id, mode, test_shop_id),
        )


def get_admin_mode(admin_id: int) -> dict:
    """اجلب وضع الأدمن — يُعيد dict دائماً (القيم قد تكون None)"""
    with _conn() as con:
        row = con.execute(
            "SELECT mode, test_shop_id, last_product FROM admin_state WHERE admin_id = ?",
            (admin_id,),
        ).fetchone()
    if row:
        return {
            "mode":         row["mode"],
            "test_shop_id": row["test_shop_id"],
            "last_product": row["last_product"],
        }
    return {"mode": None, "test_shop_id": None, "last_product": None}


def set_admin_last_product(admin_id: int, code: str) -> None:
    """حدّث آخر سلعة استُعلم عنها الزبون"""
    with _conn() as con:
        con.execute(
            """INSERT INTO admin_state (admin_id, last_product)
               VALUES (?, ?)
               ON CONFLICT(admin_id) DO UPDATE SET last_product = excluded.last_product""",
            (admin_id, code),
        )


def clear_admin_mode(admin_id: int) -> None:
    """احذف صف الحالة عند الخروج من وضع الاختبار"""
    with _conn() as con:
        con.execute("DELETE FROM admin_state WHERE admin_id = ?", (admin_id,))


def get_shop_orders(shop_id: int) -> list:
    """اجلب كل طلبات محل معيّن"""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE shop_id = ? ORDER BY created_at DESC",
            (shop_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────
# Instagram shops registry
# ────────────────────────────────────────────────────────────
def add_ig_shop(
    webhook_account_id: str,
    send_account_id: str,
    access_token: str,
    token_expires_at: int,
    owner_telegram_id: int,
    username: str = "",
) -> None:
    """UPSERT an Instagram shop row."""
    import time as _t
    now = int(_t.time())
    with _conn() as con:
        con.execute("""
            INSERT INTO ig_shops
                (webhook_account_id, send_account_id, access_token, token_expires_at,
                 owner_telegram_id, username, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(webhook_account_id) DO UPDATE SET
                send_account_id  = excluded.send_account_id,
                access_token     = excluded.access_token,
                token_expires_at = excluded.token_expires_at,
                owner_telegram_id= excluded.owner_telegram_id,
                username         = excluded.username,
                status           = 'active'
        """, (webhook_account_id, send_account_id, access_token,
              token_expires_at, owner_telegram_id, username, now))


def get_ig_shop_by_webhook_id(webhook_account_id: str) -> Optional[dict]:
    """Look up an active IG shop by the recipient.id seen in webhook events."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM ig_shops WHERE webhook_account_id = ? AND status = 'active'",
            (webhook_account_id,),
        ).fetchone()

    print("SHOP_ROW =", dict(row) if row else None)

    return dict(row) if row else None


def get_ig_shop_by_send_account_id(send_account_id: str) -> Optional[dict]:
    """Fallback lookup by send_account_id — used for self-healing webhook ID mismatch."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM ig_shops WHERE send_account_id = ? AND status = 'active'",
            (send_account_id,)
        ).fetchone()
        return dict(row) if row else None


def get_ig_shop_by_owner(owner_telegram_id: int) -> Optional[dict]:
    """Find an active IG shop by the owner's Telegram chat ID."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM ig_shops WHERE owner_telegram_id = ? AND status = 'active'",
            (owner_telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def get_pending_ig_shops() -> list:
    """محلات ربطت حديثاً وما تأكد لها webhook_account_id الحقيقي بعد."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM ig_shops WHERE webhook_account_id LIKE 'pending:%' AND status = 'active'"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_ig_shop_by_owner(owner_telegram_id: int) -> None:
    """احذف أي صف قديم لنفس المالك قبل إعادة ربط حسابه (يمنع صفوف مكررة)."""
    with _conn() as con:
        con.execute(
            "DELETE FROM ig_shops WHERE owner_telegram_id = ?",
            (owner_telegram_id,)
        )


def update_ig_shop_token(webhook_account_id: str, access_token: str, expires_at: int) -> None:
    """Persist a refreshed long-lived token."""
    with _conn() as con:
        con.execute(
            "UPDATE ig_shops SET access_token = ?, token_expires_at = ? "
            "WHERE webhook_account_id = ?",
            (access_token, expires_at, webhook_account_id)
        )


def update_ig_shop_webhook_id(old_webhook_id: str, new_webhook_id: str) -> None:
    """Self-heal: correct the stored webhook_account_id when IG reports a different one."""
    with _conn() as con:
        con.execute(
            "UPDATE ig_shops SET webhook_account_id = ? WHERE webhook_account_id = ?",
            (new_webhook_id, old_webhook_id)
        )


def list_ig_shops_expiring_before(timestamp: int) -> list:
    """Return active IG shops whose token expires before the given unix timestamp."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM ig_shops WHERE status = 'active' AND token_expires_at < ?",
            (timestamp,)
        ).fetchall()
        return [dict(r) for r in rows]


def seed_ig_shops_from_env() -> None:
    """One-time migration: seed ig_shops from env vars if the table is empty.
    Reads IG_SHOP_ACCOUNT_ID / IG_ACCESS_TOKEN / IG_SHOP_SEND_ACCOUNT_ID /
    IG_SHOP_OWNER_TELEGRAM_ID and the IG_SHOP2_* equivalents.
    Idempotent — safe to call on every startup."""
    with _conn() as con:
        if con.execute("SELECT COUNT(*) FROM ig_shops").fetchone()[0] > 0:
            return

    import time as _t
    now = int(_t.time())
    default_expires = now + 60 * 86400  # 60-day estimate

    candidates = [
        (
            os.environ.get("IG_SHOP_ACCOUNT_ID", ""),
            os.environ.get("IG_SHOP_SEND_ACCOUNT_ID", ""),
            os.environ.get("IG_ACCESS_TOKEN", ""),
            os.environ.get("IG_SHOP_OWNER_TELEGRAM_ID", ""),
        ),
        (
            os.environ.get("IG_SHOP2_ACCOUNT_ID", ""),
            os.environ.get("IG_SHOP2_SEND_ACCOUNT_ID", ""),
            os.environ.get("IG_SHOP2_ACCESS_TOKEN", ""),
            os.environ.get("IG_SHOP2_OWNER_TELEGRAM_ID", ""),
        ),
    ]

    seeded = 0
    for webhook_id, send_id, token, owner_raw in candidates:
        if not (webhook_id and token and owner_raw):
            continue
        try:
            owner_tg = int(owner_raw)  # preserve sign: negative = test shop
        except ValueError:
            logging.error("[IG-SEED] Invalid owner Telegram ID: %s", owner_raw)
            continue
        effective_send = send_id or webhook_id
        with _conn() as con:
            con.execute("""
                INSERT OR IGNORE INTO ig_shops
                    (webhook_account_id, send_account_id, access_token, token_expires_at,
                     owner_telegram_id, username, created_at, status)
                VALUES (?, ?, ?, ?, ?, '', ?, 'active')
            """, (webhook_id, effective_send, token, default_expires, owner_tg, now))
        logging.warning("[IG-SEED] Seeded ig_shop: webhook_id=%s owner_tg=%s",
                        webhook_id, owner_tg)
        seeded += 1

    if seeded == 0:
        logging.warning(
            "[IG-SEED] No shops seeded — set IG_SHOP_ACCOUNT_ID, "
            "IG_ACCESS_TOKEN, IG_SHOP_OWNER_TELEGRAM_ID in Railway"
        )
