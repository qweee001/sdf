from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import aiosqlite


_FIXED_LIVE_TEST_ACCOUNT_IDS = {
    "2ce525dfb0d4",
    "faa9a202f96e",
    "038632e4395b",
    "e63e27a4340d",
}
_FIXED_LIVE_TEST_ACCOUNT_PROFILES = {
    "2ce525dfb0d4": "21",
    "faa9a202f96e": "25",
    "038632e4395b": "29",
    "e63e27a4340d": "34",
}
_FIXED_LIVE_TEST_GROUP_ID = -5428680940
_FIXED_LIVE_TEST_DURATION_SECONDS = 3_600
_FIXED_LIVE_TEST_EVENT_CAP = 40
_FIXED_LIVE_TEST_SCHEDULE_SIZE = 30


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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS live_test_runs (
                id TEXT PRIMARY KEY,
                account_ids TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL,
                event_cap INTEGER NOT NULL,
                schedule TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                stopped_at REAL,
                stop_reason TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_live_test_runs_status
            ON live_test_runs (status, started_at DESC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS live_test_events (
                run_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                reserved_at REAL NOT NULL,
                completed_at REAL,
                detail TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                snapshot_sha256 TEXT NOT NULL DEFAULT '',
                output_sha256 TEXT NOT NULL DEFAULT '',
                trigger_received_at REAL NOT NULL DEFAULT 0,
                snapshot_at REAL NOT NULL DEFAULT 0,
                profile_id TEXT NOT NULL DEFAULT '',
                content_sha256 TEXT NOT NULL DEFAULT '',
                decode_metadata_sha256 TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, event_id)
            )
        """)
        event_cols = await db.execute("PRAGMA table_info(live_test_events)")
        event_col_names = {r[1] for r in await event_cols.fetchall()}
        if "group_id" not in event_col_names:
            await db.execute(
                "ALTER TABLE live_test_events ADD COLUMN group_id INTEGER NOT NULL DEFAULT 0"
            )
        for column in ("request_id", "snapshot_sha256", "output_sha256"):
            if column not in event_col_names:
                await db.execute(
                    f"ALTER TABLE live_test_events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        for column in ("trigger_received_at", "snapshot_at"):
            if column not in event_col_names:
                await db.execute(
                    f"ALTER TABLE live_test_events ADD COLUMN {column} REAL NOT NULL DEFAULT 0"
                )
        for column in ("profile_id", "content_sha256", "decode_metadata_sha256"):
            if column not in event_col_names:
                await db.execute(
                    f"ALTER TABLE live_test_events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_live_test_events_run_state
            ON live_test_events (run_id, state, reserved_at)
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

    # ---------- 有界真人測試 ----------

    async def create_live_test_run(
        self,
        *,
        run_id: str,
        account_ids: list[str],
        group_id: int,
        duration_seconds: int,
        event_cap: int,
        schedule: list[dict],
        started_at: float | None = None,
    ) -> bool:
        """Persist one active bounded run without relying on process memory."""
        if (
            not isinstance(account_ids, list)
            or len(account_ids) != 4
            or len(set(account_ids)) != 4
            or not all(isinstance(account_id, str) for account_id in account_ids)
            or set(account_ids) != _FIXED_LIVE_TEST_ACCOUNT_IDS
            or isinstance(group_id, bool)
            or not isinstance(group_id, int)
            or group_id != _FIXED_LIVE_TEST_GROUP_ID
            or isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or duration_seconds != _FIXED_LIVE_TEST_DURATION_SECONDS
            or isinstance(event_cap, bool)
            or not isinstance(event_cap, int)
            or event_cap != _FIXED_LIVE_TEST_EVENT_CAP
            or not isinstance(schedule, list)
            or len(schedule) != _FIXED_LIVE_TEST_SCHEDULE_SIZE
        ):
            raise ValueError("fixed live-test envelope is required")
        identifier = str(run_id).strip()
        duration = int(duration_seconds)
        cap = int(event_cap)
        started = float(time.time() if started_at is None else started_at)
        if not identifier or duration <= 0 or cap <= 0:
            return False
        accounts_json = json.dumps(
            [str(account_id) for account_id in account_ids],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        schedule_json = json.dumps(
            schedule, ensure_ascii=False, separators=(",", ":")
        )
        async with aiosqlite.connect(self.db_path, timeout=30) as run_db:
            run_db.row_factory = aiosqlite.Row
            try:
                await run_db.execute("BEGIN IMMEDIATE")
                await run_db.execute(
                    "UPDATE live_test_runs SET status = 'needs_reconciliation', "
                    "stopped_at = NULL, stop_reason = 'expired' "
                    "WHERE status = 'running' AND expires_at <= ?",
                    (started,),
                )
                cursor = await run_db.execute(
                    "SELECT id FROM live_test_runs WHERE "
                    "status IN ('running', 'needs_reconciliation', 'lockdown') "
                    "OR (status = 'failed' AND stop_reason LIKE '%stop_failed%') LIMIT 1"
                )
                if await cursor.fetchone() is not None:
                    # Keep any expiry transition above durable while refusing
                    # to create a replacement run in the same write transaction.
                    await run_db.commit()
                    return False
                cursor = await run_db.execute(
                    "INSERT OR IGNORE INTO live_test_runs "
                    "(id, account_ids, group_id, duration_seconds, event_cap, "
                    "schedule, status, started_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                    (
                        identifier,
                        accounts_json,
                        int(group_id),
                        duration,
                        cap,
                        schedule_json,
                        started,
                        started + duration,
                    ),
                )
                await run_db.commit()
                return cursor.rowcount == 1
            except BaseException:
                await run_db.rollback()
                raise

    async def reserve_live_test_event(
        self,
        run_id: str,
        event_id: str,
        account_id: str,
        kind: str,
        *,
        group_id: int,
        scripted: bool,
        request_id: str = "",
        snapshot_sha256: str = "",
        output_sha256: str = "",
        trigger_received_at: float = 0.0,
        snapshot_at: float = 0.0,
        profile_id: str = "",
        content_sha256: str = "",
        decode_metadata_sha256: str = "",
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> bool:
        """Atomically consume one persistent dispatch slot before Telegram may run."""
        if not all(str(value).strip() for value in (run_id, event_id, account_id, kind)):
            return False
        if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id >= 0:
            return False
        if not isinstance(scripted, bool):
            return False
        kind_identifier = str(kind).strip()
        raw_request_identifier = str(request_id or "")
        raw_snapshot_hash = str(snapshot_sha256 or "")
        raw_output_hash = str(output_sha256 or "")
        raw_profile_id = str(profile_id or "")
        raw_content_hash = str(content_sha256 or "")
        raw_decode_hash = str(decode_metadata_sha256 or "")
        request_identifier = raw_request_identifier.strip()
        snapshot_hash = raw_snapshot_hash.strip().lower()
        output_hash = raw_output_hash.strip().lower()
        profile_identifier = raw_profile_id.strip()
        content_hash = raw_content_hash.strip().lower()
        decode_hash = raw_decode_hash.strip().lower()
        if len(request_identifier) > 200 or not self._valid_optional_sha256(
            snapshot_hash
        ) or any(
            not self._valid_optional_sha256(value)
            for value in (output_hash, content_hash, decode_hash)
        ):
            return False
        media_kind = kind_identifier in {"voice", "video"}
        valid_media_timestamps = (
            not isinstance(trigger_received_at, bool)
            and not isinstance(snapshot_at, bool)
            and isinstance(trigger_received_at, (int, float))
            and isinstance(snapshot_at, (int, float))
            and math.isfinite(float(trigger_received_at))
            and math.isfinite(float(snapshot_at))
            and float(trigger_received_at) >= 0
            and float(snapshot_at) >= float(trigger_received_at)
        )
        if kind_identifier in {"voice", "video"} and (
            re.fullmatch(r"[0-9a-f]{32}", request_identifier) is None
            or not snapshot_hash
            or not output_hash
            or not content_hash
            or not valid_media_timestamps
            or profile_identifier
            != _FIXED_LIVE_TEST_ACCOUNT_PROFILES.get(str(account_id), "")
            or (kind_identifier == "voice" and bool(decode_hash))
            or (kind_identifier == "video" and not decode_hash)
            or raw_request_identifier != request_identifier
            or raw_snapshot_hash != snapshot_hash
            or raw_output_hash != output_hash
            or raw_profile_id != profile_identifier
            or raw_content_hash != content_hash
            or raw_decode_hash != decode_hash
        ):
            return False
        if not media_kind and (
            request_identifier
            or snapshot_hash
            or output_hash
            or isinstance(trigger_received_at, bool)
            or not isinstance(trigger_received_at, (int, float))
            or float(trigger_received_at) != 0.0
            or isinstance(snapshot_at, bool)
            or not isinstance(snapshot_at, (int, float))
            or float(snapshot_at) != 0.0
            or profile_identifier
            or content_hash
            or decode_hash
        ):
            return False
        async with aiosqlite.connect(self.db_path, timeout=30) as event_db:
            event_db.row_factory = aiosqlite.Row
            try:
                # The write lock serializes cap checks across processes/connections.
                await event_db.execute("BEGIN IMMEDIATE")
                # Read time only after acquiring the write lock. A reservation
                # may wait here behind another SQLite writer long enough for
                # the bounded run to expire.
                current = float(
                    clock() if clock is not None else time.time() if now is None else now
                )
                cursor = await event_db.execute(
                    "SELECT status, expires_at, event_cap, account_ids, group_id, schedule "
                    "FROM live_test_runs "
                    "WHERE id = ?",
                    (str(run_id),),
                )
                run = await cursor.fetchone()
                if run is None or str(run["status"]) != "running":
                    await event_db.rollback()
                    return False
                if current >= float(run["expires_at"]):
                    await event_db.execute(
                        "UPDATE live_test_runs SET status = 'needs_reconciliation', "
                        "stopped_at = NULL, stop_reason = 'expired' WHERE id = ?",
                        (str(run_id),),
                    )
                    await event_db.commit()
                    return False
                try:
                    authorized_accounts = {
                        str(value) for value in json.loads(str(run["account_ids"]))
                    }
                    schedule = json.loads(str(run["schedule"]))
                except (TypeError, json.JSONDecodeError):
                    await event_db.rollback()
                    return False
                if (
                    str(account_id) not in authorized_accounts
                    or int(group_id) != int(run["group_id"])
                ):
                    await event_db.rollback()
                    return False
                if scripted:
                    matching = [
                        event
                        for event in schedule
                        if isinstance(event, dict)
                        and str(event.get("event_id") or "") == str(event_id)
                    ]
                    if len(matching) != 1 or (
                        str(matching[0].get("account_id") or "") != str(account_id)
                        or str(matching[0].get("kind") or "") != kind_identifier
                    ):
                        await event_db.rollback()
                        return False
                elif not str(event_id).startswith("organic:"):
                    await event_db.rollback()
                    return False
                if request_identifier:
                    cursor = await event_db.execute(
                        "SELECT 1 FROM live_test_events "
                        "WHERE run_id = ? AND request_id = ? LIMIT 1",
                        (str(run_id), request_identifier),
                    )
                    if await cursor.fetchone() is not None:
                        await event_db.rollback()
                        return False
                cursor = await event_db.execute(
                    "SELECT 1 FROM live_test_events "
                    "WHERE run_id = ? AND event_id = ?",
                    (str(run_id), str(event_id)),
                )
                if await cursor.fetchone() is not None:
                    await event_db.rollback()
                    return False
                cursor = await event_db.execute(
                    "SELECT COUNT(*) AS n FROM live_test_events WHERE run_id = ? "
                    "AND state IN ('reserved', 'rpc_started', 'sent', 'failed', "
                    "'hard_attempt')",
                    (str(run_id),),
                )
                count_row = await cursor.fetchone()
                reserved = int(count_row["n"] if count_row else 0)
                if reserved >= int(run["event_cap"]):
                    await event_db.rollback()
                    return False
                await event_db.execute(
                    "INSERT INTO live_test_events "
                    "(run_id, event_id, account_id, group_id, kind, state, reserved_at, "
                    "request_id, snapshot_sha256, output_sha256, trigger_received_at, "
                    "snapshot_at, profile_id, content_sha256, decode_metadata_sha256) "
                    "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(run_id),
                        str(event_id),
                        str(account_id),
                        int(group_id),
                        kind_identifier,
                        current,
                        request_identifier,
                        snapshot_hash,
                        output_hash,
                        float(trigger_received_at),
                        float(snapshot_at),
                        profile_identifier,
                        content_hash,
                        decode_hash,
                    ),
                )
                await event_db.commit()
                return True
            except BaseException:
                await event_db.rollback()
                raise

    async def mark_live_test_event_rpc_started(
        self,
        run_id: str,
        event_id: str,
        *,
        account_id: str,
        group_id: int,
        kind: str,
        request_id: str = "",
        snapshot_sha256: str = "",
        output_sha256: str = "",
        trigger_received_at: float = 0.0,
        snapshot_at: float = 0.0,
        profile_id: str = "",
        content_sha256: str = "",
        decode_metadata_sha256: str = "",
    ) -> bool:
        """Persist the last durable boundary immediately before the RPC."""

        normalized = {
            "kind": str(kind or "").strip().lower(),
            "request_id": str(request_id or "").strip().lower(),
            "snapshot_sha256": str(snapshot_sha256 or "").strip().lower(),
            "output_sha256": str(output_sha256 or "").strip().lower(),
            "profile_id": str(profile_id or "").strip(),
            "content_sha256": str(content_sha256 or "").strip().lower(),
            "decode_metadata_sha256": str(
                decode_metadata_sha256 or ""
            ).strip().lower(),
        }
        try:
            trigger_value = float(trigger_received_at)
            snapshot_value = float(snapshot_at)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(trigger_value) or not math.isfinite(snapshot_value):
            return False

        async with aiosqlite.connect(self.db_path, timeout=30) as event_db:
            event_db.row_factory = aiosqlite.Row
            await event_db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await event_db.execute(
                    """
                    SELECT account_id, group_id, kind, state, request_id,
                           snapshot_sha256, output_sha256,
                           trigger_received_at, snapshot_at, profile_id,
                           content_sha256, decode_metadata_sha256
                    FROM live_test_events
                    WHERE run_id=? AND event_id=?
                    """,
                    (run_id, event_id),
                )
                row = await cursor.fetchone()
                if row is None or row[3] != "reserved":
                    await event_db.rollback()
                    return False
                expected = (
                    str(account_id),
                    int(group_id),
                    normalized["kind"],
                    normalized["request_id"],
                    normalized["snapshot_sha256"],
                    normalized["output_sha256"],
                    trigger_value,
                    snapshot_value,
                    normalized["profile_id"],
                    normalized["content_sha256"],
                    normalized["decode_metadata_sha256"],
                )
                actual = (
                    str(row[0]),
                    int(row[1]),
                    str(row[2] or "").strip().lower(),
                    str(row[4] or "").strip().lower(),
                    str(row[5] or "").strip().lower(),
                    str(row[6] or "").strip().lower(),
                    float(row[7] or 0.0),
                    float(row[8] or 0.0),
                    str(row[9] or "").strip(),
                    str(row[10] or "").strip().lower(),
                    str(row[11] or "").strip().lower(),
                )
                if actual != expected:
                    await event_db.rollback()
                    return False
                cur = await event_db.execute(
                    """
                    UPDATE live_test_events
                    SET state='rpc_started', detail='rpc_started'
                    WHERE run_id=? AND event_id=? AND state='reserved'
                    """,
                    (run_id, event_id),
                )
                if cur.rowcount != 1:
                    await event_db.rollback()
                    return False
                await event_db.commit()
                return True
            except BaseException:
                await event_db.rollback()
                raise

    async def reconcile_live_test_events(
        self,
        run_id: str,
        reason: str,
        *,
        now: float | None = None,
    ) -> dict[str, int]:
        """Resolve every non-terminal reservation before a run is finalized."""

        completed_at = float(time.time() if now is None else now)
        if not math.isfinite(completed_at):
            raise ValueError("reconciliation timestamp must be finite")
        safe_reason = str(reason or "reconciliation")[:500]
        async with aiosqlite.connect(self.db_path, timeout=30) as event_db:
            await event_db.execute("BEGIN IMMEDIATE")
            try:
                released = await event_db.execute(
                    """
                    UPDATE live_test_events
                    SET state='released', completed_at=?, detail=?
                    WHERE run_id=? AND state='reserved'
                    """,
                    (completed_at, f"reconciled_pre_rpc: {safe_reason}", run_id),
                )
                hard = await event_db.execute(
                    """
                    UPDATE live_test_events
                    SET state='hard_attempt', completed_at=?, detail=?
                    WHERE run_id=? AND state='rpc_started'
                    """,
                    (completed_at, f"reconciled_rpc_unknown: {safe_reason}", run_id),
                )
                await event_db.commit()
                return {
                    "released": int(released.rowcount),
                    "hard_attempt": int(hard.rowcount),
                }
            except BaseException:
                await event_db.rollback()
                raise

    async def release_live_test_event_bound(
        self,
        run_id: str,
        event_id: str,
        *,
        account_id: str,
        group_id: int,
        kind: str,
        request_id: str = "",
        snapshot_sha256: str = "",
        output_sha256: str = "",
        trigger_received_at: float = 0.0,
        snapshot_at: float = 0.0,
        profile_id: str = "",
        content_sha256: str = "",
        decode_metadata_sha256: str = "",
        detail: str = "",
    ) -> bool:
        """Release a pre-RPC reservation using only trusted bound identity."""

        expected = {
            "account_id": str(account_id),
            "group_id": int(group_id),
            "kind": str(kind or "").strip(),
            "request_id": str(request_id or "").strip(),
            "snapshot_sha256": str(snapshot_sha256 or "").strip().lower(),
            "output_sha256": str(output_sha256 or "").strip().lower(),
            "trigger_received_at": float(trigger_received_at),
            "snapshot_at": float(snapshot_at),
            "profile_id": str(profile_id or "").strip(),
            "content_sha256": str(content_sha256 or "").strip().lower(),
            "decode_metadata_sha256": str(
                decode_metadata_sha256 or ""
            ).strip().lower(),
        }
        if any(
            not math.isfinite(expected[key])
            for key in ("trigger_received_at", "snapshot_at")
        ):
            return False
        async with aiosqlite.connect(self.db_path, timeout=30) as event_db:
            event_db.row_factory = aiosqlite.Row
            await event_db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await event_db.execute(
                    """
                    SELECT account_id, group_id, kind, state, request_id,
                           snapshot_sha256, output_sha256,
                           trigger_received_at, snapshot_at, profile_id,
                           content_sha256, decode_metadata_sha256
                    FROM live_test_events
                    WHERE run_id=? AND event_id=?
                    """,
                    (str(run_id), str(event_id)),
                )
                row = await cursor.fetchone()
                if row is None or str(row["state"]) != "reserved":
                    await event_db.rollback()
                    return False
                actual = {
                    "account_id": str(row["account_id"]),
                    "group_id": int(row["group_id"]),
                    "kind": str(row["kind"] or ""),
                    "request_id": str(row["request_id"] or ""),
                    "snapshot_sha256": str(row["snapshot_sha256"] or ""),
                    "output_sha256": str(row["output_sha256"] or ""),
                    "trigger_received_at": float(row["trigger_received_at"] or 0.0),
                    "snapshot_at": float(row["snapshot_at"] or 0.0),
                    "profile_id": str(row["profile_id"] or ""),
                    "content_sha256": str(row["content_sha256"] or ""),
                    "decode_metadata_sha256": str(
                        row["decode_metadata_sha256"] or ""
                    ),
                }
                if actual != expected:
                    await event_db.rollback()
                    return False
                updated = await event_db.execute(
                    """
                    UPDATE live_test_events
                    SET state='released', completed_at=?, detail=?
                    WHERE run_id=? AND event_id=? AND state='reserved'
                    """,
                    (time.time(), str(detail)[:500], str(run_id), str(event_id)),
                )
                if updated.rowcount != 1:
                    await event_db.rollback()
                    return False
                await event_db.commit()
                return True
            except BaseException:
                await event_db.rollback()
                raise

    async def finish_live_test_event(
        self,
        run_id: str,
        event_id: str,
        state: str,
        detail: str = "",
        *,
        kind: str = "",
        request_id: str = "",
        snapshot_sha256: str = "",
        output_sha256: str = "",
        trigger_received_at: float = 0.0,
        snapshot_at: float = 0.0,
        profile_id: str = "",
        content_sha256: str = "",
        decode_metadata_sha256: str = "",
        rpc_started: bool = False,
    ) -> bool:
        if state not in {"sent", "failed", "hard_attempt", "released", "cancelled"}:
            return False
        requested_audit = {
            "kind": str(kind or ""),
            "request_id": str(request_id or ""),
            "snapshot_sha256": str(snapshot_sha256 or ""),
            "output_sha256": str(output_sha256 or ""),
            "profile_id": str(profile_id or ""),
            "content_sha256": str(content_sha256 or ""),
            "decode_metadata_sha256": str(decode_metadata_sha256 or ""),
        }
        normalized_audit = {
            "kind": requested_audit["kind"].strip(),
            "request_id": requested_audit["request_id"].strip(),
            "snapshot_sha256": requested_audit["snapshot_sha256"].strip().lower(),
            "output_sha256": requested_audit["output_sha256"].strip().lower(),
            "profile_id": requested_audit["profile_id"].strip(),
            "content_sha256": requested_audit["content_sha256"].strip().lower(),
            "decode_metadata_sha256": requested_audit[
                "decode_metadata_sha256"
            ].strip().lower(),
        }
        if len(normalized_audit["request_id"]) > 200 or any(
            not self._valid_optional_sha256(normalized_audit[key])
            for key in (
                "snapshot_sha256",
                "output_sha256",
                "content_sha256",
                "decode_metadata_sha256",
            )
        ):
            return False
        async with aiosqlite.connect(self.db_path, timeout=30) as event_db:
            event_db.row_factory = aiosqlite.Row
            try:
                await event_db.execute("BEGIN IMMEDIATE")
                cursor = await event_db.execute(
                    "SELECT state, kind, request_id, snapshot_sha256, output_sha256, "
                    "trigger_received_at, snapshot_at, profile_id, content_sha256, "
                    "decode_metadata_sha256 "
                    "FROM live_test_events WHERE run_id = ? AND event_id = ?",
                    (str(run_id), str(event_id)),
                )
                current = await cursor.fetchone()
                required_state = (
                    "rpc_started"
                    if state in {"sent", "hard_attempt"} or rpc_started
                    else "reserved"
                )
                if current is None or str(current["state"]) != required_state:
                    await event_db.rollback()
                    return False
                if state not in {"released", "cancelled"}:
                    current_kind = str(current["kind"])
                    if current_kind in {"voice", "video"}:
                        if (
                            requested_audit != normalized_audit
                            or any(
                                not normalized_audit[key]
                                for key in (
                                    "kind",
                                    "request_id",
                                    "snapshot_sha256",
                                    "output_sha256",
                                    "profile_id",
                                    "content_sha256",
                                )
                            )
                            or (
                                current_kind == "voice"
                                and bool(normalized_audit["decode_metadata_sha256"])
                            )
                            or (
                                current_kind == "video"
                                and not normalized_audit["decode_metadata_sha256"]
                            )
                            or any(
                                str(current[key] or "") != normalized_audit[key]
                                for key in normalized_audit
                            )
                            or not isinstance(trigger_received_at, (int, float))
                            or isinstance(trigger_received_at, bool)
                            or not isinstance(snapshot_at, (int, float))
                            or isinstance(snapshot_at, bool)
                            or not math.isfinite(float(trigger_received_at))
                            or not math.isfinite(float(snapshot_at))
                            or float(current["trigger_received_at"])
                            != float(trigger_received_at)
                            or float(current["snapshot_at"]) != float(snapshot_at)
                        ):
                            await event_db.rollback()
                            return False
                    elif (
                        normalized_audit["kind"] != current_kind
                        or any(
                            normalized_audit[key]
                            for key in normalized_audit
                            if key != "kind"
                        )
                        or any(
                            value != 0.0
                            for value in (trigger_received_at, snapshot_at)
                        )
                    ):
                        await event_db.rollback()
                        return False
                cursor = await event_db.execute(
                    "UPDATE live_test_events SET state = ?, completed_at = ?, detail = ? "
                    "WHERE run_id = ? AND event_id = ? AND state = ?",
                    (
                        state,
                        time.time(),
                        str(detail)[:500],
                        str(run_id),
                        str(event_id),
                        required_state,
                    ),
                )
                await event_db.commit()
                return cursor.rowcount == 1
            except BaseException:
                await event_db.rollback()
                raise

    @staticmethod
    def _valid_optional_sha256(value: str) -> bool:
        return not value or re.fullmatch(r"[0-9a-f]{64}", value) is not None

    async def get_live_test_event(self, run_id: str, event_id: str) -> dict | None:
        cursor = await self._c.execute(
            "SELECT run_id, event_id, account_id, group_id, kind, state, reserved_at, "
            "completed_at, detail, request_id, snapshot_sha256, output_sha256, "
            "trigger_received_at, snapshot_at, profile_id, content_sha256, "
            "decode_metadata_sha256 "
            "FROM live_test_events WHERE run_id = ? AND event_id = ?",
            (str(run_id), str(event_id)),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def mark_live_test_needs_reconciliation(
        self, run_id: str, reason: str
    ) -> bool:
        cursor = await self._c.execute(
            "UPDATE live_test_runs SET status = 'needs_reconciliation', "
            "stopped_at = NULL, stop_reason = ? WHERE id = ? AND ("
            "status IN ('running', 'needs_reconciliation', 'lockdown', 'expired') OR "
            "(status = 'failed' AND stop_reason LIKE '%stop_failed%'))",
            (str(reason)[:500], str(run_id)),
        )
        await self._c.commit()
        return cursor.rowcount == 1

    async def get_live_test_reconciliation_run(self) -> dict | None:
        cursor = await self._c.execute(
            "SELECT id FROM live_test_runs WHERE "
            "status IN ('running', 'needs_reconciliation', 'lockdown') OR "
            "(status = 'expired' AND stop_reason = 'expired') OR "
            "(status = 'failed' AND stop_reason LIKE '%stop_failed%') "
            "ORDER BY started_at ASC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return await self.get_live_test_run(str(row["id"]))

    async def has_live_test_reconciliation(self) -> bool:
        return await self.get_live_test_reconciliation_run() is not None

    async def finish_live_test_run(
        self,
        run_id: str,
        status: str,
        reason: str = "",
        *,
        stopped_at: float | None = None,
    ) -> bool:
        if status not in {"completed", "stopped", "expired", "failed"}:
            return False
        cursor = await self._c.execute(
            "UPDATE live_test_runs SET status = ?, stopped_at = ?, stop_reason = ? "
            "WHERE id = ? AND status IN "
            "('running', 'needs_reconciliation', 'lockdown', 'expired')",
            (
                status,
                float(time.time() if stopped_at is None else stopped_at),
                str(reason)[:500],
                str(run_id),
            ),
        )
        await self._c.commit()
        return cursor.rowcount == 1

    async def get_live_test_run(self, run_id: str | None = None) -> dict | None:
        if run_id is None:
            cursor = await self._c.execute(
                "SELECT * FROM live_test_runs ORDER BY started_at DESC LIMIT 1"
            )
        else:
            cursor = await self._c.execute(
                "SELECT * FROM live_test_runs WHERE id = ?", (str(run_id),)
            )
        row = await cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["account_ids"] = json.loads(result["account_ids"])
            result["schedule"] = json.loads(result["schedule"])
        except (TypeError, json.JSONDecodeError):
            result["account_ids"] = []
            result["schedule"] = []
        counts_cursor = await self._c.execute(
            "SELECT state, COUNT(*) AS n FROM live_test_events "
            "WHERE run_id = ? GROUP BY state",
            (str(result["id"]),),
        )
        counts = {
            str(item["state"]): int(item["n"])
            for item in await counts_cursor.fetchall()
        }
        result["reserved"] = sum(counts.values())
        result["pending"] = counts.get("reserved", 0) + counts.get(
            "rpc_started", 0
        )
        result["sent"] = counts.get("sent", 0)
        result["failed"] = counts.get("failed", 0)
        result["hard_attempt"] = counts.get("hard_attempt", 0)
        result["released"] = counts.get("released", 0)
        result["cancelled"] = counts.get("cancelled", 0)
        result["cap_used"] = sum(
            counts.get(state, 0)
            for state in (
                "reserved",
                "rpc_started",
                "sent",
                "failed",
                "hard_attempt",
            )
        )
        return result

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

    async def recent_bot_texts_by_group(
        self, group_id: int, *, hours: float = 48, limit: int = 200
    ) -> list[str]:
        """跨所有帳號讀取群內近 N 小時實際送出的文案，供主動話題持久去重。

        messages 表本來就持久保存所有 bot 發送（role='assistant'），
        重啟後從這裡回填即可，無需新表。
        """
        cutoff = time.time() - float(hours) * 3600
        cursor = await self._c.execute(
            "SELECT content FROM messages "
            "WHERE group_id = ? AND role = 'assistant' AND timestamp >= ? "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (int(group_id), float(cutoff), int(limit)),
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

    async def last_human_activity_by_group(
        self, *, now: float | None = None
    ) -> dict[int, float]:
        """每群最近一次真人活動時間；進程重啟後用於回填降頻記憶。"""
        window = 24 * 3600
        current = float(now if now is not None else time.time())
        cursor = await self._c.execute(
            "SELECT group_id, MAX(observed_at) AS at FROM group_events "
            "WHERE sender_kind = 'human' AND observed_at >= ? "
            "GROUP BY group_id",
            (float(now if now is not None else time.time()) - window,),
        )
        rows = await cursor.fetchall()
        return {
            int(row["group_id"]): float(row["at"])
            for row in rows
            if row["at"]
        }

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

    async def ensure_managed_followup_cooldown(
        self,
        group_id: int,
        account_id: str,
        cooldown_seconds: float = 600,
        *,
        now: float | None = None,
    ) -> None:
        """發送成功後的冷卻兜底：即使 reserve/complete 鏈斷裂也強制寫入群級冷卻。

        觀察到的真實故障：互聊洪水期間 cooldown 行缺失，多條 followup 在
        600 秒內連發。此方法幂等，發送成功後必須呼叫。
        """
        if not group_id or cooldown_seconds <= 0:
            return
        cooldown_key = f"managed-followup-cooldown:{group_id}"
        current = float(now if now is not None else time.time())
        async with self._claim_lock:
            cursor = await self._c.execute(
                "INSERT INTO outbound_claims "
                "(claim_key, account_id, claimed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(claim_key) DO UPDATE SET "
                "account_id = excluded.account_id, "
                "claimed_at = MAX(claimed_at, excluded.claimed_at)",
                (cooldown_key, account_id, current),
            )
            await self._c.commit()

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

    async def reserve_continuous_slot(
        self,
        group_id: int,
        slot: int,
        account_id: str,
        min_interval_seconds: float,
        pending_seconds: float,
    ) -> bool:
        """Serialize one continuous generation/send pipeline per group."""
        if (
            not group_id
            or slot < 0
            or not account_id
            or not math.isfinite(float(min_interval_seconds))
            or not math.isfinite(float(pending_seconds))
            or min_interval_seconds <= 0
            or pending_seconds <= 0
        ):
            return False
        now = time.time()
        event_key = f"continuous-slot:{group_id}:{slot}"
        pending_key = f"continuous-pending:{group_id}"
        event_prefix = f"continuous-slot:{group_id}:%"
        cutoff = now - float(min_interval_seconds)
        pending_cutoff = now - float(pending_seconds)
        cleanup_cutoff = now - max(3600.0, float(pending_seconds))
        async with self._claim_lock:
            async with aiosqlite.connect(self.db_path, timeout=30) as claim_db:
                try:
                    await claim_db.execute("BEGIN IMMEDIATE")
                    await claim_db.execute(
                        "DELETE FROM outbound_claims "
                        "WHERE claim_key = ? AND claimed_at < ?",
                        (pending_key, pending_cutoff),
                    )
                    await claim_db.execute(
                        "DELETE FROM outbound_claims "
                        "WHERE claim_key LIKE ? AND claimed_at < ?",
                        (event_prefix, cleanup_cutoff),
                    )
                    cursor = await claim_db.execute(
                        "SELECT 1 FROM outbound_claims "
                        "WHERE claim_key IN (?, ?) LIMIT 1",
                        (event_key, pending_key),
                    )
                    if await cursor.fetchone() is not None:
                        await claim_db.rollback()
                        return False
                    cursor = await claim_db.execute(
                        "SELECT 1 FROM activity WHERE group_id = ? "
                        "AND kind = 'proactive' AND at > ? LIMIT 1",
                        (int(group_id), cutoff),
                    )
                    if await cursor.fetchone() is not None:
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

    async def complete_continuous_slot(
        self, group_id: int, slot: int, account_id: str
    ) -> bool:
        event_key = f"continuous-slot:{group_id}:{slot}"
        pending_key = f"continuous-pending:{group_id}"
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
                    await claim_db.commit()
                    return True
                except BaseException:
                    await claim_db.rollback()
                    raise

    async def release_continuous_slot(
        self, group_id: int, slot: int, account_id: str
    ) -> bool:
        event_key = f"continuous-slot:{group_id}:{slot}"
        pending_key = f"continuous-pending:{group_id}"
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
                        "WHERE claim_key IN (?, ?) AND account_id = ?",
                        (event_key, pending_key, account_id),
                    )
                    await claim_db.commit()
                    return True
                except BaseException:
                    await claim_db.rollback()
                    raise

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
