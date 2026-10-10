import aiosqlite
import sqlite3
import uuid
import json
from datetime import date, datetime, timezone, timedelta
from access import active_access, service_today
from contextlib import asynccontextmanager
from config import DB_PATH

@asynccontextmanager
async def _db():
    """Открывает короткое соединение с безопасными для SQLite настройками."""
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        await db.close()


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    telegram_username TEXT,
    paid            INTEGER DEFAULT 0,
    demo_used       INTEGER DEFAULT 0,
    purchase_date   TEXT,
    target_kcal     REAL,
    plan_json       TEXT,
    shopping_json   TEXT,
    exclude_tags_json TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    consent_at      TIMESTAMP,
    consent_version TEXT,
    policy_version  TEXT,
    last_payment_id TEXT,
    marketing_consent INTEGER DEFAULT 0,
    marketing_consent_at TEXT,
    marketing_consent_version TEXT,
    marketing_opt_out_at TEXT,
    offer_accepted_at TEXT,
    last_activity_at TEXT,
    purchase_reminder_eligible_at TEXT,
    purchase_reminder_sent_at TEXT,
    purchase_reminder_claimed_at TEXT,
    offer_version TEXT
);
"""

PAYMENTS_SQL = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

PAYMENT_INTENTS_SQL = """
CREATE TABLE IF NOT EXISTS payment_intents (
    user_id INTEGER PRIMARY KEY,
    idempotence_key TEXT NOT NULL,
    payment_id TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Поля, которые могли существовать в старых версиях и больше не нужны.
# SQLite оставляет сами колонки, но при миграции мы очищаем их значения.
LEGACY_SENSITIVE_FIELDS = (
    "username", "age", "height", "weight", "gender", "activity", "goal",
    "allergens", "bmr", "tdee",
)

ALLOWED_FIELDS = {
    "telegram_username", "paid", "demo_used", "purchase_date", "target_kcal", "plan_json",
    "shopping_json", "exclude_tags_json", "consent_at", "consent_version", "policy_version",
    "last_payment_id", "marketing_consent", "marketing_consent_at",
    "marketing_consent_version", "marketing_opt_out_at", "offer_accepted_at",
    "offer_version", "last_activity_at", "purchase_reminder_eligible_at",
    "purchase_reminder_sent_at", "purchase_reminder_claimed_at",
}


async def init_db():
    async with _db() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute(CREATE_SQL)
        await db.execute(PAYMENTS_SQL)
        await db.execute(PAYMENT_INTENTS_SQL)
        await db.execute("CREATE TABLE IF NOT EXISTS payment_checks (payment_id TEXT PRIMARY KEY, checked_at TEXT)")

        # Миграции старых БД: ничего не удаляем из доступа/оплат/планов.
        migrations = (
            ("telegram_username", "TEXT"),
            ("paid", "INTEGER DEFAULT 0"),
            ("demo_used", "INTEGER DEFAULT 0"),
            ("purchase_date", "TEXT"),
            ("target_kcal", "REAL"),
            ("plan_json", "TEXT"),
            ("shopping_json", "TEXT"),
            ("exclude_tags_json", "TEXT"),
            ("created_at", "TIMESTAMP"),
            ("consent_at", "TIMESTAMP"),
            ("consent_version", "TEXT"),
            ("policy_version", "TEXT"),
            ("last_payment_id", "TEXT"),
            ("marketing_consent", "INTEGER DEFAULT 0"),
            ("marketing_consent_at", "TEXT"),
            ("marketing_consent_version", "TEXT"),
            ("marketing_opt_out_at", "TEXT"),
            ("offer_accepted_at", "TEXT"),
            ("offer_version", "TEXT"),
            ("last_activity_at", "TEXT"),
            ("purchase_reminder_eligible_at", "TEXT"),
            ("purchase_reminder_sent_at", "TEXT"),
            ("purchase_reminder_claimed_at", "TEXT"),
        )
        for col, typ in migrations:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

        # Additive migrations only. Never clear client fields during startup.
        for table, columns in {
            "users": (("plan_revision", "INTEGER DEFAULT 0"),),
            "payment_intents": (("request_json", "TEXT"),),
            "payments": (("fulfilled_at", "TEXT"), ("notification_claimed_at", "TEXT"),
                         ("notified_at", "TEXT"), ("access_revision", "INTEGER")),
        }.items():
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                present = {row[1] async for row in cur}
            for col, typ in columns:
                if col not in present:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

        # Индексы под самые частые фоновые выборки и проверки доступа.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_reminders "
            "ON users(paid, marketing_consent, purchase_reminder_sent_at, purchase_reminder_claimed_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_purchase_date "
            "ON users(purchase_date)"
        )
        await db.commit()


