from __future__ import annotations

import os
import time

import aiosqlite


class Database:
    """資料層 - 簡化 schema，按帳號/群組存記憶，按群組限流防膨脹"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("資料庫未連線")
        return self._db

    async def connect(self):
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._init_schema()

    async def _init_schema(self):
        db = self._c
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                session_key TEXT NOT NULL,
                tg_user_id INTEGER UNIQUE,
                tg_username TEXT,
                persona TEXT,
                groups TEXT,
                enabled INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0,
                updated_at REAL DEFAULT 0
            )
        """)
        # 舊庫升級：補 groups 欄位（指定群組，JSON；空 = 自動所有群）
        cols = await db.execute("PRAGMA table_info(accounts)")
        col_names = {r[1] for r in await cols.fetchall()}
        if "groups" not in col_names:
            await db.execute("ALTER TABLE accounts ADD COLUMN groups TEXT")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_account_group
            ON messages (account_id, group_id, timestamp DESC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS private_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                preview TEXT NOT NULL,
                timestamp REAL NOT NULL,
                read INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                account_id TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                at REAL NOT NULL,
                PRIMARY KEY (account_id, group_id, kind)
            )
        """)
        await db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    # ---------- 帳號 ----------

    async def create_account(self, account_id: str, name: str, session_key: str,
                             persona: str = "") -> None:
        now = time.time()
        await self._c.execute(
            "INSERT OR IGNORE INTO accounts (id, name, session_key, persona, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, name, session_key, persona, now, now),
        )
        await self._c.commit()

    async def list_accounts(self) -> list[dict]:
        cursor = await self._c.execute(
            "SELECT id, name, session_key, tg_user_id, tg_username, persona, groups, enabled "
            "FROM accounts ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_account(self, account_id: str) -> dict | None:
        cursor = await self._c.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_account(self, account_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self._c.execute(
            f"UPDATE accounts SET {cols} WHERE id = ?",
            (*fields.values(), account_id),
        )
        await self._c.commit()

    async def delete_account(self, account_id: str) -> None:
        await self._c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        await self._c.execute("DELETE FROM messages WHERE account_id = ?", (account_id,))
        await self._c.execute("DELETE FROM private_messages WHERE account_id = ?", (account_id,))
        await self._c.execute("DELETE FROM activity WHERE account_id = ?", (account_id,))
        await self._c.commit()

    # ---------- 記憶 ----------

    async def add_message(self, account_id: str, group_id: int, sender_id: int,
                          sender_name: str, role: str, content: str) -> None:
        await self._c.execute(
            "INSERT INTO messages (account_id, group_id, sender_id, sender_name, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, group_id, sender_id, sender_name, role, content, time.time()),
        )
        # 每群最多留 200 條
        await self._c.execute(
            "DELETE FROM messages WHERE account_id = ? AND group_id = ? AND id NOT IN "
            "(SELECT id FROM messages WHERE account_id = ? AND group_id = ? "
            "ORDER BY timestamp DESC LIMIT 200)",
            (account_id, group_id, account_id, group_id),
        )
        await self._c.commit()

    async def get_recent_messages(self, account_id: str, group_id: int,
                                  limit: int = 30) -> list[dict]:
        cursor = await self._c.execute(
            "SELECT sender_id, sender_name, role, content, timestamp "
            "FROM messages WHERE account_id = ? AND group_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (account_id, group_id, limit),
        )
        rows = list(await cursor.fetchall())
        return [dict(r) for r in reversed(rows)]

    # ---------- 私訊 ----------

    async def add_private_message(self, account_id: str, sender_id: int,
                                  sender_name: str, preview: str) -> None:
        await self._c.execute(
            "INSERT INTO private_messages (account_id, sender_id, sender_name, preview, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, sender_id, sender_name, preview, time.time()),
        )
        await self._c.commit()

    async def get_private_messages(self, account_id: str, unread_only: bool = False,
                                   limit: int = 50) -> list[dict]:
        where = "read = 0" if unread_only else "1=1"
        cursor = await self._c.execute(
            f"SELECT * FROM private_messages WHERE account_id = ? AND {where} "
            "ORDER BY timestamp DESC LIMIT ?",
            (account_id, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def mark_private_message_read(self, message_id: int) -> None:
        await self._c.execute(
            "UPDATE private_messages SET read = 1 WHERE id = ?", (message_id,)
        )
        await self._c.commit()

    # ---------- 活躍節奏（防機器感） ----------

    async def touch_activity(self, account_id: str, group_id: int, kind: str) -> None:
        await self._c.execute(
            "INSERT OR REPLACE INTO activity (account_id, group_id, kind, at) VALUES (?, ?, ?, ?)",
            (account_id, group_id, kind, time.time()),
        )
        await self._c.commit()

    async def last_activity(self, account_id: str, group_id: int, kind: str) -> float:
        cursor = await self._c.execute(
            "SELECT at FROM activity WHERE account_id = ? AND group_id = ? AND kind = ?",
            (account_id, group_id, kind),
        )
        row = await cursor.fetchone()
        return row["at"] if row else 0.0

    async def stats_for(self, account_id: str) -> dict:
        cursor = await self._c.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE account_id = ? AND role = 'assistant'",
            (account_id,),
        )
        row = await cursor.fetchone()
        return {"sent": row["n"] if row else 0}

    # ---------- 維護 ----------

    async def cleanup_expired(self, ttl_hours: int) -> None:
        cutoff = time.time() - ttl_hours * 3600
        await self._c.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
        await self._c.execute("DELETE FROM private_messages WHERE timestamp < ?", (cutoff,))
        await self._c.commit()
