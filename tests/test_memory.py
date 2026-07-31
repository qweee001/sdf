from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.account import AccountRecord
from app.memory import MemoryStore


V2_ACCOUNT_COLUMNS = (
    "id",
    "label",
    "session_ciphertext",
    "session_fingerprint",
    "telegram_user_id",
    "telegram_name",
    "enabled",
    "gender",
    "stage",
    "style",
    "task_name",
    "task_info",
    "ai_base_url",
    "ai_model",
    "ai_api_key_ciphertext",
    "group_reply_probability",
    "reply_on_mention",
    "reply_on_reply",
    "typing_delay_min_seconds",
    "typing_delay_max_seconds",
    "proactive_enabled",
    "proactive_idle_minutes",
    "proactive_min_interval_minutes",
    "proactive_max_interval_minutes",
    "max_proactive_per_day",
    "all_groups",
    "group_ids",
    "revision",
    "created_at",
    "updated_at",
)


def account_record(account_id: str = "alpha", revision: int = 1) -> AccountRecord:
    now = int(time.time())
    return AccountRecord(
        id=account_id,
        label="測試帳號",
        session_ciphertext="encrypted-session",
        session_fingerprint=f"fingerprint-{account_id}",
        telegram_user_id=1000 if account_id == "alpha" else 2000,
        telegram_name="測試",
        enabled=False,
        gender="male",
        stage="observer",
        style="自然",
        task_name="測試任務",
        task_info="測試內容",
        ai_base_url="https://api.example.com/v1",
        ai_model="model-a",
        ai_api_key_ciphertext="",
        group_reply_probability=0.35,
        reply_on_mention=True,
        reply_on_reply=True,
        typing_delay_min_seconds=1.0,
        typing_delay_max_seconds=2.0,
        proactive_enabled=True,
        proactive_idle_minutes=15,
        proactive_min_interval_minutes=25,
        proactive_max_interval_minutes=60,
        max_proactive_per_day=12,
        all_groups=True,
        group_ids=frozenset(),
        revision=revision,
        created_at=now,
        updated_at=now,
    )


def rebuild_as_v2_account_schema(
    path: str,
    *,
    keep_blocked_terms: bool = False,
) -> None:
    columns = list(V2_ACCOUNT_COLUMNS)
    if keep_blocked_terms:
        columns.append("blocked_terms")
    selected = ", ".join(f'"{column}"' for column in columns)

    db = sqlite3.connect(path)
    db.execute("ALTER TABLE accounts RENAME TO accounts_v3_source")
    db.execute(f"CREATE TABLE accounts AS SELECT {selected} FROM accounts_v3_source")
    db.execute("DROP TABLE accounts_v3_source")
    db.execute("PRAGMA user_version=2")
    db.commit()
    db.close()