async def upsert_user(user_id: int, **fields):
    async with _db() as db:
        if not fields:
            return

        # Жёсткий allow-list: даже внутренняя ошибка в вызывающем коде не
        # позволит записать произвольное поле или старые чувствительные данные.
        fields = {k: v for k, v in fields.items() if k in ALLOWED_FIELDS}
        if not fields:
            return

        await db.execute("BEGIN IMMEDIATE")
        if any(k in fields for k in ("target_kcal", "exclude_tags_json")):
            async with db.execute("SELECT target_kcal, exclude_tags_json FROM users WHERE user_id=?", (user_id,)) as cur:
                prior = await cur.fetchone()
            changed = prior is None or any(
                key in fields and fields[key] != prior[index]
                for index, key in enumerate(("target_kcal", "exclude_tags_json"))
            )
            if changed:
                fields.setdefault("plan_json", None)
                fields.setdefault("shopping_json", None)
                await db.execute("UPDATE users SET plan_revision=COALESCE(plan_revision,0)+1 WHERE user_id=?", (user_id,))
        # Сначала гарантируем наличие строки, затем обновляем только разрешённые поля.
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [user_id]
        await db.execute(f"UPDATE users SET {sets} WHERE user_id=?", vals)
        await db.commit()


async def get_user_by_username(username: str):
    """Ищет зарегистрированного пользователя по Telegram username без @."""
    normalized = (username or "").strip().lstrip("@").lower()
    if not normalized:
        return None
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE lower(telegram_username)=? LIMIT 1",
            (normalized,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user(user_id: int):
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def touch_activity(user_id: int, when: str):
    """Обновляет время последнего входящего действия только для существующего профиля."""
    async with _db() as db:
        await db.execute(
            "UPDATE users SET last_activity_at=? WHERE user_id=?",
            (when, user_id),
        )
        await db.commit()


async def get_due_purchase_reminders(cutoff_iso: str):
    """Возвращает пользователей, которым можно отправить одно напоминание о покупке."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT user_id, last_activity_at, purchase_reminder_eligible_at
               FROM users
              WHERE paid=0
                AND marketing_consent=1
                AND purchase_reminder_sent_at IS NULL
                AND purchase_reminder_claimed_at IS NULL
                AND purchase_reminder_eligible_at IS NOT NULL
                AND last_activity_at IS NOT NULL
                AND last_activity_at <= ?
                AND purchase_reminder_eligible_at <= ?
              ORDER BY last_activity_at ASC""",
            (cutoff_iso, cutoff_iso),
        ) as cur:
            return [dict(row) async for row in cur]


async def get_purchase_reminder_candidate(user_id: int, cutoff_iso: str):
    """Повторно проверяет условия прямо перед отправкой, чтобы свежее действие не получило старое напоминание."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT user_id FROM users
               WHERE user_id=?
                 AND paid=0
                 AND marketing_consent=1
                 AND purchase_reminder_sent_at IS NULL
                 AND purchase_reminder_claimed_at IS NULL
                 AND purchase_reminder_eligible_at IS NOT NULL
                 AND last_activity_at IS NOT NULL
                 AND last_activity_at <= ?
                 AND purchase_reminder_eligible_at <= ?""",
            (user_id, cutoff_iso, cutoff_iso),
        ) as cur:
            return await cur.fetchone()


async def claim_purchase_reminder(user_id: int, when: str, cutoff_iso: str | None = None):
    """Атомарно резервирует единственное напоминание до внешней отправки.

    Это делает механику at-most-once даже при случайном запуске нескольких
    экземпляров бота: после claim другой экземпляр пользователя уже не возьмёт.
    """
    async with _db() as db:
        await db.execute(
            "UPDATE users SET purchase_reminder_claimed_at=? "
            "WHERE user_id=? AND paid=0 AND marketing_consent=1 "
            "AND purchase_reminder_sent_at IS NULL AND purchase_reminder_claimed_at IS NULL "
            "AND last_activity_at <= ? AND purchase_reminder_eligible_at <= ?",
            (when, user_id, cutoff_iso or when, cutoff_iso or when),
        )
        changed = db.total_changes > 0
        await db.commit()
        return changed


async def mark_purchase_reminder_sent(user_id: int, when: str):
    """Фиксирует факт успешной отправки напоминания, не создавая профиль."""
    async with _db() as db:
        await db.execute(
            "UPDATE users SET purchase_reminder_sent_at=? WHERE user_id=? AND purchase_reminder_sent_at IS NULL",
            (when, user_id),
        )
        changed = db.total_changes > 0
        await db.commit()
        return changed


