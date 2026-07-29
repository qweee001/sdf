from __future__ import annotations

import asyncio
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