class MemoryStoreTests(unittest.TestCase):
    def test_memory_is_account_and_group_scoped_and_expires(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            now = int(time.time())
            await store.add("alpha", 100, 1, "甲", "user", "alpha", created_at=now)
            await store.add("beta", 100, 2, "乙", "user", "beta", created_at=now)
            await store.add(
                "alpha",
                100,
                3,
                "丙",
                "user",
                "expired",
                created_at=now - 24 * 60 * 60 - 1,
            )
            await store.add("alpha", 200, 4, "丁", "user", "other", created_at=now)

            recent = await store.recent_group("alpha", 100, 20)
            self.assertEqual([item.content for item in recent], ["alpha"])
            self.assertEqual(
                await store.statistics("alpha"),
                {"message_count": 2, "group_count": 2},
            )
            self.assertEqual(await store.clear_all("alpha"), 3)
            self.assertEqual(
                await store.statistics("beta"),
                {"message_count": 1, "group_count": 1},
            )
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_conversation_log_is_recent_bounded_ordered_and_redacted(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            now = int(time.time())
            for index in range(105):
                await store.add(
                    "alpha",
                    -1001,
                    10000 + index,
                    f"成員 {index:03d}",
                    "user" if index % 2 == 0 else "assistant",
                    f"message-{index:03d}",
                    created_at=now - 104 + index,
                )
            await store.add(
                "alpha",
                -1001,
                999,
                "過期成員",
                "user",
                "expired",
                created_at=now - 24 * 60 * 60 - 1,
            )
            await store.add(
                "beta",
                -1001,
                888,
                "其他帳號",
                "user",
                "other-account-secret",
                created_at=now,
            )
            await store.add(
                "alpha",
                -2002,
                777,
                "其他群組",
                "user",
                "other-group-secret",
                created_at=now,
            )

            entries = await store.conversation_log("alpha", -1001, 100)
            self.assertEqual(len(entries), 100)
            self.assertEqual(entries[0].content, "message-005")
            self.assertEqual(entries[-1].content, "message-104")
            self.assertEqual(
                [entry.created_at for entry in entries],
                sorted(entry.created_at for entry in entries),
            )
            self.assertTrue(
                all(
                    set(entry.__dict__)
                    == {"created_at", "sender_name", "role", "content"}
                    for entry in entries
                )
            )
            self.assertTrue(all(not hasattr(entry, "sender_id") for entry in entries))
            self.assertNotIn(
                "other-account-secret",
                [entry.content for entry in entries],
            )
            self.assertNotIn(
                "other-group-secret",
                [entry.content for entry in entries],
            )
            self.assertNotIn("expired", [entry.content for entry in entries])

            latest = await store.conversation_log("alpha", -1001, 3)
            self.assertEqual(
                [entry.content for entry in latest],
                ["message-102", "message-103", "message-104"],
            )
            for invalid_limit in (0, 101, True):
                with self.assertRaises(ValueError):
                    await store.conversation_log("alpha", -1001, invalid_limit)
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_old_message_schema_migrates_transactionally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.db")
            db = sqlite3.connect(path)
            db.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO messages
                    (group_id, sender_id, sender_name, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (100, 1, "舊成員", "user", "舊訊息", int(time.time())),
            )
            db.execute(
                "CREATE TABLE runtime_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO runtime_settings (key, value) VALUES ('ai_enabled', 'false')"
            )
            db.commit()
            db.close()

            async def scenario() -> None:
                store = MemoryStore(path, ttl_hours=24)
                await store.open()
                migrated = await store.recent_group("primary", 100, 10)
                self.assertEqual([item.content for item in migrated], ["舊訊息"])
                self.assertEqual(
                    await store.legacy_runtime_settings(),
                    {"ai_enabled": "false"},
                )
                await store.close()

                reopened = MemoryStore(path, ttl_hours=24)
                await reopened.open()
                self.assertEqual(
                    len(await reopened.recent_group("primary", 100, 10)),
                    1,
                )
                await reopened.close()

            asyncio.run(scenario())

    def test_account_crud_uses_revision_and_redacts_secrets(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            record = account_record()
            await store.create_account(record)
            loaded = await store.get_account("alpha")
            self.assertIsNotNone(loaded)
            self.assertNotIn("session_ciphertext", loaded.public_dict())

            saved = await store.update_account(
                record.with_updates(ai_model="model-b"),
                expected_revision=1,
                changed_fields=["ai_model"],
            )
            self.assertEqual(saved.revision, 2)
            self.assertEqual((await store.get_account("alpha")).ai_model, "model-b")
            with self.assertRaises(RuntimeError):
                await store.update_account(
                    record.with_updates(ai_model="stale"),
                    expected_revision=1,
                    changed_fields=["ai_model"],
                )
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_account_policy_round_trips_through_create_update_and_public_data(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            record = account_record().with_updates(
                blocked_terms=("邀約", "信用卡"),
                blocked_topics=("政治宣傳", "未成年相關內容"),
            )
            await store.create_account(record)

            loaded = await store.get_account("alpha")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.blocked_terms, ("邀約", "信用卡"))
            self.assertEqual(
                loaded.blocked_topics,
                ("政治宣傳", "未成年相關內容"),
            )
            self.assertEqual(
                loaded.public_dict()["blocked_terms"],
                ["邀約", "信用卡"],
            )
            self.assertEqual(
                loaded.public_dict()["blocked_topics"],
                ["政治宣傳", "未成年相關內容"],
            )

            saved = await store.update_account(
                loaded.with_updates(
                    blocked_terms=("投資群",),
                    blocked_topics=("借貸", "個資交換"),
                ),
                expected_revision=loaded.revision,
                changed_fields=["blocked_terms", "blocked_topics"],
            )
            self.assertEqual(saved.revision, 2)
            reloaded = await store.get_account("alpha")
            self.assertEqual(reloaded.blocked_terms, ("投資群",))
            self.assertEqual(reloaded.blocked_topics, ("借貸", "個資交換"))
            await store.close()

            db = sqlite3.connect(path)
            raw = db.execute(
                "SELECT blocked_terms, blocked_topics FROM accounts WHERE id = 'alpha'"
            ).fetchone()
            db.close()
            self.assertEqual(json.loads(raw[0]), ["投資群"])
            self.assertEqual(json.loads(raw[1]), ["借貸", "個資交換"])

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_v2_policy_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "v2.db")

            async def seed() -> None:
                store = MemoryStore(path, ttl_hours=24)
                await store.open()
                await store.create_account(account_record())
                await store.close()

            asyncio.run(seed())
            rebuild_as_v2_account_schema(path)

            async def migrate_twice() -> None:
                for _ in range(2):
                    store = MemoryStore(path, ttl_hours=24)
                    await store.open()
                    loaded = await store.get_account("alpha")
                    self.assertEqual(loaded.blocked_terms, ())
                    self.assertEqual(loaded.blocked_topics, ())
                    await store.close()

            asyncio.run(migrate_twice())

            db = sqlite3.connect(path)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(accounts)").fetchall()
            }
            version = db.execute("PRAGMA user_version").fetchone()[0]
            row = db.execute(
                "SELECT blocked_terms, blocked_topics FROM accounts WHERE id = 'alpha'"
            ).fetchone()
            db.close()
            self.assertIn("blocked_terms", columns)
            self.assertIn("blocked_topics", columns)
            self.assertEqual(version, 3)
            self.assertEqual(row, ("[]", "[]"))

    def test_policy_migration_repairs_an_interrupted_single_column_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "interrupted.db")

            async def seed() -> None:
                store = MemoryStore(path, ttl_hours=24)
                await store.open()
                await store.create_account(
                    account_record().with_updates(blocked_terms=("保留規則",))
                )
                await store.close()

            asyncio.run(seed())
            rebuild_as_v2_account_schema(path, keep_blocked_terms=True)

            async def repair() -> None:
                store = MemoryStore(path, ttl_hours=24)
                await store.open()
                loaded = await store.get_account("alpha")
                self.assertEqual(loaded.blocked_terms, ("保留規則",))
                self.assertEqual(loaded.blocked_topics, ())
                await store.close()

                reopened = MemoryStore(path, ttl_hours=24)
                await reopened.open()
                self.assertEqual(
                    (await reopened.get_account("alpha")).blocked_terms,
                    ("保留規則",),
                )
                await reopened.close()

            asyncio.run(repair())

            db = sqlite3.connect(path)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(accounts)").fetchall()
            }
            row = db.execute(
                "SELECT blocked_terms, blocked_topics FROM accounts WHERE id = 'alpha'"
            ).fetchone()
            db.close()
            self.assertIn("blocked_topics", columns)
            self.assertEqual(json.loads(row[0]), ["保留規則"])
            self.assertEqual(json.loads(row[1]), [])

    def test_first_account_adopts_orphaned_legacy_messages(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            await store.add(
                "primary",
                100,
                1,
                "舊成員",
                "user",
                "舊訊息",
            )
            await store.create_account(account_record("alpha"))
            self.assertEqual(await store.adopt_legacy_messages("alpha"), 1)
            self.assertEqual(
                [item.content for item in await store.recent_group("alpha", 100, 10)],
                ["舊訊息"],
            )
            self.assertEqual(await store.recent_group("primary", 100, 10), [])
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