async def claim_payment(user_id: int, payment_id: str, provider: str) -> bool:
    """Совместимость со старым Telegram Payments-контуром.

    Production checkout использует YooKassa и complete_yookassa_payment(),
    где payment + access фиксируются одной транзакцией.
    """
    if not payment_id:
        return False
    async with _db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO payments(payment_id, user_id, provider) VALUES (?, ?, ?)",
            (str(payment_id), user_id, provider),
        )
        changed = db.total_changes > 0
        await db.commit()
        return changed


async def get_or_create_yookassa_intent(user_id: int):
    """Атомарно резервирует один checkout на пользователя.

    Сохраняемый Idempotence-Key позволяет безопасно повторить POST в YooKassa,
    если ответ потерян, и не создавать второй платёж из-за двойного клика/таймаута.
    Проверка активного доступа выполняется внутри той же транзакции, что и резерв.
    """
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT paid, purchase_date FROM users WHERE user_id=?", (user_id,)) as cur:
            user = await cur.fetchone()
        if active_access(dict(user) if user else None):
            await db.rollback()
            raise ValueError("У пользователя уже есть активный доступ")

        async with db.execute(
            "SELECT user_id, idempotence_key, payment_id, status, created_at, request_json FROM payment_intents WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()

        if row and row[3] == "pending":
            await db.commit()
            return {"user_id": row[0], "idempotence_key": row[1], "payment_id": row[2], "status": row[3], "created_at": row[4], "request": json.loads(row[5]) if row[5] else None}

        key = str(uuid.uuid4())
        from config import YOOKASSA_RETURN_URL
        from payment_service import payment_payload
        request = payment_payload(user_id, YOOKASSA_RETURN_URL, key)
        await db.execute("DELETE FROM payment_intents WHERE user_id=?", (user_id,))
        await db.execute(
            "INSERT INTO payment_intents(user_id, idempotence_key, status, request_json) VALUES (?, ?, 'pending', ?)",
            (user_id, key, json.dumps(request)),
        )
        await db.commit()
        return {"user_id": user_id, "idempotence_key": key, "payment_id": None, "status": "pending", "request": request, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}


async def set_yookassa_intent_payment(user_id: int, payment_id: str, idempotence_key: str | None = None):
    if not payment_id:
        return False
    async with _db() as db:
        await db.execute(
            "UPDATE payment_intents SET payment_id=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND status='pending' "
            "AND (payment_id IS NULL OR payment_id=?) AND (? IS NULL OR idempotence_key=?)",
            (str(payment_id), user_id, str(payment_id), idempotence_key, idempotence_key),
        )
        changed = db.total_changes > 0
        await db.commit()
        return changed


async def complete_yookassa_payment(user_id: int, payment_id: str,
                                    purchase_date: str, offer_accepted_at: str,
                                    offer_version: str):
    """One irreversible credit per payment, including after expiry/revocation."""
    if not payment_id:
        raise ValueError("payment_id обязателен")
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)) as cur:
            existing = await cur.fetchone()
        if existing:
            if existing["user_id"] != user_id or existing["provider"] != "yookassa":
                raise ValueError("Платёж принадлежит другому владельцу/провайдеру")
            await db.commit()
            return {"new_purchase": False, "already_recorded": True}
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            user = await cur.fetchone()
        if not user:
            raise ValueError("Пользователь не найден")
        async with db.execute("SELECT status,payment_id FROM payment_intents WHERE user_id=?", (user_id,)) as cur:
            intent = await cur.fetchone()
        if not intent or intent["status"] != "pending" or (intent["payment_id"] is not None and intent["payment_id"] != payment_id):
            raise ValueError("Заказ отменён, отозван или изменился")
        is_new = not active_access(dict(user))
        revision = (user["plan_revision"] or 0) + 1
        await db.execute(
            "INSERT INTO payments(payment_id,user_id,provider,access_revision) VALUES (?,?,'yookassa',?)",
            (payment_id,user_id,revision),
        )
        await db.execute(
            "UPDATE users SET paid=1,last_payment_id=?,offer_accepted_at=?,offer_version=?,"
            "purchase_date=?,plan_json=CASE WHEN ? THEN NULL ELSE plan_json END,"
            "shopping_json=CASE WHEN ? THEN NULL ELSE shopping_json END,plan_revision=? "
            "WHERE user_id=?",
            (payment_id,offer_accepted_at,offer_version,
             purchase_date if is_new else user["purchase_date"],is_new,is_new,revision,user_id),
        )
        await db.execute(
            "UPDATE payment_intents SET payment_id=?,status='succeeded',updated_at=CURRENT_TIMESTAMP "
            "WHERE user_id=? AND status='pending' AND (payment_id IS NULL OR payment_id=?)",
            (payment_id,user_id,payment_id),
        )
        await db.commit()
        return {"new_purchase": is_new, "already_recorded": False}


