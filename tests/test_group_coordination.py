from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.account import AccountRecord
from app.content_guard import ContentGuard, SafeReply
from app.memory import MemoryStore
from app.worker import AccountWorker


GROUP_ID = -100111
HUMAN_ID = 900001


def _account(account_id: str, telegram_id: int) -> AccountRecord:
    now = int(time.time())
    return AccountRecord(
        id=account_id,
        label=account_id,
        session_ciphertext="encrypted-session",
        session_fingerprint=f"fingerprint-{account_id}",
        telegram_user_id=telegram_id,
        telegram_name=account_id,
        enabled=True,
        gender="male",
        stage="old_member",
        style="自然",
        task_name="一般群聊互動",
        task_info="",
        ai_base_url="https://openrouter.ai/api/v1",
        ai_model="test-model",
        ai_api_key_ciphertext="",
        group_reply_probability=1.0,
        reply_on_mention=True,
        reply_on_reply=True,
        typing_delay_min_seconds=0,
        typing_delay_max_seconds=0,
        proactive_enabled=True,
        proactive_idle_minutes=1,
        proactive_min_interval_minutes=1,
        proactive_max_interval_minutes=1,
        max_proactive_per_day=12,
        all_groups=True,
        group_ids=frozenset(),
        revision=1,
        created_at=now,
        updated_at=now,
    )


def _proactive_worker(
    account: AccountRecord,
    store: MemoryStore,
    last_activity: float,
) -> AccountWorker:
    worker = AccountWorker.__new__(AccountWorker)
    worker.account = account
    worker.settings = SimpleNamespace(memory_history_limit=20)
    worker.store = store
    worker.content_guard = ContentGuard()
    worker.me_id = account.telegram_user_id
    worker.me_name = account.telegram_name
    worker.client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(date=None))
    )
    worker.group_locks = defaultdict(asyncio.Lock)
    worker.last_activity = {GROUP_ID: last_activity}
    worker.last_proactive = {}
    worker.next_proactive_interval = {}
    worker.proactive_counts = defaultdict(int)
    worker.replies_sent = 0
    worker.errors = 0
    worker.blocked_messages = 0
    worker.last_error = ""
    worker.generate = AsyncMock(  # type: ignore[method-assign]
        return_value=SafeReply(
            text="主動訊息",
            policy_digest=worker.content_guard.policy_digest,
        )
    )
    return worker


class GroupActivityTests(unittest.TestCase):
    def test_nontext_and_managed_messages_only_refresh_activity(self) -> None:
        async def scenario() -> None:
            store = SimpleNamespace(
                record_group_activity=AsyncMock(return_value=True),
                add=AsyncMock(),
            )
            worker = AccountWorker.__new__(AccountWorker)
            worker.account = SimpleNamespace(id="alpha")
            worker.store = store
            worker.last_activity = {}
            worker.group_allowed = lambda _group_id: True  # type: ignore[method-assign]
            worker.managed_ids_provider = lambda: frozenset({222002})

            nontext = SimpleNamespace(
                is_private=False,
                is_group=True,
                chat_id=GROUP_ID,
                raw_text="",
                sender_id=HUMAN_ID,
                message=SimpleNamespace(id=10),
            )
            await worker._handle_message(nontext)
            self.assertIn(GROUP_ID, worker.last_activity)
            store.record_group_activity.assert_awaited_once()
            store.add.assert_not_awaited()

            store.record_group_activity.reset_mock()
            managed = SimpleNamespace(
                is_private=False,
                is_group=True,
                chat_id=GROUP_ID,
                raw_text="受管帳號剛發出的文字",
                sender_id=222002,
                message=SimpleNamespace(id=11),
                get_sender=AsyncMock(),
            )
            await worker._handle_message(managed)
            store.record_group_activity.assert_awaited_once()
            store.add.assert_awaited_once_with(
                "alpha",
                GROUP_ID,
                222002,
                "受管帳號 222002",
                "assistant",
                "受管帳號剛發出的文字",
            )
            managed.get_sender.assert_awaited_once()

        asyncio.run(scenario())


