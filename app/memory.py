from __future__ import annotations

import asyncio
import calendar
import json
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiosqlite

from .account import AccountRecord
from .adult_safety import ADULT_TEXT_MODES
from .media_types import (
    MediaJob,
    MediaJobReservation,
    MediaQuotaDecision,
    clean_media_kind,
    media_settings_from_json,
    safe_media_job_payload,
    utc_day_key,
)


SCHEMA_VERSION = 9
# claim_proactive_lease accepts cooldowns up to seven days. Keep one extra UTC
# day so cleanup near a day boundary cannot erase a still-enforced cooldown.
PROACTIVE_STATE_MIN_RETENTION_SECONDS = 8 * 24 * 60 * 60


@dataclass(frozen=True)
class MemoryMessage:
    account_id: str
    group_id: int
    sender_id: int
    sender_name: str
    role: str
    content: str
    created_at: int


@dataclass(frozen=True)
class ConversationLogEntry:
    created_at: int
    sender_name: str
    role: str
    content: str


@dataclass(frozen=True)
class PrivateAlertEntry:
    alert_id: str
    sender_name: str
    preview: str
    created_at: int
    acknowledged: bool


@dataclass(frozen=True)
class ProactiveLeaseDecision:
    allowed: bool
    reason: str
    lease_token: str = ""
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class GroupActivityProfile:
    recent_messages: int
    recent_participants: int
    trailing_messages: int
    trailing_participants: int
    latest_message_at: int | None