async def get_yookassa_intent(user_id):
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM payment_intents WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_yookassa_intent_status(user_id, payment_id, status):
    if status not in {"canceled", "succeeded"}: raise ValueError("Invalid terminal status")
    async with _db() as db:
        await db.execute("UPDATE payment_intents SET status=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND payment_id=? AND status='pending'", (status,user_id,payment_id))
        await db.commit()


async def get_payment(payment_id):
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def save_plan_if_current(user_id, revision, plan_json, shopping_json):
    async with _db() as db:
        await db.execute("UPDATE users SET plan_json=?,shopping_json=? WHERE user_id=? AND plan_revision=? AND paid=1", (plan_json,shopping_json,user_id,revision))
        changed=db.total_changes > 0
        await db.commit()
        return changed


async def claim_payment_notification(payment_id):
    async with _db() as db:
        await db.execute("UPDATE payments SET notification_claimed_at=CURRENT_TIMESTAMP WHERE payment_id=? AND notified_at IS NULL AND notification_claimed_at IS NULL AND access_revision IS NOT NULL", (payment_id,))
        changed=db.total_changes > 0
        await db.commit()
        return changed


async def finish_payment_notification(payment_id, sent):
    async with _db() as db:
        if sent:
            await db.execute("UPDATE payments SET notified_at=CURRENT_TIMESTAMP WHERE payment_id=?", (payment_id,))
        else:
            await db.execute("UPDATE payments SET notification_claimed_at=NULL WHERE payment_id=? AND notified_at IS NULL", (payment_id,))
        await db.commit()


async def mark_payment_fulfilled(payment_id):
    async with _db() as db:
        await db.execute("UPDATE payments SET fulfilled_at=COALESCE(fulfilled_at,CURRENT_TIMESTAMP) WHERE payment_id=?", (payment_id,))
        await db.commit()


async def revoke_access(user_id):
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE users SET paid=0,plan_json=NULL,shopping_json=NULL,target_kcal=NULL,plan_revision=COALESCE(plan_revision,0)+1,purchase_reminder_eligible_at=NULL WHERE user_id=?", (user_id,))
        await db.execute("UPDATE payment_intents SET status='revoked',updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND status='pending'", (user_id,))
        await db.commit()


async def delete_user_data(user_id: int):
    """Удаляет профиль и сохранённые пользовательские данные из локальной БД."""
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("DELETE FROM payment_intents WHERE user_id=?", (user_id,))
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fsm'") as cur:
            has_fsm = await cur.fetchone()
        if has_fsm:
            import json
            async with db.execute("SELECT key FROM fsm") as cur:
                keys = [row[0] async for row in cur if json.loads(row[0])[2] == user_id]
            for key in keys:
                await db.execute("DELETE FROM fsm WHERE key=?", (key,))
        await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        changed = db.total_changes > 0
        await db.commit()
        return changed


async def revoke_access_for_grant(user_id, purchase_date):
    """New manual/test period, preserving registration and calculation."""
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE users SET paid=1,purchase_date=?,plan_json=NULL,shopping_json=NULL,last_payment_id=NULL,plan_revision=COALESCE(plan_revision,0)+1 WHERE user_id=?", (purchase_date,user_id))
        await db.execute("UPDATE payment_intents SET status='superseded',updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND status='pending'", (user_id,))
        await db.commit()


async def get_recoverable_payment_ids(limit=100):
    async with _db() as db:
        async with db.execute("""WITH candidates AS (
            SELECT payment_id FROM payment_intents WHERE status='pending' AND payment_id IS NOT NULL
            UNION SELECT p.payment_id FROM payments p JOIN users u ON u.user_id=p.user_id
            WHERE p.provider='yookassa' AND p.access_revision IS NOT NULL AND u.paid=1
              AND u.last_payment_id=p.payment_id
              AND (p.fulfilled_at IS NULL OR (p.notified_at IS NULL AND p.notification_claimed_at IS NULL))
        ) SELECT c.payment_id FROM candidates c LEFT JOIN payment_checks pc ON pc.payment_id=c.payment_id
          ORDER BY COALESCE(pc.checked_at,'') LIMIT ?""", (limit,)) as cur:
            return [row[0] async for row in cur]


async def mark_payment_checked(payment_id):
    async with _db() as db:
        await db.execute("INSERT INTO payment_checks(payment_id,checked_at) VALUES (?,CURRENT_TIMESTAMP) ON CONFLICT(payment_id) DO UPDATE SET checked_at=CURRENT_TIMESTAMP", (payment_id,))
        await db.commit()
