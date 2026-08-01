from __future__ import annotations

import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.types import MessageEntityMention

from app.content_guard import ContentGuard, SafeReply
from app.worker import AccountWorker


GROUP_ID = -100111
TARGET_ID = 111001
CONTENDER_ID = 111002
HUMAN_ID = 900001


class _TypingAction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Client:
    def action(self, _group_id: int, _kind: str) -> _TypingAction:
        return _TypingAction()


class _SharedClaimStore:
    def __init__(self) -> None:
        self._row_id = 0
        self.claimed_by: str | None = None
        self.claim_calls: list[tuple[str, int, int]] = []

    async def add(self, *_args: object, **_kwargs: object) -> int:
        self._row_id += 1
        return self._row_id

    async def claim_group_reply(
        self,
        account_id: str,
        group_id: int,
        telegram_message_id: int,
    ) -> bool:
        self.claim_calls.append((account_id, group_id, telegram_message_id))
        if self.claimed_by is not None:
            return False
        self.claimed_by = account_id
        return True

    async def recent_group(
        self,
        _account_id: str,
        _group_id: int,
        _limit: int,
        *,
        through_id: int | None = None,
    ) -> list[object]:
        del through_id
        return []


def _worker(
    telegram_id: int,
    *,
    store: object | None = None,
) -> AccountWorker:
    worker = AccountWorker.__new__(AccountWorker)
    worker.account = SimpleNamespace(
        id=f"account-{telegram_id}",
        role_key="male_old_member",
        style="自然",
        task_name="",
        task_info="",
        blocked_terms=(),
        blocked_topics=(),
        reply_on_mention=True,
        reply_on_reply=True,
        group_reply_probability=1.0,
        typing_delay_min_seconds=0,
        typing_delay_max_seconds=0,
    )
    worker.settings = SimpleNamespace(memory_history_limit=20)
    worker.me_id = telegram_id
    worker.me_name = f"帳號 {telegram_id}"
    worker.managed_ids_provider = lambda: frozenset(
        {TARGET_ID, CONTENDER_ID}
    )
    if store is not None:
        worker.store = store
        worker.client = _Client()
        worker.content_guard = ContentGuard()
        worker.group_locks = defaultdict(asyncio.Lock)
        worker.last_activity = {}
        worker.replies_sent = 0
        worker.errors = 0
        worker.blocked_messages = 0
        worker.last_error = ""
        worker.group_allowed = lambda _group_id: True  # type: ignore[method-assign]
        worker._detect_media_intent = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )
        worker.generate = AsyncMock(  # type: ignore[method-assign]
            return_value=SafeReply(
                text="定向回覆",
                policy_digest=worker.content_guard.policy_digest,
            )
        )
    return worker


def _reply_event(worker_target_id: int) -> SimpleNamespace:
    message = SimpleNamespace(
        id=321,
        mentioned=False,
        is_reply=True,
        entities=[],
        get_reply_message=AsyncMock(
            return_value=SimpleNamespace(sender_id=worker_target_id)
        ),
    )
    return SimpleNamespace(
        is_private=False,
        is_group=True,
        chat_id=GROUP_ID,
        raw_text="你剛才說的我懂了",
        sender_id=HUMAN_ID,
        message=message,
        get_sender=AsyncMock(return_value=None),
        reply=AsyncMock(return_value=SimpleNamespace(date=None)),
    )


class DirectedReplyPriorityTests(unittest.TestCase):
    def test_reply_to_managed_account_disables_other_accounts_random_path(
        self,
    ) -> None:
        async def scenario() -> None:
            target = _worker(TARGET_ID)
            contender = _worker(CONTENDER_ID)

            with patch("app.worker.random.random", return_value=0.0):
                self.assertTrue(
                    await target.should_reply(_reply_event(TARGET_ID))
                )
                self.assertFalse(
                    await contender.should_reply(_reply_event(TARGET_ID))
                )

        asyncio.run(scenario())

    def test_mention_entity_disables_non_mentioned_accounts_random_path(
        self,
    ) -> None:
        async def scenario() -> None:
            target = _worker(TARGET_ID)
            contender = _worker(CONTENDER_ID)
            entity = MessageEntityMention(offset=0, length=12)
            target_event = SimpleNamespace(
                message=SimpleNamespace(
                    mentioned=True,
                    is_reply=False,
                    entities=[entity],
                )
            )
            contender_event = SimpleNamespace(
                message=SimpleNamespace(
                    mentioned=False,
                    is_reply=False,
                    entities=[entity],
                )
            )

            with patch("app.worker.random.random", return_value=0.0):
                self.assertTrue(await target.should_reply(target_event))
                self.assertFalse(await contender.should_reply(contender_event))

        asyncio.run(scenario())

    def test_ordinary_message_still_uses_configured_random_probability(
        self,
    ) -> None:
        async def scenario() -> None:
            worker = _worker(CONTENDER_ID)
            event = SimpleNamespace(
                message=SimpleNamespace(
                    mentioned=False,
                    is_reply=False,
                    entities=[],
                )
            )

            with patch("app.worker.random.random", return_value=0.0):
                self.assertTrue(await worker.should_reply(event))

        asyncio.run(scenario())

    def test_random_contender_cannot_claim_before_reply_target(self) -> None:
        async def scenario() -> None:
            store = _SharedClaimStore()
            contender = _worker(CONTENDER_ID, store=store)
            target = _worker(TARGET_ID, store=store)

            # Exercise the dangerous order explicitly: the random contender
            # receives and handles the update before the directed target.
            with patch("app.worker.random.random", return_value=0.0):
                await contender._handle_message(_reply_event(TARGET_ID))
                await target._handle_message(_reply_event(TARGET_ID))

            self.assertEqual(store.claimed_by, target.account.id)
            self.assertEqual(
                store.claim_calls,
                [(target.account.id, GROUP_ID, 321)],
            )
            contender.generate.assert_not_awaited()
            target.generate.assert_awaited_once()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
