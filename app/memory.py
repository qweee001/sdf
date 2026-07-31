from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from .account import AccountRecord


SCHEMA_VERSION = 3


@dataclass(frozen=True)
class MemoryMessage:
    account_id: str
    group_id: int
    sender_id: int
    sender_name: str
    role: str
    content: str
    created_at: int


class MemoryStore:
    def __init__(self, path: str, ttl_hours: int) -> None:
        self.path = path
        self.ttl_seconds = ttl_hours * 60 * 60
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

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
                blocked_topics TEXT NOT NULL DEFAULT '[]'
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

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("MemoryStore is not open")
        return self._db

    @staticmethod
    def _account_from_row(row: aiosqlite.Row) -> AccountRecord:
        try:
            group_ids = frozenset(int(item) for item in json.loads(row["group_ids"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            group_ids = frozenset()
        blocked_terms = MemoryStore._policy_values(row, "blocked_terms")
        blocked_topics = MemoryStore._policy_values(row, "blocked_topics")
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
                    blocked_terms, blocked_topics
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
                    updated_at=?, blocked_terms=?, blocked_topics=?
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
    ) -> None:
        timestamp = created_at if created_at is not None else int(time.time())
        async with self._lock:
            db = self._connection()
            await db.execute(
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
            await db.commit()

    async def recent_group(
        self,
        account_id: str,
        group_id: int,
        limit: int,
    ) -> list[MemoryMessage]:
        cutoff = int(time.time()) - self.ttl_seconds
        async with self._lock:
            db = self._connection()
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
            rows = await cursor.fetchall()
            await cursor.close()
        return [MemoryMessage(**dict(row)) for row in reversed(rows)]

    async def purge_expired(self, *, now: int | None = None) -> int:
        current = now if now is not None else int(time.time())
        cutoff = current - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                "DELETE FROM messages WHERE created_at < ?",
                (cutoff,),
            )
            await db.commit()
            return max(cursor.rowcount, 0)

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
