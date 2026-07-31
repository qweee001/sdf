from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.account import AccountRecord
from app.memory import MemoryStore


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
