from __future__ import annotations

import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.content_guard import ContentGuard, SafeReply
from app.memory import MemoryMessage
from app.worker import AccountWorker


class _TypingAction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Client:
    def action(self, _group_id: int, _kind: str) -> _TypingAction:
        return _TypingAction()


class _HistoryStore:
    def __init__(self, messages: list[MemoryMessage]) -> None:
        self.messages = messages
        self.add = AsyncMock(side_effect=[100, 101])
        self.claim_group_reply = AsyncMock(return_value=True)
        self.recent_calls: list[tuple[str, int, int, int | None]] = []

    async def recent_group(
        self,
        account_id: str,
        group_id: int,
        limit: int,
        *,
        through_id: int | None = None,
    ) -> list[MemoryMessage]:
        self.recent_calls.append((account_id, group_id, limit, through_id))
        return self.messages[-limit:]


class ReplyContextTests(unittest.TestCase):
    def test_group_reply_prompt_contains_only_the_latest_twenty_messages(
        self,
    ) -> None:
        async def scenario() -> None:
            account_id = "context-test-account"
            group_id = -100111
            history = [
                MemoryMessage(
                    account_id=account_id,
                    group_id=group_id,
                    sender_id=1000 + index,
                    sender_name=f"群友 {index:03d}",
                    role="user",
                    content=f"MSG_{index:03d}_UNIQUE",
                    created_at=1_800_000_000 + index,
                )
                for index in range(25)
            ]
            store = _HistoryStore(history)
            worker = AccountWorker.__new__(AccountWorker)
            worker.account = SimpleNamespace(
                id=account_id,
                role_key="male_old_member",
                style="自然",
                task_name="",
                task_info="",
                blocked_terms=(),
                blocked_topics=(),
                typing_delay_min_seconds=0,
                typing_delay_max_seconds=0,
            )
            # Even an older deployment setting above 20 must not expand the
            # automatic-reply context beyond the product limit.
            worker.settings = SimpleNamespace(memory_history_limit=99)
            worker.content_guard = ContentGuard()
            worker.store = store
            worker.client = _Client()
            worker.group_locks = defaultdict(asyncio.Lock)
            worker.last_activity = {}
            worker.managed_ids_provider = lambda: frozenset()
            worker.group_allowed = lambda _group_id: True  # type: ignore[method-assign]
            worker.should_reply = AsyncMock(return_value=True)  # type: ignore[method-assign]
            worker._detect_media_intent = AsyncMock(return_value=None)  # type: ignore[method-assign]
            worker.generate = AsyncMock(  # type: ignore[method-assign]
                return_value=SafeReply(
                    text="收到",
                    policy_digest=worker.content_guard.policy_digest,
                )
            )
            worker.me_id = 999
            worker.me_name = "測試帳號"
            worker.replies_sent = 0
            worker.errors = 0
            worker.blocked_messages = 0
            worker.last_error = ""

            event = SimpleNamespace(
                is_private=False,
                is_group=True,
                chat_id=group_id,
                raw_text="MSG_024_UNIQUE",
                sender_id=1024,
                message=SimpleNamespace(id=321),
                get_sender=AsyncMock(return_value=None),
                reply=AsyncMock(return_value=SimpleNamespace(date=None)),
            )

            await worker._handle_message(event)

            self.assertEqual(
                store.recent_calls,
                [(account_id, group_id, 20, 100)],
            )
            prompt = worker.generate.await_args.args[0]
            self.assertEqual(prompt.count('"speaker":'), 20)
            for excluded in range(5):
                self.assertNotIn(f"MSG_{excluded:03d}_UNIQUE", prompt)
            for included in range(5, 25):
                self.assertIn(f"MSG_{included:03d}_UNIQUE", prompt)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
