from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class MemoryMessage:
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
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_group_time "
            "ON messages (group_id, created_at)"
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            INSERT OR IGNORE INTO runtime_settings (key, value)
            VALUES ('ai_enabled', 'true')
            """
        )
        await self._db.commit()
        await self.purge_expired()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("MemoryStore is not open")
        return self._db

    async def add(
        self,
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
                    (group_id, sender_id, sender_name, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    sender_id,
                    sender_name[:120],
                    role,
                    content[:4000],
                    timestamp,
                ),
            )
            await db.commit()

    async def recent_group(self, group_id: int, limit: int) -> list[MemoryMessage]:
        cutoff = int(time.time()) - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                SELECT group_id, sender_id, sender_name, role, content, created_at
                FROM messages
                WHERE group_id = ? AND created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (group_id, cutoff, limit),
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

    async def get_ai_enabled(self) -> bool:
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                "SELECT value FROM runtime_settings WHERE key = 'ai_enabled'"
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row is None or str(row["value"]).lower() == "true"

    async def set_ai_enabled(self, enabled: bool) -> None:
        async with self._lock:
            db = self._connection()
            await db.execute(
                """
                INSERT INTO runtime_settings (key, value)
                VALUES ('ai_enabled', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("true" if enabled else "false",),
            )
            await db.commit()

    async def get_group_filter(self) -> tuple[bool, frozenset[int]] | None:
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                SELECT key, value
                FROM runtime_settings
                WHERE key IN ('group_mode', 'group_ids')
                """
            )
            rows = await cursor.fetchall()
            await cursor.close()
        values = {str(row["key"]): str(row["value"]) for row in rows}
        if "group_mode" not in values:
            return None
        try:
            ids = frozenset(int(item) for item in json.loads(values.get("group_ids", "[]")))
        except (TypeError, ValueError, json.JSONDecodeError):
            ids = frozenset()
        return values["group_mode"] == "all", ids

    async def set_group_filter(self, all_groups: bool, group_ids: frozenset[int]) -> None:
        async with self._lock:
            db = self._connection()
            await db.executemany(
                """
                INSERT INTO runtime_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    ("group_mode", "all" if all_groups else "selected"),
                    ("group_ids", json.dumps(sorted(group_ids))),
                ),
            )
            await db.commit()

    async def statistics(self) -> dict[str, int]:
        cutoff = int(time.time()) - self.ttl_seconds
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """
                SELECT
                    COUNT(*) AS message_count,
                    COUNT(DISTINCT group_id) AS group_count
                FROM messages
                WHERE created_at >= ?
                """,
                (cutoff,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return {
            "message_count": int(row["message_count"]) if row else 0,
            "group_count": int(row["group_count"]) if row else 0,
        }

    async def clear_all(self) -> int:
        async with self._lock:
            db = self._connection()
            cursor = await db.execute("DELETE FROM messages")
            await db.commit()
            return max(cursor.rowcount, 0)
