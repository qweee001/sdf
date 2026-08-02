from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

from telethon.tl.types import User

from app.account import AccountRecord
from app.manager import AccountManager
from app.memory import MemoryStore, SCHEMA_VERSION
from app.worker import AccountWorker


def account_record(account_id: str, telegram_user_id: int) -> AccountRecord:
    now = int(time.time())
    return AccountRecord(
        id=account_id,
        label=f"Account {account_id}",
        session_ciphertext="encrypted-session",
        session_fingerprint=f"fingerprint-{account_id}",
        telegram_user_id=telegram_user_id,
        telegram_name=f"Telegram {account_id}",
        enabled=False,
        gender="male",
        stage="observer",
        style="natural",
        task_name="group chat",
        task_info="group only",
        ai_base_url="https://api.example.com/v1",
        ai_model="model-a",
        ai_api_key_ciphertext="",
        group_reply_probability=0.35,
        reply_on_mention=True,
        reply_on_reply=True,
        typing_delay_min_seconds=0,
        typing_delay_max_seconds=0,
        proactive_enabled=False,
        proactive_idle_minutes=15,
        proactive_min_interval_minutes=25,
        proactive_max_interval_minutes=60,
        max_proactive_per_day=0,
        all_groups=True,
        group_ids=frozenset(),
        revision=1,
        created_at=now,
        updated_at=now,
    )


