import os
import json
import logging
import sqlite3
import random
import string
from contextlib import contextmanager
from typing import Optional

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
        """)

        # أضف الأعمدة الجديدة آمناً على قواعد بيانات قائمة
        existing = {row["name"] for row in con.execute("PRAGMA table_info(orders)").fetchall()}
        if "customer_chat_id" not in existing:
            con.execute("ALTER TABLE orders ADD COLUMN customer_chat_id INTEGER")
        if "status" not in existing:
            con.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")

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
        return bool(affected)


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
) -> int:
    """سجّل طلب زبون جديد ويُعيد id الطلب"""
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO orders
               (shop_id, product_code, customer_name, customer_phone,
                customer_address, customer_chat_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (shop_id, product_code, customer_name, customer_phone,
             customer_address, customer_chat_id),
        )
        return cur.lastrowid


def get_order(order_id: int) -> Optional[dict]:
    """اجلب طلباً بمعرّفه"""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def mark_order_accepted(order_id: int) -> None:
    """علّم الطلب مقبولاً"""
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