class MemoryStore:
    def __init__(self, path: str, ttl_hours: int) -> None:
        self.path = path
        self.ttl_seconds = ttl_hours * 60 * 60
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._group_activity_cache: dict[int, int] = {}

    async def open(self) -> None:
        parent = Path(self.path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._migrate_messages()
        await self._create_management_tables()
        await self._migrate_account_policy_columns()
        await self._db.commit()
        await self._repair_management_foreign_keys()
        await self.repair_retired_grok_model()
        await self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._db.commit()
        await self.purge_expired()

    async def _migrate_messages(self) -> None:
        db = self._connection()
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        exists = await cursor.fetchone()
        await cursor.close()
        if not exists:
            await db.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    group_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
        else:
            cursor = await db.execute("PRAGMA table_info(messages)")
            columns = {str(row["name"]) for row in await cursor.fetchall()}
            await cursor.close()
            if "account_id" not in columns:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute(
                        """
                        CREATE TABLE messages_v2 (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            account_id TEXT NOT NULL,
                            group_id INTEGER NOT NULL,
                            sender_id INTEGER NOT NULL,
                            sender_name TEXT NOT NULL,
                            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                            content TEXT NOT NULL,
                            created_at INTEGER NOT NULL
                        )
                        """
                    )
                    await db.execute(
                        """
                        INSERT INTO messages_v2
                            (id, account_id, group_id, sender_id, sender_name, role,
                             content, created_at)
                        SELECT id, 'primary', group_id, sender_id, sender_name, role,
                               content, created_at
                        FROM messages
                        """
                    )
                    await db.execute("DROP TABLE messages")
                    await db.execute("ALTER TABLE messages_v2 RENAME TO messages")
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_account_group_time "
            "ON messages (account_id, group_id, created_at)"
        )

    async def _create_management_tables(self) -> None:
        db = self._connection()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                session_ciphertext TEXT NOT NULL,
                session_fingerprint TEXT NOT NULL UNIQUE,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                telegram_name TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                gender TEXT NOT NULL,
                stage TEXT NOT NULL,
                style TEXT NOT NULL,
                task_name TEXT NOT NULL,
                task_info TEXT NOT NULL,
                ai_base_url TEXT NOT NULL,
                ai_model TEXT NOT NULL,
                ai_api_key_ciphertext TEXT NOT NULL,
                group_reply_probability REAL NOT NULL,
                reply_on_mention INTEGER NOT NULL,
                reply_on_reply INTEGER NOT NULL,
                typing_delay_min_seconds REAL NOT NULL,
                typing_delay_max_seconds REAL NOT NULL,
                proactive_enabled INTEGER NOT NULL,
                proactive_idle_minutes INTEGER NOT NULL,
                proactive_min_interval_minutes INTEGER NOT NULL,
                proactive_max_interval_minutes INTEGER NOT NULL,
                max_proactive_per_day INTEGER NOT NULL,
                all_groups INTEGER NOT NULL,
                group_ids TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                blocked_terms TEXT NOT NULL DEFAULT '[]',
                blocked_topics TEXT NOT NULL DEFAULT '[]',
                media_settings TEXT NOT NULL DEFAULT '{}',
                adult_text_enabled INTEGER NOT NULL DEFAULT 0,
                adult_text_mode TEXT NOT NULL DEFAULT 'auto'
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT,
                action TEXT NOT NULL,
                fields TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS media_usage (
                account_id TEXT NOT NULL,
                media_type TEXT NOT NULL
                    CHECK (media_type IN ('image', 'voice', 'video')),
                day_key TEXT NOT NULL,
                used_count INTEGER NOT NULL,
                last_reserved_at INTEGER NOT NULL,
                PRIMARY KEY (account_id, media_type),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS media_jobs (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                media_type TEXT NOT NULL
                    CHECK (media_type IN ('image', 'voice', 'video')),
                status TEXT NOT NULL
                    CHECK (
                        status IN (
                            'queued', 'running', 'completed', 'failed', 'cancelled'
                        )
                    ),
                payload TEXT NOT NULL,
                result_ref TEXT NOT NULL,
                error TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                available_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS private_alerts (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                sender_fingerprint TEXT NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                preview TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                acknowledged_at INTEGER,
                UNIQUE (
                    account_id, sender_fingerprint, telegram_message_id
                ),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reply_claims (
                group_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, telegram_message_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reply_claims_v2 (
                group_id INTEGER NOT NULL,
                claim_key TEXT NOT NULL,
                account_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, claim_key)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reply_claims_v3 (
                group_id INTEGER NOT NULL,
                claim_key TEXT NOT NULL,
                slot INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, claim_key, slot),
                UNIQUE (group_id, claim_key, account_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS proactive_group_state (
                group_id INTEGER PRIMARY KEY,
                last_activity_at INTEGER NOT NULL,
                last_proactive_at INTEGER NOT NULL,
                lease_owner TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                lease_until INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS proactive_usage (
                account_id TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                day_key TEXT NOT NULL,
                used_count INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (account_id, group_id, day_key),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reply_claims_created_at
            ON reply_claims (created_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reply_claims_v2_created_at
            ON reply_claims_v2 (created_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_private_alerts_account_status_time
            ON private_alerts (
                account_id, acknowledged_at, created_at DESC, id DESC
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_proactive_usage_updated_at
            ON proactive_usage (updated_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_media_jobs_queue
            ON media_jobs (status, available_at, created_at, id)
            """
        )

    async def _migrate_account_policy_columns(self) -> None:
        db = self._connection()
        cursor = await db.execute("PRAGMA table_info(accounts)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        await cursor.close()
        if "blocked_terms" not in columns:
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN "
                "blocked_terms TEXT NOT NULL DEFAULT '[]'"
            )
        if "blocked_topics" not in columns:
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN "
                "blocked_topics TEXT NOT NULL DEFAULT '[]'"
            )
        if "media_settings" not in columns:
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN "
                "media_settings TEXT NOT NULL DEFAULT '{}'"
            )
        if "adult_text_enabled" not in columns:
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN "
                "adult_text_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "adult_text_mode" not in columns:
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN "
                "adult_text_mode TEXT NOT NULL DEFAULT 'auto'"
            )
            await db.execute(
                "UPDATE accounts SET adult_text_mode = "
                "CASE WHEN adult_text_enabled = 1 THEN 'auto' ELSE 'auto' END"
            )
        if "account_state" not in columns:
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN "
                "account_state TEXT NOT NULL DEFAULT ''"
            )
        valid_modes = tuple(ADULT_TEXT_MODES)
        placeholders = ", ".join("?" for _ in valid_modes)
        await db.execute(
            f"""
            UPDATE accounts
            SET adult_text_mode = CASE
                WHEN lower(trim(adult_text_mode)) IN ({placeholders})
                    THEN lower(trim(adult_text_mode))
                WHEN adult_text_enabled = 1 THEN 'auto'
                ELSE 'strict'
            END
            """,
            valid_modes,
        )
        await db.execute(
            """
            UPDATE accounts
            SET adult_text_enabled = CASE
                1
            END
            """
        )

    async def _repair_management_foreign_keys(self) -> None:
        """Repair foreign keys rewritten by legacy account-table migrations."""
        db = self._connection()
        broken_tables: list[str] = []
        for table in (
            "media_usage",
            "media_jobs",
            "private_alerts",
            "proactive_usage",
        ):
            cursor = await db.execute(f"PRAGMA foreign_key_list({table})")
            targets = {str(row["table"]) for row in await cursor.fetchall()}
            await cursor.close()
            if targets and targets != {"accounts"}:
                broken_tables.append(table)
        if not broken_tables:
            return

        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("BEGIN IMMEDIATE")
        try:
            if "media_usage" in broken_tables:
                await db.execute(
                    "ALTER TABLE media_usage RENAME TO media_usage_legacy_fk"
                )
                await db.execute(
                    """
                    CREATE TABLE media_usage (
                        account_id TEXT NOT NULL,
                        media_type TEXT NOT NULL
                            CHECK (media_type IN ('image', 'voice', 'video')),
                        day_key TEXT NOT NULL,
                        used_count INTEGER NOT NULL,
                        last_reserved_at INTEGER NOT NULL,
                        PRIMARY KEY (account_id, media_type),
                        FOREIGN KEY (account_id) REFERENCES accounts(id)
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO media_usage
                        (account_id, media_type, day_key, used_count,
                         last_reserved_at)
                    SELECT account_id, media_type, day_key, used_count,
                           last_reserved_at
                    FROM media_usage_legacy_fk
                    WHERE account_id IN (SELECT id FROM accounts)
                    """
                )
                await db.execute("DROP TABLE media_usage_legacy_fk")

            if "media_jobs" in broken_tables:
                await db.execute(
                    "ALTER TABLE media_jobs RENAME TO media_jobs_legacy_fk"
                )
                await db.execute(
                    """
                    CREATE TABLE media_jobs (
                        id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        group_id INTEGER NOT NULL,
                        media_type TEXT NOT NULL
                            CHECK (media_type IN ('image', 'voice', 'video')),
                        status TEXT NOT NULL
                            CHECK (
                                status IN (
                                    'queued', 'running', 'completed',
                                    'failed', 'cancelled'
                                )
                            ),
                        payload TEXT NOT NULL,
                        result_ref TEXT NOT NULL,
                        error TEXT NOT NULL,
                        attempts INTEGER NOT NULL,
                        available_at INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        FOREIGN KEY (account_id) REFERENCES accounts(id)
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO media_jobs
                        (id, account_id, group_id, media_type, status, payload,
                         result_ref, error, attempts, available_at, created_at,
                         updated_at)
                    SELECT id, account_id, group_id, media_type, status, payload,
                           result_ref, error, attempts, available_at, created_at,
                           updated_at
                    FROM media_jobs_legacy_fk
                    WHERE account_id IN (SELECT id FROM accounts)
                    """
                )
                await db.execute("DROP TABLE media_jobs_legacy_fk")
                await db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_media_jobs_queue
                    ON media_jobs (status, available_at, created_at, id)
                    """
                )
            if "private_alerts" in broken_tables:
                await db.execute(
                    "ALTER TABLE private_alerts RENAME TO private_alerts_legacy_fk"
                )
                await db.execute(
                    "DROP INDEX IF EXISTS idx_private_alerts_account_status_time"
                )
                await db.execute(
                    """
                    CREATE TABLE private_alerts (
                        id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        sender_fingerprint TEXT NOT NULL,
                        telegram_message_id INTEGER NOT NULL,
                        sender_name TEXT NOT NULL,
                        preview TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        acknowledged_at INTEGER,
                        UNIQUE (
                            account_id, sender_fingerprint,
                            telegram_message_id
                        ),
                        FOREIGN KEY (account_id) REFERENCES accounts(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO private_alerts (
                        id, account_id, sender_fingerprint,
                        telegram_message_id, sender_name, preview,
                        created_at, acknowledged_at
                    )
                    SELECT id, account_id, sender_fingerprint,
                           telegram_message_id, sender_name, preview,
                           created_at, acknowledged_at
                    FROM private_alerts_legacy_fk
                    WHERE account_id IN (SELECT id FROM accounts)
                    """
                )
                await db.execute("DROP TABLE private_alerts_legacy_fk")
                await db.execute(
                    """
                    CREATE INDEX idx_private_alerts_account_status_time
                    ON private_alerts (
                        account_id, acknowledged_at, created_at DESC, id DESC
                    )
                    """
                )
            if "proactive_usage" in broken_tables:
                await db.execute(
                    "ALTER TABLE proactive_usage RENAME TO proactive_usage_legacy_fk"
                )
                await db.execute(
                    "DROP INDEX IF EXISTS idx_proactive_usage_updated_at"
                )
                await db.execute(
                    """
                    CREATE TABLE proactive_usage (
                        account_id TEXT NOT NULL,
                        group_id INTEGER NOT NULL,
                        day_key TEXT NOT NULL,
                        used_count INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (account_id, group_id, day_key),
                        FOREIGN KEY (account_id) REFERENCES accounts(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO proactive_usage (
                        account_id, group_id, day_key, used_count, updated_at
                    )
                    SELECT account_id, group_id, day_key, used_count, updated_at
                    FROM proactive_usage_legacy_fk
                    WHERE account_id IN (SELECT id FROM accounts)
                    """
                )
                await db.execute("DROP TABLE proactive_usage_legacy_fk")
                await db.execute(
                    """
                    CREATE INDEX idx_proactive_usage_updated_at
                    ON proactive_usage (updated_at)
                    """
                )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        finally:
            await db.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("MemoryStore is not open")
        return self._db

    @staticmethod
    def _clean_private_alert_text(
        value: object,
        *,
        limit: int,
        fallback: str,
    ) -> str:
        text = str(value or "")
        text = "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in text
        )
        cleaned = " ".join(text.split())[:limit].strip()
        return cleaned or fallback

    @staticmethod
    def _account_from_row(row: aiosqlite.Row) -> AccountRecord:
        try:
            group_ids = frozenset(int(item) for item in json.loads(row["group_ids"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            group_ids = frozenset()
        blocked_terms = MemoryStore._policy_values(row, "blocked_terms")
        blocked_topics = MemoryStore._policy_values(row, "blocked_topics")
        try:
            media_settings = media_settings_from_json(row["media_settings"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Stored media settings are invalid") from exc
        return AccountRecord(
            id=str(row["id"]),
            label=str(row["label"]),
            session_ciphertext=str(row["session_ciphertext"]),
            session_fingerprint=str(row["session_fingerprint"]),
            telegram_user_id=int(row["telegram_user_id"]),
            telegram_name=str(row["telegram_name"]),
            enabled=bool(row["enabled"]),
            gender=str(row["gender"]),
            stage=str(row["stage"]),
            style=str(row["style"]),
            task_name=str(row["task_name"]),
            task_info=str(row["task_info"]),
            ai_base_url=str(row["ai_base_url"]),
            ai_model=str(row["ai_model"]),
            ai_api_key_ciphertext=str(row["ai_api_key_ciphertext"]),
            group_reply_probability=float(row["group_reply_probability"]),
            reply_on_mention=bool(row["reply_on_mention"]),
            reply_on_reply=bool(row["reply_on_reply"]),
            typing_delay_min_seconds=float(row["typing_delay_min_seconds"]),
            typing_delay_max_seconds=float(row["typing_delay_max_seconds"]),
            proactive_enabled=bool(row["proactive_enabled"]),
            proactive_idle_minutes=int(row["proactive_idle_minutes"]),
            proactive_min_interval_minutes=int(row["proactive_min_interval_minutes"]),
            proactive_max_interval_minutes=int(row["proactive_max_interval_minutes"]),
            max_proactive_per_day=int(row["max_proactive_per_day"]),
            all_groups=bool(row["all_groups"]),
            group_ids=group_ids,
            revision=int(row["revision"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            blocked_terms=blocked_terms,
            blocked_topics=blocked_topics,
            media_settings=media_settings,
            adult_text_enabled=bool(row["adult_text_enabled"]),
            adult_text_mode=str(row["adult_text_mode"]),
            account_state=str(row["account_state"] or ""),
        )

    @staticmethod
    def _policy_values(
        row: aiosqlite.Row,
        column: str,
    ) -> tuple[str, ...]:
        try:
            decoded = json.loads(row[column])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Stored blocked content policy is invalid") from exc
        if not isinstance(decoded, list) or any(
            not isinstance(item, str) for item in decoded
        ):
            raise RuntimeError("Stored blocked content policy is invalid")
        return tuple(decoded)

    async def count_accounts(self) -> int:
        async with self._lock:
            cursor = await self._connection().execute("SELECT COUNT(*) AS total FROM accounts")
            row = await cursor.fetchone()
            await cursor.close()
        return int(row["total"]) if row else 0

    async def list_accounts(self) -> list[AccountRecord]:
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT * FROM accounts ORDER BY created_at, id"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._account_from_row(row) for row in rows]

    async def get_account(self, account_id: str) -> AccountRecord | None:
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT * FROM accounts WHERE id = ?",
                (account_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._account_from_row(row) if row else None

    async def create_account(self, record: AccountRecord) -> None:
        async with self._lock:
            db = self._connection()
            await db.execute(
                f"""
                INSERT INTO accounts (
                    id, label, session_ciphertext, session_fingerprint,
                    telegram_user_id, telegram_name, enabled, gender, stage,
                    style, task_name, task_info, ai_base_url, ai_model,
                    ai_api_key_ciphertext, group_reply_probability,
                    reply_on_mention, reply_on_reply, typing_delay_min_seconds,
                    typing_delay_max_seconds, proactive_enabled,
                    proactive_idle_minutes, proactive_min_interval_minutes,
                    proactive_max_interval_minutes, max_proactive_per_day,
                    all_groups, group_ids, revision, created_at, updated_at,
                    blocked_terms, blocked_topics, media_settings,
                    adult_text_enabled, adult_text_mode, account_state
                ) VALUES (
                    {", ".join("?" for _ in self._account_values(record))}
                )
                """,
                self._account_values(record),
            )
            await self._audit_locked(record.id, "account_created", ["account"])
            await db.commit()

    async def adopt_legacy_messages(self, account_id: str) -> int:
        """Attach pre-upgrade messages when no legacy `primary` account exists."""
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                UPDATE messages
                SET account_id = ?
                WHERE account_id = 'primary'
                  AND NOT EXISTS (
                      SELECT 1 FROM accounts WHERE id = 'primary'
                  )
                """,
                (account_id,),
            )
            adopted = max(int(cursor.rowcount), 0)
            await cursor.close()
            await db.commit()
        return adopted

    @staticmethod
    def _account_values(record: AccountRecord) -> tuple[object, ...]:
        return (
            record.id,
            record.label,
            record.session_ciphertext,
            record.session_fingerprint,
            record.telegram_user_id,
            record.telegram_name,
            int(record.enabled),
            record.gender,
            record.stage,
            record.style,
            record.task_name,
            record.task_info,
            record.ai_base_url,
            record.ai_model,
            record.ai_api_key_ciphertext,
            record.group_reply_probability,
            int(record.reply_on_mention),
            int(record.reply_on_reply),
            record.typing_delay_min_seconds,
            record.typing_delay_max_seconds,
            int(record.proactive_enabled),
            record.proactive_idle_minutes,
            record.proactive_min_interval_minutes,
            record.proactive_max_interval_minutes,
            record.max_proactive_per_day,
            int(record.all_groups),
            json.dumps(sorted(record.group_ids)),
            record.revision,
            record.created_at,
            record.updated_at,
            json.dumps(list(record.blocked_terms), ensure_ascii=False),
            json.dumps(list(record.blocked_topics), ensure_ascii=False),
            json.dumps(
                record.media_settings.public_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            int(record.adult_text_enabled),
            record.adult_text_mode,
            record.account_state,
        )

    async def update_account(
        self,
        record: AccountRecord,
        *,
        expected_revision: int,
        changed_fields: list[str],
    ) -> AccountRecord:
        next_revision = expected_revision + 1
        updated = record.with_updates(revision=next_revision, updated_at=int(time.time()))
        values = self._account_values(updated)
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                UPDATE accounts SET
                    label=?, session_ciphertext=?, session_fingerprint=?,
                    telegram_user_id=?, telegram_name=?, enabled=?, gender=?,
                    stage=?, style=?, task_name=?, task_info=?, ai_base_url=?,
                    ai_model=?, ai_api_key_ciphertext=?,
                    group_reply_probability=?, reply_on_mention=?,
                    reply_on_reply=?, typing_delay_min_seconds=?,
                    typing_delay_max_seconds=?, proactive_enabled=?,
                    proactive_idle_minutes=?, proactive_min_interval_minutes=?,
                    proactive_max_interval_minutes=?, max_proactive_per_day=?,
                    all_groups=?, group_ids=?, revision=?, created_at=?,
                    updated_at=?, blocked_terms=?, blocked_topics=?,
                    media_settings=?, adult_text_enabled=?, adult_text_mode=?,
                    account_state=?
                WHERE id=? AND revision=?
                """,
                (
                    *values[1:],
                    updated.id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise RuntimeError("Account settings changed in another request")
            await self._audit_locked(updated.id, "account_updated", changed_fields)
            await db.commit()
        return updated

    async def update_account_state(
        self,
        account_id: str,
        state: dict[str, object],
    ) -> None:
        """持久化账号人格状态（偏好/记忆/情绪），不触发 revision 冲突。"""
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                "UPDATE accounts SET account_state = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    int(time.time()),
                    account_id,
                ),
            )
            await db.commit()
            await cursor.close()

    async def clear_account_api_keys(self) -> int:
        """Remove legacy per-account provider keys without ever decrypting them."""
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                UPDATE accounts
                SET ai_api_key_ciphertext=''
                WHERE ai_api_key_ciphertext <> ''
                """
            )
            await db.commit()
            return max(0, int(cursor.rowcount))

    async def repair_retired_grok_model(self) -> int:
        """Replace only the retired Grok default generated by this project."""
        retired_model = "x-ai/grok-4.1-fast"
        replacement_model = "x-ai/grok-4.3"

        def is_project_openrouter_url(value: object) -> bool:
            try:
                parsed = urlsplit(str(value or ""))
                port = parsed.port
            except ValueError:
                return False
            return (
                parsed.scheme.lower() == "https"
                and (parsed.hostname or "").lower() == "openrouter.ai"
                and port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and parsed.path.rstrip("/") == "/api/v1"
                and not parsed.query
                and not parsed.fragment
            )

        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT id, ai_base_url FROM accounts
                    WHERE ai_model=?
                    ORDER BY id
                    """,
                    (retired_model,),
                )
                rows = [
                    row
                    for row in await cursor.fetchall()
                    if is_project_openrouter_url(row["ai_base_url"])
                ]
                await cursor.close()
                if not rows:
                    await db.commit()
                    return 0

                now = int(time.time())
                for row in rows:
                    account_id = str(row["id"])
                    cursor = await db.execute(
                        """
                        UPDATE accounts
                        SET ai_model=?, revision=revision+1, updated_at=?
                        WHERE id=? AND ai_base_url=? AND ai_model=?
                        """,
                        (
                            replacement_model,
                            now,
                            account_id,
                            str(row["ai_base_url"]),
                            retired_model,
                        ),
                    )
                    changed = int(cursor.rowcount)
                    await cursor.close()
                    if changed != 1:
                        raise RuntimeError(
                            "Account changed during retired Grok model repair"
                        )
                    await self._audit_locked(
                        account_id,
                        "repair_retired_grok_model",
                        ["ai_model"],
                    )
                await db.commit()
                return len(rows)
            except BaseException:
                await db.rollback()
                raise

    async def migrate_existing_accounts_to_grok_adult(self) -> int:
        """Apply the explicit operator-requested Grok adult-text migration.

        This updates each non-conforming existing account exactly once and
        records an audit entry for the fields that changed. It never reads or
        writes provider credentials and does not alter media settings or any
        other account policy.
        """
        target_base_url = "https://openrouter.ai/api/v1"
        target_model = "x-ai/grok-4.20"
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT id, ai_base_url, ai_model, adult_text_enabled, adult_text_mode
                    FROM accounts
                    WHERE ai_base_url <> ?
                       OR ai_model <> ?
                       OR adult_text_enabled <> 1
                       OR adult_text_mode <> 'auto'
                    ORDER BY id
                    """,
                    (target_base_url, target_model),
                )
                try:
                    rows = await cursor.fetchall()
                finally:
                    await cursor.close()

                now = int(time.time())
                for row in rows:
                    changed_fields: list[str] = []
                    if str(row["ai_base_url"]) != target_base_url:
                        changed_fields.append("ai_base_url")
                    if str(row["ai_model"]) != target_model:
                        changed_fields.append("ai_model")
                    if int(row["adult_text_enabled"]) != 1:
                        changed_fields.append("adult_text_enabled")
                    if str(row["adult_text_mode"]) != "auto":
                        changed_fields.append("adult_text_mode")

                    cursor = await db.execute(
                        """
                        UPDATE accounts
                        SET ai_base_url=?, ai_model=?, adult_text_enabled=1,
                            adult_text_mode='auto',
                            revision=revision+1, updated_at=?
                        WHERE id=?
                        """,
                        (
                            target_base_url,
                            target_model,
                            now,
                            str(row["id"]),
                        ),
                    )
                    updated_rows = int(cursor.rowcount)
                    await cursor.close()
                    if updated_rows != 1:
                        raise RuntimeError(
                            "Account changed during Grok adult migration"
                        )
                    await self._audit_locked(
                        str(row["id"]),
                        "migrate_existing_accounts_to_grok_adult",
                        changed_fields,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            return len(rows)

    async def migrate_openrouter_defaults(
        self,
        *,
        ai_base_url: str,
        ai_model: str,
        image_model: str,
        tts_model: str,
        video_model: str,
    ) -> int:
        """Upgrade only the exact provider defaults used by earlier releases.

        Custom model choices are intentionally left untouched. The operation is
        idempotent and never reads, writes, or logs provider credentials.
        """
        legacy_text_models = {"gpt-5-mini", "openai/gpt-5-mini"}
        legacy_text_bases = {
            "https://api.openai.com/v1",
            "https://openrouter.ai/api/v1",
        }
        legacy_image_models = {"gpt-image-1", "gpt-image-1.5"}
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                SELECT id, gender, ai_base_url, ai_model, media_settings
                FROM accounts
                """
            )
            rows = await cursor.fetchall()
            await cursor.close()
            migrated = 0
            now = int(time.time())
            for row in rows:
                current_base = str(row["ai_base_url"]).rstrip("/")
                current_model = str(row["ai_model"]).strip()
                next_base = current_base
                next_model = current_model
                changed_fields: list[str] = []
                if (
                    current_base in legacy_text_bases
                    and current_model in legacy_text_models
                ):
                    next_base = ai_base_url.rstrip("/")
                    next_model = ai_model
                    changed_fields.extend(["ai_base_url", "ai_model"])

                raw_media = str(row["media_settings"])
                next_media = raw_media
                try:
                    media = media_settings_from_json(raw_media).public_dict()
                except ValueError:
                    media = None
                if media is not None:
                    image = media["image"]
                    voice = media["voice"]
                    video = media["video"]
                    media_changed = False
                    if image["model"] in legacy_image_models:
                        image["model"] = image_model
                        media_changed = True
                    if voice["model"] == "azure-speech":
                        voice["model"] = tts_model
                        media_changed = True
                    if str(voice["voice"]).startswith("zh-TW-"):
                        voice["voice"] = (
                            "rex" if str(row["gender"]) == "male" else "eve"
                        )
                        media_changed = True
                    if video["model"] == "sora-2":
                        video["model"] = video_model
                        media_changed = True
                    if media_changed:
                        next_media = json.dumps(
                            media,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        changed_fields.append("media")

                if not changed_fields:
                    continue
                await db.execute(
                    """
                    UPDATE accounts
                    SET ai_base_url=?, ai_model=?, media_settings=?,
                        revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (
                        next_base,
                        next_model,
                        next_media,
                        now,
                        str(row["id"]),
                    ),
                )
                await self._audit_locked(
                    str(row["id"]),
                    "migrate_openrouter_defaults",
                    changed_fields,
                )
                migrated += 1
            await db.commit()
            return migrated

    async def _audit_locked(
        self,
        account_id: str | None,
        action: str,
        fields: list[str],
    ) -> None:
        safe_fields = sorted(
            item
            for item in fields
            if item not in {"session_string", "ai_api_key", "session_ciphertext"}
        )
        await self._connection().execute(
            """
            INSERT INTO audit_log (account_id, action, fields, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (account_id, action[:80], json.dumps(safe_fields), int(time.time())),
        )

    @staticmethod
    def _clean_quota_parameters(
        daily_limit: object,
        cooldown_seconds: object,
    ) -> tuple[int, int]:
        if (
            isinstance(daily_limit, bool)
            or not isinstance(daily_limit, int)
            or not 0 <= daily_limit <= 1000
        ):
            raise ValueError("daily_limit must be an integer between 0 and 1000")
        if (
            isinstance(cooldown_seconds, bool)
            or not isinstance(cooldown_seconds, int)
            or not 0 <= cooldown_seconds <= 7 * 24 * 60 * 60
        ):
            raise ValueError(
                "cooldown_seconds must be an integer between 0 and 604800"
            )
        return daily_limit, cooldown_seconds

    @staticmethod
    def _seconds_until_utc_midnight(now: int) -> int:
        current = time.gmtime(now)
        midnight = calendar.timegm(
            (
                current.tm_year,
                current.tm_mon,
                current.tm_mday,
                0,
                0,
                0,
                0,
                0,
                0,
            )
        )
        return max(midnight + 24 * 60 * 60 - now, 1)

    async def _require_account_locked(self, account_id: str) -> None:
        cursor = await self._connection().execute(
            "SELECT 1 FROM accounts WHERE id = ?",
            (account_id,),
        )
        exists = await cursor.fetchone()
        await cursor.close()
        if exists is None:
            raise ValueError("account does not exist")

    async def _reserve_media_quota_locked(
        self,
        account_id: str,
        media_type: str,
        daily_limit: int,
        cooldown_seconds: int,
        now: int,
    ) -> MediaQuotaDecision:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT day_key, used_count, last_reserved_at
            FROM media_usage
            WHERE account_id = ? AND media_type = ?
            """,
            (account_id, media_type),
        )
        row = await cursor.fetchone()
        await cursor.close()

        day_key = utc_day_key(now)
        stored_day = str(row["day_key"]) if row is not None else ""
        used = int(row["used_count"]) if row is not None and stored_day == day_key else 0
        last_reserved_at = int(row["last_reserved_at"]) if row is not None else 0
        remaining = max(daily_limit - used, 0)
        if daily_limit == 0 or used >= daily_limit:
            return MediaQuotaDecision(
                allowed=False,
                reason="daily_limit",
                used=used,
                remaining=remaining,
                retry_after_seconds=self._seconds_until_utc_midnight(now),
            )

        elapsed = now - last_reserved_at
        if last_reserved_at and elapsed < cooldown_seconds:
            return MediaQuotaDecision(
                allowed=False,
                reason="cooldown",
                used=used,
                remaining=remaining,
                retry_after_seconds=max(cooldown_seconds - elapsed, 1),
            )

        next_used = used + 1
        await db.execute(
            """
            INSERT INTO media_usage (
                account_id, media_type, day_key, used_count, last_reserved_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id, media_type) DO UPDATE SET
                day_key=excluded.day_key,
                used_count=excluded.used_count,
                last_reserved_at=excluded.last_reserved_at
            """,
            (account_id, media_type, day_key, next_used, now),
        )
        return MediaQuotaDecision(
            allowed=True,
            reason="allowed",
            used=next_used,
            remaining=max(daily_limit - next_used, 0),
            retry_after_seconds=0,
        )

    async def reserve_media_quota(
        self,
        account_id: str,
        media_type: str,
        *,
        daily_limit: int,
        cooldown_seconds: int,
        now: int | None = None,
    ) -> MediaQuotaDecision:
        kind = clean_media_kind(media_type)
        cleaned_limit, cleaned_cooldown = self._clean_quota_parameters(
            daily_limit,
            cooldown_seconds,
        )
        timestamp = int(time.time()) if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("now must be a non-negative integer timestamp")
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_account_locked(account_id)
                decision = await self._reserve_media_quota_locked(
                    account_id,
                    kind,
                    cleaned_limit,
                    cleaned_cooldown,
                    timestamp,
                )
                await db.commit()
                return decision
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    def _media_job_from_row(row: aiosqlite.Row) -> MediaJob:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Stored media job payload is invalid") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Stored media job payload is invalid")
        return MediaJob(
            id=str(row["id"]),
            account_id=str(row["account_id"]),
            group_id=int(row["group_id"]),
            media_type=str(row["media_type"]),
            status=str(row["status"]),
            payload=payload,
            result_ref=str(row["result_ref"]),
            error=str(row["error"]),
            attempts=int(row["attempts"]),
            available_at=int(row["available_at"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    async def enqueue_media_job(
        self,
        account_id: str,
        group_id: int,
        media_type: str,
        payload: dict[str, object],
        *,
        daily_limit: int,
        cooldown_seconds: int,
        now: int | None = None,
        available_at: int | None = None,
    ) -> MediaJobReservation:
        if isinstance(group_id, bool) or not isinstance(group_id, int):
            raise ValueError("group_id must be an integer")
        kind = clean_media_kind(media_type)
        cleaned_limit, cleaned_cooldown = self._clean_quota_parameters(
            daily_limit,
            cooldown_seconds,
        )
        payload_json = safe_media_job_payload(payload)
        source_message_id = payload.get("source_message_id")
        if (
            isinstance(source_message_id, bool)
            or not isinstance(source_message_id, int)
            or source_message_id <= 0
        ):
            raise ValueError(
                "media job payload must contain a positive source_message_id"
            )
        timestamp = int(time.time()) if now is None else now
        scheduled_at = timestamp if available_at is None else available_at
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
            or isinstance(scheduled_at, bool)
            or not isinstance(scheduled_at, int)
            or scheduled_at < timestamp
        ):
            raise ValueError(
                "now and available_at must be valid non-negative timestamps"
            )
        job_id = f"media_{uuid.uuid4().hex}"
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_account_locked(account_id)
                quota = await self._reserve_media_quota_locked(
                    account_id,
                    kind,
                    cleaned_limit,
                    cleaned_cooldown,
                    timestamp,
                )
                if not quota.allowed:
                    await db.commit()
                    return MediaJobReservation(quota=quota, job=None)
                await db.execute(
                    """
                    INSERT INTO media_jobs (
                        id, account_id, group_id, media_type, status, payload,
                        result_ref, error, attempts, available_at, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', ?, '', '', 0, ?, ?, ?)
                    """,
                    (
                        job_id,
                        account_id,
                        group_id,
                        kind,
                        payload_json,
                        scheduled_at,
                        timestamp,
                        timestamp,
                    ),
                )
                await db.commit()
                job = MediaJob(
                    id=job_id,
                    account_id=account_id,
                    group_id=group_id,
                    media_type=kind,
                    status="queued",
                    payload=json.loads(payload_json),
                    result_ref="",
                    error="",
                    attempts=0,
                    available_at=scheduled_at,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                return MediaJobReservation(quota=quota, job=job)
            except BaseException:
                await db.rollback()
                raise

    async def claim_next_media_job(
        self,
        account_id: str,
        *,
        media_type: str | None = None,
        now: int | None = None,
    ) -> MediaJob | None:
        kind = clean_media_kind(media_type) if media_type is not None else None
        timestamp = int(time.time()) if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("now must be a non-negative integer timestamp")
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_account_locked(account_id)
                if kind is None:
                    cursor = await db.execute(
                        """
                        SELECT * FROM media_jobs
                        WHERE account_id = ?
                          AND status = 'queued' AND available_at <= ?
                        ORDER BY available_at, created_at, id
                        LIMIT 1
                        """,
                        (account_id, timestamp),
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT * FROM media_jobs
                        WHERE account_id = ?
                          AND status = 'queued' AND available_at <= ?
                          AND media_type = ?
                        ORDER BY available_at, created_at, id
                        LIMIT 1
                        """,
                        (account_id, timestamp, kind),
                    )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    await db.commit()
                    return None
                await db.execute(
                    """
                    UPDATE media_jobs
                    SET status='running', attempts=attempts + 1, updated_at=?
                    WHERE id=? AND status='queued'
                    """,
                    (timestamp, row["id"]),
                )
                cursor = await db.execute(
                    "SELECT * FROM media_jobs WHERE id = ?",
                    (row["id"],),
                )
                claimed = await cursor.fetchone()
                await cursor.close()
                await db.commit()
                return self._media_job_from_row(claimed)
            except BaseException:
                await db.rollback()
                raise

    async def list_media_jobs(
        self,
        account_id: str,
        limit: int = 100,
    ) -> list[MediaJob]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        async with self._lock:
            await self._require_account_locked(account_id)
            cursor = await self._connection().execute(
                """
                SELECT * FROM media_jobs
                WHERE account_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (account_id, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._media_job_from_row(row) for row in rows]

    async def recover_stale_media_jobs(
        self,
        account_id: str,
        *,
        now: int | None = None,
    ) -> int:
        timestamp = int(time.time()) if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("now must be a non-negative integer timestamp")
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_account_locked(account_id)
                cursor = await db.execute(
                    """
                    UPDATE media_jobs
                    SET status='queued', updated_at=?
                    WHERE account_id=? AND status='running'
                    """,
                    (timestamp, account_id),
                )
                recovered = max(int(cursor.rowcount), 0)
                await cursor.close()
                await db.commit()
                return recovered
            except BaseException:
                await db.rollback()
                raise

    async def finish_media_job(
        self,
        job_id: str,
        status: str,
        *,
        result_ref: str = "",
        error: str = "",
        now: int | None = None,
    ) -> MediaJob:
        cleaned_status = str(status or "").strip().lower()
        if cleaned_status not in {"completed", "failed", "cancelled"}:
            raise ValueError("status must be completed, failed, or cancelled")
        if len(result_ref) > 2000 or len(error) > 1000:
            raise ValueError("media job result or error is too long")
        timestamp = int(time.time()) if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("now must be a non-negative integer timestamp")
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                UPDATE media_jobs
                SET status=?, payload='{}', result_ref=?, error=?, updated_at=?
                WHERE id=? AND status IN ('queued', 'running')
                """,
                (cleaned_status, result_ref, error, timestamp, job_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ValueError("media job does not exist or is already finished")
            cursor = await db.execute(
                "SELECT * FROM media_jobs WHERE id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await db.commit()
        return self._media_job_from_row(row)

    async def add(
        self,
        account_id: str,
        group_id: int,
        sender_id: int,
        sender_name: str,
        role: str,
        content: str,
        *,
        created_at: int | None = None,
    ) -> int:
        timestamp = created_at if created_at is not None else int(time.time())
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                INSERT INTO messages
                    (account_id, group_id, sender_id, sender_name, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    group_id,
                    sender_id,
                    sender_name[:120],
                    role,
                    content[:4000],
                    timestamp,
                ),
            )
            row_id = int(cursor.lastrowid)
            await cursor.close()
            await db.commit()
            return row_id

    async def group_activity_profile(
        self,
        account_id: str,
        group_id: int,
        *,
        now: int | None = None,
        recent_window_seconds: int = 5 * 60,
        trailing_window_seconds: int = 20 * 60,
    ) -> GroupActivityProfile:
        """Summarize human traffic for adaptive reply decisions.

        Managed-account messages use the assistant role and are excluded. Each
        worker reads only its own copy of group history so one Telegram message
        is counted once rather than once per connected account.
        """
        cleaned_account_id = str(account_id or "").strip()
        if not cleaned_account_id:
            raise ValueError("account_id is required")
        if isinstance(group_id, bool) or not isinstance(group_id, int):
            raise ValueError("group_id must be an integer")
        timestamp = int(time.time()) if now is None else now
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
        ):
            raise ValueError("now must be a non-negative integer timestamp")
        if (
            isinstance(recent_window_seconds, bool)
            or not isinstance(recent_window_seconds, int)
            or recent_window_seconds <= 0
        ):
            raise ValueError("recent_window_seconds must be a positive integer")
        if (
            isinstance(trailing_window_seconds, bool)
            or not isinstance(trailing_window_seconds, int)
            or trailing_window_seconds < recent_window_seconds
        ):
            raise ValueError(
                "trailing_window_seconds must be at least the recent window"
            )

        recent_cutoff = timestamp - recent_window_seconds
        trailing_cutoff = timestamp - trailing_window_seconds
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END)
                        AS recent_messages,
                    COUNT(DISTINCT CASE
                        WHEN created_at >= ? THEN sender_id
                    END) AS recent_participants,
                    COUNT(*) AS trailing_messages,
                    COUNT(DISTINCT sender_id) AS trailing_participants,
                    MAX(created_at) AS latest_message_at
                FROM messages
                WHERE account_id = ?
                  AND group_id = ?
                  AND role = 'user'
                  AND created_at >= ?
                  AND created_at <= ?
                """,
                (
                    recent_cutoff,
                    recent_cutoff,
                    cleaned_account_id,
                    group_id,
                    trailing_cutoff,
                    timestamp,
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
        latest = row["latest_message_at"] if row is not None else None
        return GroupActivityProfile(
            recent_messages=int(row["recent_messages"] or 0) if row else 0,
            recent_participants=(
                int(row["recent_participants"] or 0) if row else 0
            ),
            trailing_messages=int(row["trailing_messages"] or 0) if row else 0,
            trailing_participants=(
                int(row["trailing_participants"] or 0) if row else 0
            ),
            latest_message_at=int(latest) if latest is not None else None,
        )

    @staticmethod
    def _validate_proactive_parameters(
        account_id: str,
        group_id: int,
        idle_seconds: int,
        cooldown_seconds: int,
        daily_limit: int,
        timestamp: int,
    ) -> str:
        cleaned_account_id = (
            account_id.strip() if isinstance(account_id, str) else ""
        )
        sqlite_integer_max = (1 << 63) - 1
        sqlite_integer_min = -(1 << 63)
        if not cleaned_account_id or len(cleaned_account_id) > 128:
            raise ValueError(
                "account_id must be a non-empty string up to 128 characters"
            )
        if (
            isinstance(group_id, bool)
            or not isinstance(group_id, int)
            or not sqlite_integer_min <= group_id <= sqlite_integer_max
            or group_id == 0
        ):
            raise ValueError("group_id must be a non-zero 64-bit integer")
        for value, name, maximum in (
            (idle_seconds, "idle_seconds", 7 * 24 * 60 * 60),
            (cooldown_seconds, "cooldown_seconds", 7 * 24 * 60 * 60),
            (daily_limit, "daily_limit", 100_000),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= maximum
            ):
                raise ValueError(f"{name} must be between 0 and {maximum}")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not 0 <= timestamp <= sqlite_integer_max
        ):
            raise ValueError("now must be a non-negative 64-bit integer timestamp")
        return cleaned_account_id

    async def record_group_activity(
        self,
        group_id: int,
        *,
        now: int | None = None,
    ) -> bool:
        """Persist group activity and invalidate a pending proactive lease."""
        timestamp = int(time.time()) if now is None else now
        # Reuse the identifier and timestamp validation without requiring an
        # account row: activity is intentionally shared by all managed accounts.
        self._validate_proactive_parameters(
            "activity",
            group_id,
            0,
            0,
            0,
            timestamp,
        )
        if self._group_activity_cache.get(group_id, -1) >= timestamp:
            return False
        async with self._lock:
            if self._group_activity_cache.get(group_id, -1) >= timestamp:
                return False
            db = self._connection()
            await db.execute(
                """
                INSERT INTO proactive_group_state (
                    group_id, last_activity_at, last_proactive_at,
                    lease_owner, lease_token, lease_until
                ) VALUES (?, ?, 0, '', '', 0)
                ON CONFLICT(group_id) DO UPDATE SET
                    last_activity_at=MAX(
                        proactive_group_state.last_activity_at,
                        excluded.last_activity_at
                    ),
                    lease_owner=CASE
                        WHEN excluded.last_activity_at >=
                             proactive_group_state.last_activity_at
                        THEN '' ELSE proactive_group_state.lease_owner END,
                    lease_token=CASE
                        WHEN excluded.last_activity_at >=
                             proactive_group_state.last_activity_at
                        THEN '' ELSE proactive_group_state.lease_token END,
                    lease_until=CASE
                        WHEN excluded.last_activity_at >=
                             proactive_group_state.last_activity_at
                        THEN 0 ELSE proactive_group_state.lease_until END
                """,
                (group_id, timestamp),
            )
            await db.commit()
            self._group_activity_cache[group_id] = timestamp
            return True

    async def claim_proactive_lease(
        self,
        account_id: str,
        group_id: int,
        *,
        idle_seconds: int,
        cooldown_seconds: int,
        daily_limit: int,
        lease_seconds: int = 10 * 60,
        now: int | None = None,
    ) -> ProactiveLeaseDecision:
        """Atomically reserve one group's next proactive-generation attempt."""
        timestamp = int(time.time()) if now is None else now
        cleaned_account_id = self._validate_proactive_parameters(
            account_id,
            group_id,
            idle_seconds,
            cooldown_seconds,
            daily_limit,
            timestamp,
        )
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 60 * 60
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        day_key = utc_day_key(timestamp)
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_account_locked(cleaned_account_id)
                await db.execute(
                    """
                    INSERT OR IGNORE INTO proactive_group_state (
                        group_id, last_activity_at, last_proactive_at,
                        lease_owner, lease_token, lease_until
                    ) VALUES (?, 0, 0, '', '', 0)
                    """,
                    (group_id,),
                )
                cursor = await db.execute(
                    "SELECT * FROM proactive_group_state WHERE group_id = ?",
                    (group_id,),
                )
                state = await cursor.fetchone()
                await cursor.close()
                if state is None:
                    raise RuntimeError("proactive group state was not created")

                last_activity = int(state["last_activity_at"])
                if last_activity and timestamp - last_activity < idle_seconds:
                    retry_after = idle_seconds - (timestamp - last_activity)
                    await db.commit()
                    return ProactiveLeaseDecision(
                        False,
                        "active",
                        retry_after_seconds=max(retry_after, 1),
                    )
                lease_until = int(state["lease_until"])
                if lease_until > timestamp:
                    await db.commit()
                    return ProactiveLeaseDecision(
                        False,
                        "leased",
                        retry_after_seconds=lease_until - timestamp,
                    )
                last_proactive = int(state["last_proactive_at"])
                if (
                    last_proactive
                    and timestamp - last_proactive < cooldown_seconds
                ):
                    retry_after = cooldown_seconds - (
                        timestamp - last_proactive
                    )
                    await db.commit()
                    return ProactiveLeaseDecision(
                        False,
                        "cooldown",
                        retry_after_seconds=max(retry_after, 1),
                    )
                cursor = await db.execute(
                    """
                    SELECT used_count FROM proactive_usage
                    WHERE account_id=? AND group_id=? AND day_key=?
                    """,
                    (cleaned_account_id, group_id, day_key),
                )
                usage = await cursor.fetchone()
                await cursor.close()
                used_count = int(usage["used_count"]) if usage else 0
                if used_count >= daily_limit:
                    await db.commit()
                    return ProactiveLeaseDecision(False, "daily_limit")

                lease_token = uuid.uuid4().hex
                await db.execute(
                    """
                    UPDATE proactive_group_state
                    SET lease_owner=?, lease_token=?, lease_until=?
                    WHERE group_id=?
                    """,
                    (
                        cleaned_account_id,
                        lease_token,
                        timestamp + lease_seconds,
                        group_id,
                    ),
                )
                await db.commit()
                return ProactiveLeaseDecision(
                    True,
                    "reserved",
                    lease_token=lease_token,
                )
            except BaseException:
                await db.rollback()
                raise

    async def commit_proactive_lease(
        self,
        account_id: str,
        group_id: int,
        lease_token: str,
        *,
        idle_seconds: int,
        cooldown_seconds: int,
        daily_limit: int,
        now: int | None = None,
    ) -> ProactiveLeaseDecision:
        """Revalidate activity, then persist cooldown and daily usage."""
        timestamp = int(time.time()) if now is None else now
        cleaned_account_id = self._validate_proactive_parameters(
            account_id,
            group_id,
            idle_seconds,
            cooldown_seconds,
            daily_limit,
            timestamp,
        )
        cleaned_token = lease_token.strip() if isinstance(lease_token, str) else ""
        if len(cleaned_token) != 32 or any(
            char not in "0123456789abcdef" for char in cleaned_token
        ):
            raise ValueError("lease_token must be a lowercase hexadecimal UUID")
        day_key = utc_day_key(timestamp)
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_account_locked(cleaned_account_id)
                cursor = await db.execute(
                    "SELECT * FROM proactive_group_state WHERE group_id = ?",
                    (group_id,),
                )
                state = await cursor.fetchone()
                await cursor.close()
                if (
                    state is None
                    or str(state["lease_owner"]) != cleaned_account_id
                    or str(state["lease_token"]) != cleaned_token
                    or int(state["lease_until"]) < timestamp
                ):
                    await db.commit()
                    return ProactiveLeaseDecision(False, "lease_lost")

                async def deny(
                    reason: str,
                    retry_after_seconds: int = 0,
                ) -> ProactiveLeaseDecision:
                    await db.execute(
                        """
                        UPDATE proactive_group_state
                        SET lease_owner='', lease_token='', lease_until=0
                        WHERE group_id=? AND lease_owner=? AND lease_token=?
                        """,
                        (group_id, cleaned_account_id, cleaned_token),
                    )
                    await db.commit()
                    return ProactiveLeaseDecision(
                        False,
                        reason,
                        retry_after_seconds=max(retry_after_seconds, 0),
                    )

                last_activity = int(state["last_activity_at"])
                if last_activity and timestamp - last_activity < idle_seconds:
                    return await deny(
                        "active",
                        idle_seconds - (timestamp - last_activity),
                    )
                last_proactive = int(state["last_proactive_at"])
                if (
                    last_proactive
                    and timestamp - last_proactive < cooldown_seconds
                ):
                    return await deny(
                        "cooldown",
                        cooldown_seconds - (timestamp - last_proactive),
                    )
                cursor = await db.execute(
                    """
                    SELECT used_count FROM proactive_usage
                    WHERE account_id=? AND group_id=? AND day_key=?
                    """,
                    (cleaned_account_id, group_id, day_key),
                )
                usage = await cursor.fetchone()
                await cursor.close()
                used_count = int(usage["used_count"]) if usage else 0
                if used_count >= daily_limit:
                    return await deny("daily_limit")

                await db.execute(
                    """
                    UPDATE proactive_group_state
                    SET last_proactive_at=?, lease_owner='', lease_token='',
                        lease_until=0
                    WHERE group_id=? AND lease_owner=? AND lease_token=?
                    """,
                    (
                        timestamp,
                        group_id,
                        cleaned_account_id,
                        cleaned_token,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO proactive_usage (
                        account_id, group_id, day_key, used_count, updated_at
                    ) VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(account_id, group_id, day_key) DO UPDATE SET
                        used_count=proactive_usage.used_count + 1,
                        updated_at=excluded.updated_at
                    """,
                    (cleaned_account_id, group_id, day_key, timestamp),
                )
                await db.commit()
                return ProactiveLeaseDecision(True, "committed")
            except BaseException:
                await db.rollback()
                raise

    async def release_proactive_lease(
        self,
        account_id: str,
        group_id: int,
        lease_token: str,
    ) -> bool:
        cleaned_account_id = (
            account_id.strip() if isinstance(account_id, str) else ""
        )
        cleaned_token = lease_token.strip() if isinstance(lease_token, str) else ""
        if not cleaned_account_id or not cleaned_token:
            return False
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                UPDATE proactive_group_state
                SET lease_owner='', lease_token='', lease_until=0
                WHERE group_id=? AND lease_owner=? AND lease_token=?
                """,
                (group_id, cleaned_account_id, cleaned_token),
            )
            released = cursor.rowcount == 1
            await cursor.close()
            await db.commit()
            return released

    async def claim_group_reply(
        self,
        account_id: str,
        group_id: int,
        claim_key: int | str,
        *,
        now: int | None = None,
    ) -> bool:
        """Atomically reserve one group message for exactly one account.

        String keys are stable cross-account fingerprints used for Telegram
        basic groups, whose local message IDs differ by account. Integer keys
        retain compatibility with already-deployed callers and old tests.
        """
        cleaned_account_id = (
            account_id.strip() if isinstance(account_id, str) else ""
        )
        if not cleaned_account_id or len(cleaned_account_id) > 128:
            raise ValueError("account_id must be a non-empty string up to 128 characters")
        sqlite_integer_max = (1 << 63) - 1
        sqlite_integer_min = -(1 << 63)
        if (
            isinstance(group_id, bool)
            or not isinstance(group_id, int)
            or not sqlite_integer_min <= group_id <= sqlite_integer_max
            or group_id == 0
        ):
            raise ValueError("group_id must be a non-zero 64-bit integer")
        legacy_message_id: int | None = None
        stable_claim_key = ""
        if isinstance(claim_key, int) and not isinstance(claim_key, bool):
            if not 1 <= claim_key <= sqlite_integer_max:
                raise ValueError(
                    "telegram_message_id must be a positive 64-bit integer"
                )
            legacy_message_id = claim_key
        elif isinstance(claim_key, str):
            stable_claim_key = claim_key.strip().lower()
            if len(stable_claim_key) != 64 or any(
                char not in "0123456789abcdef" for char in stable_claim_key
            ):
                raise ValueError("claim_key must be a 64-character SHA-256 hex digest")
        else:
            raise ValueError(
                "claim_key must be a positive message ID or SHA-256 hex digest"
            )
        timestamp = int(time.time()) if now is None else now
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not 0 <= timestamp <= sqlite_integer_max
        ):
            raise ValueError("now must be a non-negative 64-bit integer timestamp")

        cutoff = timestamp - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_account_locked(cleaned_account_id)
                await db.execute(
                    "DELETE FROM reply_claims WHERE created_at < ?",
                    (cutoff,),
                )
                await db.execute(
                    "DELETE FROM reply_claims_v2 WHERE created_at < ?",
                    (cutoff,),
                )
                if legacy_message_id is not None:
                    cursor = await db.execute(
                        """
                        INSERT OR IGNORE INTO reply_claims (
                            group_id, telegram_message_id, account_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            legacy_message_id,
                            cleaned_account_id,
                            timestamp,
                        ),
                    )
                else:
                    cursor = await db.execute(
                        """
                        INSERT OR IGNORE INTO reply_claims_v2 (
                            group_id, claim_key, account_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            stable_claim_key,
                            cleaned_account_id,
                            timestamp,
                        ),
                    )
                claimed = cursor.rowcount == 1
                await cursor.close()
                await db.commit()
                return claimed
            except BaseException:
                await db.rollback()
                raise

    async def claim_group_reply_with_slots(
        self,
        account_id: str,
        group_id: int,
        claim_key: int | str,
        *,
        max_claims: int = 1,
        now: int | None = None,
    ) -> int:
        """Atomically reserve one of a bounded number of reply slots.

        String keys are stable cross-account fingerprints used for Telegram
        basic groups, whose local message IDs differ by account. Integer keys
        retain compatibility with already-deployed callers and old tests.
        The returned value is the 1-based slot number, or zero when no slot
        remains.
        """
        if max_claims <= 1:
            return 1 if await self.claim_group_reply(
                account_id,
                group_id,
                claim_key,
                now=now,
            ) else 0

        cleaned_account_id = (
            account_id.strip() if isinstance(account_id, str) else ""
        )
        if not cleaned_account_id or len(cleaned_account_id) > 128:
            raise ValueError("account_id must be a non-empty string up to 128 characters")
        sqlite_integer_max = (1 << 63) - 1
        sqlite_integer_min = -(1 << 63)
        if (
            isinstance(group_id, bool)
            or not isinstance(group_id, int)
            or not sqlite_integer_min <= group_id <= sqlite_integer_max
            or group_id == 0
        ):
            raise ValueError("group_id must be a non-zero 64-bit integer")
        legacy_message_id: int | None = None
        stable_claim_key = ""
        if isinstance(claim_key, int) and not isinstance(claim_key, bool):
            if not 1 <= claim_key <= sqlite_integer_max:
                raise ValueError(
                    "telegram_message_id must be a positive 64-bit integer"
                )
            legacy_message_id = claim_key
            stable_claim_key = f"legacy:{claim_key}"
        elif isinstance(claim_key, str):
            stable_claim_key = claim_key.strip().lower()
            if len(stable_claim_key) != 64 or any(
                char not in "0123456789abcdef" for char in stable_claim_key
            ):
                raise ValueError("claim_key must be a 64-character SHA-256 hex digest")
        else:
            raise ValueError(
                "claim_key must be a positive message ID or SHA-256 hex digest"
            )
        if (
            isinstance(max_claims, bool)
            or not isinstance(max_claims, int)
            or not 1 <= max_claims <= 3
        ):
            raise ValueError("max_claims must be an integer between 1 and 3")
        timestamp = int(time.time()) if now is None else now
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not 0 <= timestamp <= sqlite_integer_max
        ):
            raise ValueError("now must be a non-negative 64-bit integer timestamp")

        cutoff = timestamp - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_account_locked(cleaned_account_id)
                await db.execute(
                    "DELETE FROM reply_claims WHERE created_at < ?",
                    (cutoff,),
                )
                await db.execute(
                    "DELETE FROM reply_claims_v2 WHERE created_at < ?",
                    (cutoff,),
                )
                await db.execute(
                    "DELETE FROM reply_claims_v3 WHERE created_at < ?",
                    (cutoff,),
                )
                if legacy_message_id is not None:
                    cursor = await db.execute(
                        """
                        SELECT account_id FROM reply_claims
                        WHERE group_id = ? AND telegram_message_id = ?
                        """,
                        (group_id, legacy_message_id),
                    )
                    legacy_claim = await cursor.fetchone()
                    await cursor.close()
                    if legacy_claim is not None:
                        await db.commit()
                        return 0
                cursor = await db.execute(
                    """
                    SELECT slot FROM reply_claims_v3
                    WHERE group_id=? AND claim_key=? AND account_id=?
                    """,
                    (group_id, stable_claim_key, cleaned_account_id),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None:
                    await db.commit()
                    return 0
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) AS claim_count FROM reply_claims_v3
                    WHERE group_id=? AND claim_key=?
                    """,
                    (group_id, stable_claim_key),
                )
                row = await cursor.fetchone()
                await cursor.close()
                claim_count = int(row["claim_count"]) if row is not None else 0
                if claim_count >= max_claims:
                    await db.commit()
                    return 0
                slot = claim_count + 1
                await db.execute(
                    """
                    INSERT INTO reply_claims_v3 (
                        group_id, claim_key, slot, account_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        stable_claim_key,
                        slot,
                        cleaned_account_id,
                        timestamp,
                    ),
                )
                await db.execute(
                    """
                    INSERT OR IGNORE INTO reply_claims_v2 (
                        group_id, claim_key, account_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        stable_claim_key,
                        cleaned_account_id,
                        timestamp,
                    ),
                )
                if legacy_message_id is not None:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO reply_claims (
                            group_id, telegram_message_id, account_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            legacy_message_id,
                            cleaned_account_id,
                            timestamp,
                        ),
                    )
                await db.commit()
                return slot
            except BaseException:
                await db.rollback()
                raise
    async def add_private_alert(
        self,
        account_id: str,
        sender_fingerprint: str,
        telegram_message_id: int,
        sender_name: object,
        preview: object,
        *,
        created_at: int | None = None,
    ) -> bool:
        """Store a private-message notification without exposing Telegram IDs.

        The sender fingerprint and Telegram message ID are internal deduplication
        fields only. Callers must not include them in dashboard responses or logs.
        """
        if not sender_fingerprint or len(sender_fingerprint) > 128:
            raise ValueError("sender_fingerprint is invalid")
        if (
            isinstance(telegram_message_id, bool)
            or not isinstance(telegram_message_id, int)
            or telegram_message_id < 0
        ):
            raise ValueError("telegram_message_id must be a non-negative integer")
        timestamp = int(time.time()) if created_at is None else created_at
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("created_at must be a non-negative integer timestamp")
        safe_name = self._clean_private_alert_text(
            sender_name,
            limit=120,
            fallback="Telegram 使用者",
        )
        safe_preview = self._clean_private_alert_text(
            preview,
            limit=280,
            fallback="（非文字訊息）",
        )
        alert_id = str(uuid.uuid4())
        cutoff = timestamp - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            await db.execute(
                "DELETE FROM private_alerts WHERE account_id = ? AND created_at < ?",
                (account_id, cutoff),
            )
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO private_alerts (
                    id, account_id, sender_fingerprint, telegram_message_id,
                    sender_name, preview, created_at, acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    alert_id,
                    account_id,
                    sender_fingerprint,
                    telegram_message_id,
                    safe_name,
                    safe_preview,
                    timestamp,
                ),
            )
            inserted = cursor.rowcount == 1
            await cursor.close()
            await db.execute(
                """
                DELETE FROM private_alerts
                WHERE account_id = ? AND id IN (
                    SELECT id FROM private_alerts
                    WHERE account_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET 200
                )
                """,
                (account_id, account_id),
            )
            await db.commit()
        return inserted

    async def list_private_alerts(
        self,
        account_id: str,
        limit: int = 50,
        *,
        unread_only: bool = False,
    ) -> list[PrivateAlertEntry]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if not isinstance(unread_only, bool):
            raise ValueError("unread_only must be a boolean")
        cutoff = int(time.time()) - self.ttl_seconds
        unread_clause = "AND acknowledged_at IS NULL" if unread_only else ""
        async with self._lock:
            cursor = await self._connection().execute(
                f"""
                SELECT id, sender_name, preview, created_at, acknowledged_at
                FROM private_alerts
                WHERE account_id = ? AND created_at >= ? {unread_clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (account_id, cutoff, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            PrivateAlertEntry(
                alert_id=str(row["id"]),
                sender_name=str(row["sender_name"]),
                preview=str(row["preview"]),
                created_at=int(row["created_at"]),
                acknowledged=row["acknowledged_at"] is not None,
            )
            for row in rows
        ]

    async def private_unread_count(self, account_id: str) -> int:
        cutoff = int(time.time()) - self.ttl_seconds
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT COUNT(*) AS total FROM private_alerts
                WHERE account_id = ? AND created_at >= ?
                  AND acknowledged_at IS NULL
                """,
                (account_id, cutoff),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row["total"]) if row else 0

    async def private_alert_summary(self, account_id: str) -> dict[str, int]:
        cutoff = int(time.time()) - self.ttl_seconds
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT
                    COUNT(
                        CASE WHEN acknowledged_at IS NULL THEN 1 END
                    ) AS unread_count,
                    MAX(created_at) AS latest_at
                FROM private_alerts
                WHERE account_id = ? AND created_at >= ?
                """,
                (account_id, cutoff),
            )
            row = await cursor.fetchone()
            await cursor.close()
        unread = int(row["unread_count"]) if row else 0
        latest_at = int(row["latest_at"]) if row and row["latest_at"] is not None else 0
        return {"unread_count": unread, "latest_at": latest_at}

    async def acknowledge_private_alert(
        self,
        account_id: str,
        alert_id: str,
        *,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else now
        cutoff = timestamp - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                UPDATE private_alerts SET acknowledged_at = ?
                WHERE account_id = ? AND id = ? AND created_at >= ?
                  AND acknowledged_at IS NULL
                """,
                (timestamp, account_id, alert_id, cutoff),
            )
            changed = cursor.rowcount == 1
            await cursor.close()
            await db.commit()
        return changed

    async def acknowledge_all_private_alerts(
        self,
        account_id: str,
        *,
        now: int | None = None,
    ) -> int:
        timestamp = int(time.time()) if now is None else now
        cutoff = timestamp - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                UPDATE private_alerts SET acknowledged_at = ?
                WHERE account_id = ? AND created_at >= ?
                  AND acknowledged_at IS NULL
                """,
                (timestamp, account_id, cutoff),
            )
            changed = max(cursor.rowcount, 0)
            await cursor.close()
            await db.commit()
        return changed

    async def recent_group(
        self,
        account_id: str,
        group_id: int,
        limit: int,
        *,
        through_id: int | None = None,
    ) -> list[MemoryMessage]:
        if (
            through_id is not None
            and (
                isinstance(through_id, bool)
                or not isinstance(through_id, int)
                or through_id <= 0
            )
        ):
            raise ValueError("through_id must be a positive integer")
        cutoff = int(time.time()) - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            if through_id is None:
                cursor = await db.execute(
                    """
                    SELECT account_id, group_id, sender_id, sender_name, role,
                           content, created_at
                    FROM messages
                    WHERE account_id = ? AND group_id = ? AND created_at >= ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (account_id, group_id, cutoff, limit),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT account_id, group_id, sender_id, sender_name, role,
                           content, created_at
                    FROM messages
                    WHERE account_id = ? AND group_id = ? AND created_at >= ?
                      AND id <= ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (account_id, group_id, cutoff, through_id, limit),
                )
            rows = await cursor.fetchall()
            await cursor.close()
        return [MemoryMessage(**dict(row)) for row in reversed(rows)]

    async def conversation_log(
        self,
        account_id: str,
        group_id: int,
        limit: int,
    ) -> list[ConversationLogEntry]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        messages = await self.recent_group(account_id, group_id, limit)
        return [
            ConversationLogEntry(
                created_at=message.created_at,
                sender_name=message.sender_name,
                role=message.role,
                content=message.content,
            )
            for message in messages
        ]

    async def purge_expired(self, *, now: int | None = None) -> int:
        current = now if now is not None else int(time.time())
        cutoff = current - self.ttl_seconds
        proactive_cutoff = current - max(
            self.ttl_seconds,
            PROACTIVE_STATE_MIN_RETENTION_SECONDS,
        )
        current_day_key = utc_day_key(current)
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                "DELETE FROM messages WHERE created_at < ?",
                (cutoff,),
            )
            removed_messages = max(cursor.rowcount, 0)
            cursor = await db.execute(
                "DELETE FROM private_alerts WHERE created_at < ?",
                (cutoff,),
            )
            removed_alerts = max(cursor.rowcount, 0)
            cursor = await db.execute(
                "DELETE FROM reply_claims WHERE created_at < ?",
                (cutoff,),
            )
            removed_reply_claims = max(cursor.rowcount, 0)
            cursor = await db.execute(
                "DELETE FROM reply_claims_v2 WHERE created_at < ?",
                (cutoff,),
            )
            removed_reply_claims += max(cursor.rowcount, 0)
            cursor = await db.execute(
                "DELETE FROM reply_claims_v3 WHERE created_at < ?",
                (cutoff,),
            )
            removed_reply_claims += max(cursor.rowcount, 0)
            cursor = await db.execute(
                """
                DELETE FROM proactive_usage
                WHERE updated_at < ? AND day_key < ?
                """,
                (proactive_cutoff, current_day_key),
            )
            removed_proactive_usage = max(cursor.rowcount, 0)
            cursor = await db.execute(
                """
                DELETE FROM proactive_group_state
                WHERE MAX(last_activity_at, last_proactive_at, lease_until) < ?
                """,
                (proactive_cutoff,),
            )
            removed_proactive_state = max(cursor.rowcount, 0)
            await db.execute(
                """
                UPDATE media_jobs
                SET status='cancelled', payload='{}', result_ref='',
                    error='expired', updated_at=?
                WHERE created_at < ? AND status IN ('queued', 'running')
                """,
                (current, cutoff),
            )
            await db.execute(
                """
                DELETE FROM media_jobs
                WHERE updated_at < ?
                  AND status IN ('completed', 'failed', 'cancelled')
                """,
                (cutoff,),
            )
            await db.commit()
            return (
                removed_messages
                + removed_alerts
                + removed_reply_claims
                + removed_proactive_usage
                + removed_proactive_state
            )

    async def legacy_runtime_settings(self) -> dict[str, str]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT key, value FROM runtime_settings
                WHERE key IN ('ai_enabled', 'group_mode', 'group_ids')
                """
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def statistics(self, account_id: str | None = None) -> dict[str, int]:
        cutoff = int(time.time()) - self.ttl_seconds
        where = "created_at >= ?"
        values: tuple[object, ...] = (cutoff,)
        if account_id is not None:
            where += " AND account_id = ?"
            values = (cutoff, account_id)
        async with self._lock:
            cursor = await self._connection().execute(
                f"""
                SELECT
                    COUNT(*) AS message_count,
                    COUNT(DISTINCT group_id) AS group_count
                FROM messages
                WHERE {where}
                """,
                values,
            )
            row = await cursor.fetchone()
            await cursor.close()
        return {
            "message_count": int(row["message_count"]) if row else 0,
            "group_count": int(row["group_count"]) if row else 0,
        }

    async def clear_all(self, account_id: str | None = None) -> int:
        async with self._lock:
            db = self._connection()
            if account_id is None:
                cursor = await db.execute("DELETE FROM messages")
            else:
                cursor = await db.execute(
                    "DELETE FROM messages WHERE account_id = ?",
                    (account_id,),
                )
            await self._audit_locked(account_id, "memory_cleared", ["messages"])
            await db.commit()
            return max(cursor.rowcount, 0)

    async def clear_memory_with_persona(
        self,
        record: AccountRecord,
        *,
        style: str,
        expected_revision: int,
    ) -> tuple[int, AccountRecord]:
        """Atomically clear one account's messages and replace its persona."""
        now = int(time.time())
        updated = record.with_updates(
            style=style,
            revision=expected_revision + 1,
            updated_at=now,
        )
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                update_cursor = await db.execute(
                    """
                    UPDATE accounts
                    SET style=?, revision=?, updated_at=?
                    WHERE id=? AND revision=?
                    """,
                    (
                        style,
                        updated.revision,
                        now,
                        record.id,
                        expected_revision,
                    ),
                )
                updated_rows = int(update_cursor.rowcount)
                await update_cursor.close()
                if updated_rows != 1:
                    raise RuntimeError(
                        "Account settings changed in another request"
                    )
                delete_cursor = await db.execute(
                    "DELETE FROM messages WHERE account_id = ?",
                    (record.id,),
                )
                removed = max(int(delete_cursor.rowcount), 0)
                await delete_cursor.close()
                await self._audit_locked(
                    record.id,
                    "memory_cleared",
                    ["messages"],
                )
                await self._audit_locked(
                    record.id,
                    "account_updated",
                    ["style"],
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return removed, updated