class PrivateAlertMemoryTests(unittest.TestCase):
    def test_alerts_are_private_bounded_deduplicated_and_acknowledgeable(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            await store.create_account(account_record("alpha", 1001))
            await store.create_account(account_record("beta", 1002))
            now = int(time.time())

            inserted = await store.add_private_alert(
                "alpha",
                "fingerprint-a",
                10,
                " Alice\x00\nName ",
                " hello\x07\nworld ",
                created_at=now,
            )
            duplicate = await store.add_private_alert(
                "alpha",
                "fingerprint-a",
                10,
                "Changed",
                "duplicate",
                created_at=now,
            )
            await store.add_private_alert(
                "beta",
                "fingerprint-a",
                10,
                "Other account",
                "secret",
                created_at=now,
            )
            self.assertTrue(inserted)
            self.assertFalse(duplicate)

            entries = await store.list_private_alerts("alpha")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].sender_name, "Alice Name")
            self.assertEqual(entries[0].preview, "hello world")
            self.assertEqual(
                set(entries[0].__dict__),
                {
                    "alert_id",
                    "sender_name",
                    "preview",
                    "created_at",
                    "acknowledged",
                },
            )
            uuid.UUID(entries[0].alert_id)
            self.assertEqual(await store.private_unread_count("alpha"), 1)
            self.assertTrue(
                await store.acknowledge_private_alert(
                    "alpha", entries[0].alert_id, now=now + 1
                )
            )
            self.assertFalse(
                await store.acknowledge_private_alert(
                    "alpha", entries[0].alert_id, now=now + 2
                )
            )
            self.assertFalse(
                await store.add_private_alert(
                    "alpha",
                    "fingerprint-a",
                    10,
                    "Alice",
                    "restored after restart",
                    created_at=now,
                )
            )
            self.assertEqual(await store.private_unread_count("alpha"), 0)
            self.assertEqual(await store.private_unread_count("beta"), 1)

            for index in range(205):
                await store.add_private_alert(
                    "alpha",
                    f"fingerprint-{index}",
                    index + 100,
                    "N" * 140,
                    "P" * 320,
                    created_at=now + index,
                )
            cursor = await store._connection().execute(
                "SELECT COUNT(*) AS total FROM private_alerts WHERE account_id='alpha'"
            )
            row = await cursor.fetchone()
            await cursor.close()
            self.assertEqual(int(row["total"]), 200)
            newest = await store.list_private_alerts("alpha", 100)
            self.assertTrue(all(len(item.sender_name) <= 120 for item in newest))
            self.assertTrue(all(len(item.preview) <= 280 for item in newest))
            self.assertEqual(
                await store.acknowledge_all_private_alerts("alpha", now=now + 300),
                200,
            )
            await store.close()

            raw = sqlite3.connect(path)
            version = raw.execute("PRAGMA user_version").fetchone()[0]
            columns = {
                row[1] for row in raw.execute("PRAGMA table_info(private_alerts)")
            }
            raw.close()
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertNotIn("username", columns)
            self.assertNotIn("phone", columns)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_alert_query_and_purge_enforce_ttl(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            await store.create_account(account_record("alpha", 1001))
            now = int(time.time())
            await store.add_private_alert(
                "alpha",
                "fresh",
                1,
                "Fresh",
                "fresh",
                created_at=now,
            )
            # Insert directly so add_private_alert's eager account cleanup does
            # not remove the row before query-cutoff behaviour is exercised.
            await store._connection().execute(
                """
                INSERT INTO private_alerts
                    (id, account_id, sender_fingerprint, telegram_message_id,
                     sender_name, preview, created_at, acknowledged_at)
                VALUES (?, 'alpha', 'expired', 2, 'Old', 'old', ?, NULL)
                """,
                (str(uuid.uuid4()), now - 24 * 60 * 60 - 1),
            )
            await store._connection().commit()
            self.assertEqual(
                [item.preview for item in await store.list_private_alerts("alpha")],
                ["fresh"],
            )
            self.assertEqual(await store.private_unread_count("alpha"), 1)
            self.assertEqual(await store.purge_expired(now=now), 1)
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


class PrivateAlertWorkerTests(unittest.TestCase):
    @staticmethod
    def worker() -> AccountWorker:
        worker = AccountWorker.__new__(AccountWorker)
        worker.account = SimpleNamespace(id="account-1")
        worker._private_sender_hmac_key = b"server-side-secret"
        worker.store = SimpleNamespace(
            add_private_alert=AsyncMock(return_value=True),
            add=AsyncMock(),
        )
        worker.last_activity = {-1001: 123.0}
        worker.generate = AsyncMock()
        worker.should_reply = AsyncMock()
        return worker

    def test_private_message_records_alert_then_stops_before_group_pipeline(self) -> None:
        async def scenario() -> None:
            worker = self.worker()
            sender_id = 998877
            message = SimpleNamespace(
                id=55,
                photo=None,
                voice=None,
                video_note=None,
                video=None,
                audio=None,
                sticker=None,
                gif=None,
                document=None,
                contact=None,
                poll=None,
                geo=None,
                venue=None,
                download_media=AsyncMock(),
            )
            event = SimpleNamespace(
                is_private=True,
                is_group=False,
                chat_id=sender_id,
                sender_id=sender_id,
                raw_text="hello privately",
                message=message,
                get_sender=AsyncMock(return_value=None),
            )
            await worker.on_message(event)

            expected_fingerprint = hmac.new(
                b"server-side-secret",
                f"account-1:{sender_id}".encode(),
                hashlib.sha256,
            ).hexdigest()
            worker.store.add_private_alert.assert_awaited_once_with(
                "account-1",
                expected_fingerprint,
                55,
                "Telegram 使用者",
                "hello privately",
            )
            self.assertNotEqual(
                expected_fingerprint,
                hashlib.sha256(str(sender_id).encode()).hexdigest(),
            )
            worker.store.add.assert_not_awaited()
            worker.generate.assert_not_awaited()
            worker.should_reply.assert_not_awaited()
            message.download_media.assert_not_awaited()
            self.assertEqual(worker.last_activity, {-1001: 123.0})

        asyncio.run(scenario())

    def test_private_media_uses_fixed_label_and_storage_failure_still_stops(self) -> None:
        async def scenario() -> None:
            worker = self.worker()
            worker.store.add_private_alert.side_effect = RuntimeError("db unavailable")
            message = SimpleNamespace(id=56, photo=object(), download_media=AsyncMock())
            event = SimpleNamespace(
                is_private=True,
                is_group=False,
                chat_id=5,
                sender_id=5,
                raw_text="",
                message=message,
                get_sender=AsyncMock(side_effect=RuntimeError("lookup failed")),
            )
            await worker.on_message(event)
            worker.store.add_private_alert.assert_awaited_once()
            self.assertEqual(
                worker.store.add_private_alert.await_args.args[-1],
                "（圖片）",
            )
            worker.store.add.assert_not_awaited()
            worker.generate.assert_not_awaited()
            worker.should_reply.assert_not_awaited()
            message.download_media.assert_not_awaited()

        asyncio.run(scenario())

    def test_startup_dialog_scan_restores_only_recent_unread_private_alerts(self) -> None:
        async def scenario() -> None:
            worker = self.worker()
            worker.me_id = 999
            worker.store.ttl_seconds = 24 * 60 * 60
            now = datetime.now(timezone.utc)

            def private_dialog(
                sender_id: int,
                message_id: int,
                *,
                unread: int = 1,
                outgoing: bool = False,
                age_hours: int = 0,
                read_inbox_max_id: int = 0,
            ) -> SimpleNamespace:
                return SimpleNamespace(
                    is_user=True,
                    is_group=False,
                    id=sender_id,
                    unread_count=unread,
                    name="Alice",
                    entity=User(id=sender_id, first_name="Alice"),
                    dialog=SimpleNamespace(read_inbox_max_id=read_inbox_max_id),
                    message=SimpleNamespace(
                        id=message_id,
                        out=outgoing,
                        date=now - timedelta(hours=age_hours),
                        raw_text="missed private message",
                    ),
                )

            dialogs = [
                private_dialog(11, 101),
                private_dialog(12, 102, outgoing=True),
                private_dialog(13, 103, age_hours=25),
                private_dialog(14, 104, read_inbox_max_id=104),
                SimpleNamespace(
                    is_user=False,
                    is_group=True,
                    id=-1001,
                    name="Group",
                    entity=SimpleNamespace(title="Group"),
                ),
            ]

            async def iter_dialogs():
                for dialog in dialogs:
                    yield dialog

            worker.client = SimpleNamespace(iter_dialogs=iter_dialogs)
            await worker.refresh_joined_groups()

            worker.store.add_private_alert.assert_awaited_once()
            call = worker.store.add_private_alert.await_args
            self.assertEqual(call.args[0], "account-1")
            self.assertEqual(call.args[2], 101)
            self.assertEqual(call.args[3], "Alice")
            self.assertEqual(call.args[4], "missed private message")
            self.assertAlmostEqual(
                call.kwargs["created_at"],
                int(now.timestamp()),
                delta=1,
            )
            self.assertEqual(
                worker.joined_groups,
                [{"id": -1001, "title": "Group"}],
            )

        asyncio.run(scenario())

    def test_startup_dialog_scan_seeds_group_activity_for_proactive_chat(self) -> None:
        async def scenario() -> None:
            worker = self.worker()
            observed_at = datetime.now(timezone.utc) - timedelta(minutes=12)
            group_dialog = SimpleNamespace(
                is_user=False,
                is_group=True,
                id=-5428680940,
                name="111",
                entity=SimpleNamespace(title="111"),
                message=SimpleNamespace(id=55, date=observed_at),
            )

            async def iter_dialogs():
                yield group_dialog

            worker.client = SimpleNamespace(iter_dialogs=iter_dialogs)
            await worker.refresh_joined_groups()

            self.assertAlmostEqual(
                worker.last_activity[-5428680940],
                observed_at.timestamp(),
                delta=1,
            )
            self.assertEqual(
                worker.joined_groups,
                [{"id": -5428680940, "title": "111"}],
            )

        asyncio.run(scenario())


class PrivateAlertManagerTests(unittest.TestCase):
    def test_manager_redacts_internal_fields_and_updates_status_counts(self) -> None:
        async def scenario(path: str) -> None:
            store = MemoryStore(path, ttl_hours=24)
            await store.open()
            await store.create_account(account_record("alpha", 1001))
            await store.create_account(account_record("beta", 1002))
            await store.add_private_alert(
                "alpha", "internal-fingerprint", 700, "Alice", "hello"
            )
            await store.add_private_alert(
                "beta", "other-fingerprint", 701, "Bob", "other"
            )

            manager = AccountManager.__new__(AccountManager)
            manager.store = store
            manager.workers = {}
            manager.start_errors = {}
            manager.settings = SimpleNamespace(
                max_accounts=20,
                memory_ttl_hours=24,
                media_provider_readiness={},
            )

            result = await manager.private_alerts("alpha")
            self.assertEqual(result["unread_count"], 1)
            self.assertEqual(len(result["alerts"]), 1)
            public = result["alerts"][0]
            self.assertEqual(
                set(public),
                {
                    "alert_id",
                    "sender_name",
                    "preview",
                    "created_at",
                    "acknowledged",
                },
            )
            self.assertNotIn("fingerprint", repr(result))
            self.assertNotIn("700", repr(result))

            acknowledged = await manager.acknowledge_private_alerts(
                "alpha", alert_ids=[str(public["alert_id"])]
            )
            self.assertEqual(acknowledged["acknowledged"], 1)
            self.assertEqual(acknowledged["unread_count"], 0)
            with self.assertRaises(ValueError):
                await manager.acknowledge_private_alerts(
                    "alpha", alert_ids=["not-a-uuid"]
                )

            status = await manager.status()
            self.assertEqual(status["summary"]["private_unread_count"], 1)
            by_id = {item["id"]: item for item in status["accounts"]}
            self.assertEqual(by_id["alpha"]["private_unread_count"], 0)
            self.assertEqual(by_id["beta"]["private_unread_count"], 1)
            await store.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