class BasicGroupClaimTests(unittest.TestCase):
    created_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def message(cls, local_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=local_id,
            date=cls.created_at,
            is_reply=False,
            via_bot_id=None,
            grouped_id=None,
        )

    @classmethod
    def key(cls, worker: AccountWorker, local_id: int) -> str:
        result = worker._group_message_claim_key(
            GROUP_ID,
            HUMAN_ID,
            cls.message(local_id),
            "同一段文字",
        )
        assert isinstance(result, str)
        return result

    def test_identical_events_keep_the_same_key_when_handlers_run_in_reverse(
        self,
    ) -> None:
        first_worker = AccountWorker.__new__(AccountWorker)
        second_worker = AccountWorker.__new__(AccountWorker)

        first_event_on_first = self.key(first_worker, 101)
        second_event_on_first = self.key(first_worker, 102)
        second_event_on_second = self.key(second_worker, 902)
        first_event_on_second = self.key(second_worker, 901)

        self.assertEqual(first_event_on_first, first_event_on_second)
        self.assertEqual(second_event_on_first, second_event_on_second)

    def test_missing_an_earlier_identical_event_does_not_change_the_key(self) -> None:
        complete_worker = AccountWorker.__new__(AccountWorker)
        partial_worker = AccountWorker.__new__(AccountWorker)

        self.key(complete_worker, 201)
        target_from_complete_stream = self.key(complete_worker, 202)
        target_from_partial_stream = self.key(partial_worker, 902)

        self.assertEqual(target_from_complete_stream, target_from_partial_stream)

    def test_same_local_message_replay_keeps_the_same_key(self) -> None:
        worker = AccountWorker.__new__(AccountWorker)

        first_delivery = self.key(worker, 301)
        replay = self.key(worker, 301)

        self.assertEqual(first_delivery, replay)

    def test_worker_restart_keeps_the_same_key(self) -> None:
        original_worker = AccountWorker.__new__(AccountWorker)
        restarted_worker = AccountWorker.__new__(AccountWorker)

        self.key(original_worker, 401)
        before_restart = self.key(original_worker, 402)
        after_restart = self.key(restarted_worker, 902)

        self.assertEqual(before_restart, after_restart)

    def test_stable_claim_is_atomic_across_account_connections(self) -> None:
        async def scenario(path: str) -> None:
            first = MemoryStore(path, ttl_hours=24)
            second = MemoryStore(path, ttl_hours=24)
            await first.open()
            await first.create_account(_account("alpha", 101001))
            await first.create_account(_account("beta", 101002))
            await second.open()
            first_worker = AccountWorker.__new__(AccountWorker)
            second_worker = AccountWorker.__new__(AccountWorker)
            first_key = self.key(first_worker, 501)
            second_key = self.key(second_worker, 9501)
            self.assertEqual(first_key, second_key)

            results = await asyncio.gather(
                first.claim_group_reply("alpha", GROUP_ID, first_key),
                second.claim_group_reply("beta", GROUP_ID, second_key),
            )
            self.assertEqual(sum(results), 1)
            await second.close()
            await first.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


class ProactiveCoordinationTests(unittest.TestCase):
    def test_two_workers_share_lease_cooldown_and_persistent_daily_limit(
        self,
    ) -> None:
        async def scenario(path: str) -> None:
            first = MemoryStore(path, ttl_hours=24)
            second = MemoryStore(path, ttl_hours=24)
            await first.open()
            alpha = _account("alpha", 202001)
            beta = _account("beta", 202002)
            await first.create_account(alpha)
            await first.create_account(beta)
            await second.open()
            now = int(time.time())
            old_activity = now - 10 * 60
            await first.record_group_activity(GROUP_ID, now=old_activity)
            alpha_worker = _proactive_worker(alpha, first, old_activity)
            beta_worker = _proactive_worker(beta, second, old_activity)

            sent = await asyncio.gather(
                alpha_worker._try_proactive_message(GROUP_ID, now=now),
                beta_worker._try_proactive_message(GROUP_ID, now=now),
            )
            self.assertEqual(sum(sent), 1)
            self.assertEqual(
                alpha_worker.client.send_message.await_count
                + beta_worker.client.send_message.await_count,
                1,
            )
            winner = alpha if sent[0] else beta
            await second.close()
            await first.close()

            reopened = MemoryStore(path, ttl_hours=24)
            await reopened.open()
            cooldown = await reopened.claim_proactive_lease(
                "beta" if winner.id == "alpha" else "alpha",
                GROUP_ID,
                idle_seconds=0,
                cooldown_seconds=5 * 60,
                daily_limit=12,
                now=now + 60,
            )
            self.assertFalse(cooldown.allowed)
            self.assertEqual(cooldown.reason, "cooldown")
            daily = await reopened.claim_proactive_lease(
                winner.id,
                GROUP_ID,
                idle_seconds=0,
                cooldown_seconds=5 * 60,
                daily_limit=1,
                now=now + 5 * 60 + 1,
            )
            self.assertFalse(daily.allowed)
            self.assertEqual(daily.reason, "daily_limit")
            await reopened.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))

    def test_new_activity_from_another_connection_invalidates_lease(self) -> None:
        async def scenario(path: str) -> None:
            first = MemoryStore(path, ttl_hours=24)
            second = MemoryStore(path, ttl_hours=24)
            await first.open()
            await first.create_account(_account("alpha", 303001))
            await second.open()
            now = int(time.time())
            await first.record_group_activity(GROUP_ID, now=now - 10 * 60)
            lease = await first.claim_proactive_lease(
                "alpha",
                GROUP_ID,
                idle_seconds=60,
                cooldown_seconds=60,
                daily_limit=12,
                now=now,
            )
            self.assertTrue(lease.allowed)

            await second.record_group_activity(GROUP_ID, now=now + 1)
            committed = await first.commit_proactive_lease(
                "alpha",
                GROUP_ID,
                lease.lease_token,
                idle_seconds=60,
                cooldown_seconds=60,
                daily_limit=12,
                now=now + 2,
            )
            self.assertFalse(committed.allowed)
            self.assertIn(committed.reason, {"active", "lease_lost"})
            await second.close()
            await first.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(str(Path(directory) / "memory.db")))


if __name__ == "__main__":
    unittest.main()
