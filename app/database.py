from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import aiosqlite


class Database:
    """資料層 - 簡化 schema，按帳號/群組存記憶，按群組限流防膨脹"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._media_budget_lock = asyncio.Lock()
        self._claim_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._event_lock = asyncio.Lock()

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
                setup_complete INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0,
                updated_at REAL DEFAULT 0
            )
        """)
        # 舊庫升級：補 groups / setup_complete 欄位
        cols = await db.execute("PRAGMA table_info(accounts)")
        col_names = {r[1] for r in await cols.fetchall()}
        if "groups" not in col_names:
            await db.execute("ALTER TABLE accounts ADD COLUMN groups TEXT")
        if "setup_complete" not in col_names:
            # 舊帳號沿用既有行為，不因升級被突然禁止啟動。
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN setup_complete INTEGER DEFAULT 1"
            )
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS media_spend (
                reservation_id TEXT PRIMARY KEY,
                day TEXT NOT NULL,
                account_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                reserved_usd REAL NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_spend_day
            ON media_spend (day)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_claims (
                claim_key TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                claimed_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runtime_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_events (
                group_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sender_kind TEXT NOT NULL,
                observed_at REAL NOT NULL,
                PRIMARY KEY (group_id, message_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_group_events_pressure
            ON group_events (group_id, sender_kind, observed_at DESC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reply_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_events_unique_stage
            ON reply_events (group_id, message_id, account_id, stage, reason)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_reply_events_pressure
            ON reply_events (group_id, stage, reason, at DESC)
        """)
        await db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    # ---------- 運行時設定 ----------

    async def get_runtime_settings(self) -> dict[str, str]:
        cursor = await self._c.execute(
            "SELECT key, value FROM runtime_settings ORDER BY key"
        )
        return {str(row["key"]): str(row["value"]) for row in await cursor.fetchall()}

    async def set_runtime_settings(self, values: dict[str, str]) -> None:
        if not values:
            return
        now = time.time()
        async with self._settings_lock:
            async with aiosqlite.connect(self.db_path, timeout=30) as settings_db:
                try:
                    await settings_db.execute("BEGIN IMMEDIATE")
                    for key, value in values.items():
                        await settings_db.execute(
                            "INSERT INTO runtime_settings (key, value, updated_at) "
                            "VALUES (?, ?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET "
                            "value = excluded.value, updated_at = excluded.updated_at",
                            (str(key), str(value), now),
                        )
                    await settings_db.commit()
                except BaseException:
                    await settings_db.rollback()
                    raise

    # ---------- 帳號 ----------

    async def create_account(self, account_id: str, name: str, session_key: str,
                             persona: str = "") -> None:
        now = time.time()
        await self._c.execute(
            "INSERT OR IGNORE INTO accounts "
            "(id, name, session_key, persona, setup_complete, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (account_id, name, session_key, persona, now, now),
        )
        await self._c.commit()

    async def list_accounts(self) -> list[dict]:
        cursor = await self._c.execute(
            "SELECT id, name, session_key, tg_user_id, tg_username, persona, groups, "
            "setup_complete, enabled FROM accounts ORDER BY created_at"
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

    async def get_recent_group_replies(
        self, group_id: int, limit: int = 20
    ) -> list[str]:
        """跨所有帳號讀取群內近期實際送出的文案，供全群去重。"""
        cursor = await self._c.execute(
            "SELECT content FROM messages "
            "WHERE group_id = ? AND role = 'assistant' "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (group_id, limit),
        )
        return [str(row["content"]) for row in await cursor.fetchall()]

    # ---------- 去重流量與持久回覆診斷 ----------

    async def record_group_event(
        self, group_id: int, message_id: int, sender_kind: str
    ) -> bool:
        """跨觀察帳號只記一次 Telegram 平台事件，不保存內容或身分。"""
        if not group_id or message_id <= 0 or sender_kind not in {"human", "managed"}:
            return False
        async with self._event_lock:
            cursor = await self._c.execute(
                "INSERT OR IGNORE INTO group_events "
                "(group_id, message_id, sender_kind, observed_at) VALUES (?, ?, ?, ?)",
                (int(group_id), int(message_id), sender_kind, time.time()),
            )
            await self._c.commit()
            return cursor.rowcount == 1

    async def record_reply_event(
        self,
        *,
        group_id: int,
        message_id: int,
        account_id: str,
        stage: str,
        reason: str,
    ) -> bool:
        """持久保存阶段和原因；绝不保存 prompt、输入或回复正文。"""
        if not group_id or message_id <= 0 or not stage or not reason:
            return False
        async with self._event_lock:
            cursor = await self._c.execute(
                "INSERT OR IGNORE INTO reply_events "
                "(group_id, message_id, account_id, stage, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(group_id),
                    int(message_id),
                    str(account_id),
                    str(stage),
                    str(reason),
                    time.time(),
                ),
            )
            await self._c.commit()
            return cursor.rowcount == 1

    async def interaction_pressure(
        self, group_id: int, *, now: float | None = None
    ) -> dict[str, int]:
        current = float(now if now is not None else time.time())
        cursor = await self._c.execute(
            "SELECT "
            "SUM(CASE WHEN sender_kind = 'human' AND observed_at >= ? THEN 1 ELSE 0 END) AS human_5m "
            "FROM group_events WHERE group_id = ? AND observed_at >= ?",
            (current - 300, int(group_id), current - 300),
        )
        human_row = await cursor.fetchone()
        cursor = await self._c.execute(
            "SELECT "
            "SUM(CASE WHEN stage = 'sent' AND reason = 'human' AND at >= ? THEN 1 ELSE 0 END) AS human_sent_5m, "
            "SUM(CASE WHEN stage = 'claimed' AND reason = 'human' AND at >= ? THEN 1 ELSE 0 END) AS ordinary_claimed_5m, "
            "SUM(CASE WHEN stage = 'claimed' AND reason = 'human' AND at >= ? THEN 1 ELSE 0 END) AS ordinary_claimed_20s, "
            "SUM(CASE WHEN stage = 'claimed' AND reason = 'human' AND at >= ? THEN 1 ELSE 0 END) AS ordinary_claimed_10m, "
            "SUM(CASE WHEN stage = 'sent' AND at >= ? THEN 1 ELSE 0 END) AS sent_20s, "
            "SUM(CASE WHEN stage = 'sent' AND at >= ? THEN 1 ELSE 0 END) AS sent_10m "
            "FROM reply_events WHERE group_id = ? AND at >= ?",
            (
                current - 300,
                current - 300,
                current - 20,
                current - 600,
                current - 20,
                current - 600,
                int(group_id),
                current - 600,
            ),
        )
        reply_row = await cursor.fetchone()
        return {
            "human_5m": int((human_row["human_5m"] if human_row else 0) or 0),
            "human_sent_5m": int((reply_row["human_sent_5m"] if reply_row else 0) or 0),
            "ordinary_claimed_5m": int((reply_row["ordinary_claimed_5m"] if reply_row else 0) or 0),
            "ordinary_claimed_20s": int((reply_row["ordinary_claimed_20s"] if reply_row else 0) or 0),
            "ordinary_claimed_10m": int((reply_row["ordinary_claimed_10m"] if reply_row else 0) or 0),
            "sent_20s": int((reply_row["sent_20s"] if reply_row else 0) or 0),
            "sent_10m": int((reply_row["sent_10m"] if reply_row else 0) or 0),
        }

    async def admit_ordinary_reply(
        self, group_id: int, message_id: int, account_id: str
    ) -> bool:
        """原子执行群级普通回复预算，并记录一次排队占位。"""
        if not group_id or message_id <= 0:
            return False
        now = time.time()
        async with self._event_lock:
            async with aiosqlite.connect(self.db_path, timeout=30) as event_db:
                event_db.row_factory = aiosqlite.Row
                try:
                    await event_db.execute("BEGIN IMMEDIATE")
                    human_cursor = await event_db.execute(
                        "SELECT COUNT(*) AS n FROM group_events "
                        "WHERE group_id = ? AND sender_kind = 'human' AND observed_at >= ?",
                        (int(group_id), now - 300),
                    )
                    human_row = await human_cursor.fetchone()
                    human_5m = int((human_row["n"] if human_row else 0) or 0)
                    claim_cursor = await event_db.execute(
                        "SELECT "
                        "SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END) AS n20, "
                        "SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END) AS n5, "
                        "COUNT(*) AS n10 "
                        "FROM reply_events WHERE group_id = ? "
                        "AND stage = 'claimed' AND reason = 'human' AND at >= ?",
                        (now - 20, now - 300, int(group_id), now - 600),
                    )
                    claim_row = await claim_cursor.fetchone()
                    claimed_20s = int((claim_row["n20"] if claim_row else 0) or 0)
                    claimed_5m = int((claim_row["n5"] if claim_row else 0) or 0)
                    claimed_10m = int((claim_row["n10"] if claim_row else 0) or 0)
                    if claimed_10m >= 8 or (
                        human_5m >= 14 and (claimed_20s >= 1 or claimed_5m >= 2)
                    ):
                        await event_db.rollback()
                        return False
                    cursor = await event_db.execute(
                        "INSERT OR IGNORE INTO reply_events "
                        "(group_id, message_id, account_id, stage, reason, at) "
                        "VALUES (?, ?, ?, 'claimed', 'human', ?)",
                        (int(group_id), int(message_id), str(account_id), now),
                    )
                    await event_db.commit()
                    return cursor.rowcount == 1
                except BaseException:
                    await event_db.rollback()
                    raise

    async def reply_event_summary(self, hours: int = 24) -> dict[str, dict[str, int]]:
        cutoff = time.time() - max(1, int(hours)) * 3600
        cursor = await self._c.execute(
            "SELECT stage, reason, COUNT(*) AS n FROM reply_events "
            "WHERE at >= ? GROUP BY stage, reason ORDER BY stage, reason",
            (cutoff,),
        )
        summary: dict[str, dict[str, int]] = {}
        for row in await cursor.fetchall():
            summary.setdefault(str(row["stage"]), {})[str(row["reason"])] = int(row["n"])
        return summary

    @staticmethod
    def _billing_day() -> str:
        hkt = timezone(timedelta(hours=8))
        return datetime.now(hkt).date().isoformat()

    async def media_spend_total(self, day: str | None = None) -> float:
        billing_day = day or self._billing_day()
        cursor = await self._c.execute(
            "SELECT COALESCE(SUM(reserved_usd), 0) AS total "
            "FROM media_spend WHERE day = ?",
            (billing_day,),
        )
        row = await cursor.fetchone()
        return float(row["total"] if row else 0)

    async def reserve_media_budget(
        self,
        account_id: str,
        kind: str,
        estimated_usd: float,
        daily_cap_usd: float,
        *,
        day: str | None = None,
    ) -> bool:
        """在任何付費生成前原子預約額度；四帳號共用每日上限。"""
        amount = round(float(estimated_usd), 6)
        cap = round(float(daily_cap_usd), 6)
        if (
            not math.isfinite(amount)
            or not math.isfinite(cap)
            or amount <= 0
            or cap <= 0
            or amount > cap
        ):
            return False
        billing_day = day or self._billing_day()
        async with self._media_budget_lock:
            # 使用專用連線，避免其他訊息寫入的 commit 提前提交預算事務。
            async with aiosqlite.connect(
                self.db_path, timeout=30
            ) as budget_db:
                budget_db.row_factory = aiosqlite.Row
                try:
                    # SQLite write lock serializes the cap check across processes.
                    await budget_db.execute("BEGIN IMMEDIATE")
                    cursor = await budget_db.execute(
                        "SELECT COALESCE(SUM(reserved_usd), 0) AS total "
                        "FROM media_spend WHERE day = ?",
                        (billing_day,),
                    )
                    row = await cursor.fetchone()
                    total = float(row["total"] if row else 0)
                    if total + amount > cap + 1e-9:
                        await budget_db.rollback()
                        return False
                    reservation_id = (
                        f"{billing_day}:{time.time_ns()}:{account_id}:{kind}"
                    )
                    await budget_db.execute(
                        "INSERT INTO media_spend "
                        "(reservation_id, day, account_id, kind, reserved_usd, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            reservation_id,
                            billing_day,
                            account_id,
                            kind,
                            amount,
                            time.time(),
                        ),
                    )
                    await budget_db.commit()
                    return True
                except BaseException:
                    await budget_db.rollback()
                    raise

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

    async def mark_private_message_read(self,
                                       message_id: int,
                                       account_id: str | None = None) -> None:
        if account_id is not None:
            await self._c.execute(
                "UPDATE private_messages SET read = 1 WHERE id = ? AND account_id = ?",
                (message_id, account_id),
            )
            await self._c.commit()
            return
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

    async def last_group_activity(self, group_id: int, kind: str) -> float:
        cursor = await self._c.execute(
            "SELECT COALESCE(MAX(at), 0) AS at FROM activity "
            "WHERE group_id = ? AND kind = ?",
            (group_id, kind),
        )
        row = await cursor.fetchone()
        return float(row["at"] if row else 0.0)

    async def claim_message_response(
        self, group_id: int, message_id: int, account_id: str
    ) -> bool:
        if not group_id or message_id <= 0:
            return False
        key = f"reply:{group_id}:{message_id}"
        async with self._claim_lock:
            cursor = await self._c.execute(
                "INSERT OR IGNORE INTO outbound_claims "
                "(claim_key, account_id, claimed_at) VALUES (?, ?, ?)",
                (key, account_id, time.time()),
            )
            await self._c.commit()
            return cursor.rowcount == 1

    async def release_message_response_claim(
        self, group_id: int, message_id: int, account_id: str
    ) -> bool:
        if not group_id or message_id <= 0:
            return False
        key = f"reply:{group_id}:{message_id}"
        async with self._claim_lock:
            cursor = await self._c.execute(
                "DELETE FROM outbound_claims WHERE claim_key = ? AND account_id = ?",
                (key, account_id),
            )
            await self._c.commit()
            return cursor.rowcount == 1

    @staticmethod
    def _normalize_outbound_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(text)).casefold()
        return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)

    async def claim_group_text(
        self,
        group_id: int,
        text: str,
        account_id: str,
        window_seconds: float = 3600,
    ) -> bool:
        """跨帳號原子認領群組文案，阻止並發重複發送。"""
        normalized = self._normalize_outbound_text(text)
        if not group_id or not normalized or window_seconds <= 0:
            return False
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        key = f"text:{group_id}:{digest}"
        now = time.time()
        cutoff = now - float(window_seconds)
        async with self._claim_lock:
            cursor = await self._c.execute(
                "INSERT INTO outbound_claims (claim_key, account_id, claimed_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(claim_key) DO UPDATE SET "
                "account_id = excluded.account_id, claimed_at = excluded.claimed_at "
                "WHERE outbound_claims.claimed_at < ?",
                (key, account_id, now, cutoff),
            )
            await self._c.commit()
            return cursor.rowcount == 1

    async def claim_managed_followup(
        self,
        group_id: int,
        message_id: int,
        account_id: str,
        cooldown_seconds: float = 600,
    ) -> bool:
        """舊介面：原子認領並立即完成；僅供相容既有呼叫。"""
        reserved = await self.reserve_managed_followup(
            group_id,
            message_id,
            account_id,
            cooldown_seconds=cooldown_seconds,
        )
        if not reserved:
            return False
        return await self.complete_managed_followup(
            group_id,
            message_id,
            account_id,
            cooldown_seconds=cooldown_seconds,
        )

    async def reserve_managed_followup(
        self,
        group_id: int,
        message_id: int,
        account_id: str,
        pending_seconds: float = 120,
        cooldown_seconds: float = 600,
    ) -> bool:
        """先保留事件；生成或發送失敗可釋放，不提前消耗成功冷卻。"""
        if (
            not group_id
            or message_id <= 0
            or pending_seconds <= 0
            or cooldown_seconds <= 0
        ):
            return False
        now = time.time()
        event_key = f"managed-followup-event:{group_id}:{message_id}"
        pending_key = f"managed-followup-pending:{group_id}"
        cooldown_key = f"managed-followup-cooldown:{group_id}"
        async with self._claim_lock:
            async with aiosqlite.connect(self.db_path, timeout=30) as claim_db:
                try:
                    await claim_db.execute("BEGIN IMMEDIATE")
                    await claim_db.execute(
                        "DELETE FROM outbound_claims WHERE claim_key = ? AND claimed_at < ?",
                        (pending_key, now - float(pending_seconds)),
                    )
                    cursor = await claim_db.execute(
                        "SELECT claim_key, claimed_at FROM outbound_claims "
                        "WHERE claim_key IN (?, ?, ?)",
                        (event_key, pending_key, cooldown_key),
                    )
                    rows = await cursor.fetchall()
                    for claim_key, claimed_at in rows:
                        if claim_key in (event_key, pending_key):
                            await claim_db.rollback()
                            return False
                        if (
                            claim_key == cooldown_key
                            and float(claimed_at) >= now - float(cooldown_seconds)
                        ):
                            await claim_db.rollback()
                            return False
                    await claim_db.executemany(
                        "INSERT INTO outbound_claims "
                        "(claim_key, account_id, claimed_at) VALUES (?, ?, ?)",
                        [
                            (event_key, account_id, now),
                            (pending_key, account_id, now),
                        ],
                    )
                    await claim_db.commit()
                    return True
                except BaseException:
                    await claim_db.rollback()
                    raise

    async def complete_managed_followup(
        self,
        group_id: int,
        message_id: int,
        account_id: str,
        cooldown_seconds: float = 600,
    ) -> bool:
        """Telegram 真正送出後才提交群級冷卻。"""
        if not group_id or message_id <= 0 or cooldown_seconds <= 0:
            return False
        now = time.time()
        event_key = f"managed-followup-event:{group_id}:{message_id}"
        pending_key = f"managed-followup-pending:{group_id}"
        cooldown_key = f"managed-followup-cooldown:{group_id}"
        async with self._claim_lock:
            async with aiosqlite.connect(self.db_path, timeout=30) as claim_db:
                try:
                    await claim_db.execute("BEGIN IMMEDIATE")
                    cursor = await claim_db.execute(
                        "SELECT COUNT(*) FROM outbound_claims "
                        "WHERE claim_key IN (?, ?) AND account_id = ?",
                        (event_key, pending_key, account_id),
                    )
                    row = await cursor.fetchone()
                    if int(row[0] if row else 0) != 2:
                        await claim_db.rollback()
                        return False
                    await claim_db.execute(
                        "DELETE FROM outbound_claims "
                        "WHERE claim_key = ? AND account_id = ?",
                        (pending_key, account_id),
                    )
                    await claim_db.execute(
                        "INSERT INTO outbound_claims "
                        "(claim_key, account_id, claimed_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(claim_key) DO UPDATE SET "
                        "account_id = excluded.account_id, claimed_at = excluded.claimed_at",
                        (cooldown_key, account_id, now),
                    )
                    await claim_db.commit()
                    return True
                except BaseException:
                    await claim_db.rollback()
                    raise

    async def release_managed_followup(
        self, group_id: int, message_id: int, account_id: str
    ) -> None:
        """生成／發送失敗時釋放事件與暫存，不建立成功冷卻。"""
        event_key = f"managed-followup-event:{group_id}:{message_id}"
        pending_key = f"managed-followup-pending:{group_id}"
        async with self._claim_lock:
            async with aiosqlite.connect(self.db_path, timeout=30) as claim_db:
                try:
                    await claim_db.execute("BEGIN IMMEDIATE")
                    await claim_db.execute(
                        "DELETE FROM outbound_claims "
                        "WHERE claim_key IN (?, ?) AND account_id = ?",
                        (event_key, pending_key, account_id),
                    )
                    await claim_db.commit()
                except BaseException:
                    await claim_db.rollback()
                    raise

    async def claim_proactive_slot(
        self,
        group_id: int,
        slot: int,
        account_id: str,
        min_interval_seconds: float,
    ) -> bool:
        if not group_id or slot < 0:
            return False
        now = time.time()
        key = f"proactive:{group_id}:{slot}"
        cutoff = now - max(0.0, float(min_interval_seconds))
        async with self._claim_lock:
            cursor = await self._c.execute(
                "INSERT OR IGNORE INTO outbound_claims "
                "(claim_key, account_id, claimed_at) "
                "SELECT ?, ?, ? WHERE NOT EXISTS ("
                "SELECT 1 FROM activity WHERE group_id = ? "
                "AND kind = 'proactive' AND at > ?)",
                (key, account_id, now, group_id, cutoff),
            )
            await self._c.commit()
            return cursor.rowcount == 1

    async def claim_daily_voice(
        self,
        account_id: str,
        day_index: int,
        group_id: int | None = None,
        min_interval_seconds: float = 30 * 60,
    ) -> bool:
        """Atomically allow at most one proactive voice per account and HKT day.

        When ``group_id`` is provided, also require that no other account sent a
        voice in that group within ``min_interval_seconds`` (activity table,
        kind ``voice_proactive``) so managed accounts never post near-simultaneous
        voice notes into the same group.
        """
        if not str(account_id).strip() or int(day_index) < 0:
            return False
        key = f"daily-voice:{account_id}:{int(day_index)}"
        now = time.time()
        cutoff = now - max(0.0, float(min_interval_seconds))
        async with self._claim_lock:
            async with aiosqlite.connect(self.db_path, timeout=30) as claim_db:
                try:
                    await claim_db.execute("BEGIN IMMEDIATE")
                    cursor = await claim_db.execute(
                        "INSERT OR IGNORE INTO outbound_claims "
                        "(claim_key, account_id, claimed_at) VALUES (?, ?, ?)",
                        (key, str(account_id), now),
                    )
                    if cursor.rowcount != 1:
                        await claim_db.rollback()
                        return False
                    if group_id:
                        other = await claim_db.execute(
                            "SELECT 1 FROM activity WHERE group_id = ? "
                            "AND kind = 'voice_proactive' AND at > ? LIMIT 1",
                            (int(group_id), cutoff),
                        )
                        if await other.fetchone():
                            await claim_db.execute(
                                "DELETE FROM outbound_claims WHERE claim_key = ?",
                                (key,),
                            )
                            await claim_db.rollback()
                            return False
                    await claim_db.commit()
                    return True
                except BaseException:
                    await claim_db.rollback()
                    raise

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
        await self._c.execute("DELETE FROM group_events WHERE observed_at < ?", (cutoff,))
        await self._c.execute("DELETE FROM reply_events WHERE at < ?", (cutoff,))
        await self._c.commit()
