from __future__ import annotations

import asyncio
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.custom.message import Message
from telethon.tl.types import PeerChannel

from app.content_guard import ContentGuard
from app.manager import AccountConflictError, AccountManager
from app.worker import AccountWorker


GROUP_ID = -1001
SECOND_GROUP_ID = -1002


def telegram_message(message_id: int = 777) -> Message:
    return Message(
        id=message_id,
        peer_id=PeerChannel(1001),
        date=datetime.now(timezone.utc),
    )


def make_worker(
    *,
    all_groups: bool = False,
    group_ids: frozenset[int] = frozenset({GROUP_ID}),
    joined_group_ids: tuple[int, ...] = (GROUP_ID,),
    connected: bool = True,
) -> AccountWorker:
    worker = AccountWorker.__new__(AccountWorker)
    worker.account = SimpleNamespace(
        id="account-1",
        all_groups=all_groups,
        group_ids=group_ids,
        role_key="female_old_member",
        blocked_terms=("禁止詞",),
        blocked_topics=(),
    )
    worker.content_guard = ContentGuard(
        worker.account.blocked_terms,
        worker.account.blocked_topics,
    )
    worker.state = "online"
    worker.client = SimpleNamespace(
        is_connected=lambda: connected,
        send_message=AsyncMock(return_value=telegram_message()),
    )
    worker.joined_groups = [
        {"id": group_id, "title": f"Group {group_id}"}
        for group_id in joined_group_ids
    ]
    worker.store = SimpleNamespace(add=AsyncMock())
    worker.me_id = 987654
    worker.me_name = "固定角色帳號"
    worker.group_locks = defaultdict(asyncio.Lock)
    worker.last_activity = {}
    worker.replies_sent = 0
    worker.policy_rejections = 0
    worker.blocked_messages = 0
    return worker


class WorkerManualSendTests(unittest.TestCase):
    def test_requires_a_connected_online_worker(self) -> None:
        async def scenario() -> None:
            disconnected = make_worker(connected=False)
            with self.assertRaisesRegex(RuntimeError, "not connected"):
                await disconnected.manual_send_text(GROUP_ID, "晚安")
            disconnected.client.send_message.assert_not_awaited()

            offline = make_worker()
            offline.state = "stopped"
            with self.assertRaisesRegex(RuntimeError, "not connected"):
                await offline.manual_send_text(GROUP_ID, "晚安")
            offline.client.send_message.assert_not_awaited()

        asyncio.run(scenario())

    def test_group_must_be_enabled_and_joined(self) -> None:
        async def scenario() -> None:
            outside_scope = make_worker(
                group_ids=frozenset({SECOND_GROUP_ID}),
                joined_group_ids=(GROUP_ID, SECOND_GROUP_ID),
            )
            with self.assertRaisesRegex(ValueError, "enabled scope"):
                await outside_scope.manual_send_text(GROUP_ID, "大家晚安")
            outside_scope.client.send_message.assert_not_awaited()

            not_joined = make_worker(
                all_groups=True,
                group_ids=frozenset(),
                joined_group_ids=(SECOND_GROUP_ID,),
            )
            with self.assertRaisesRegex(ValueError, "not joined"):
                await not_joined.manual_send_text(GROUP_ID, "大家晚安")
            not_joined.client.send_message.assert_not_awaited()

        asyncio.run(scenario())

    def test_rejects_invalid_text_before_policy_audit(self) -> None:
        async def scenario() -> None:
            for invalid in (None, 123, True):
                worker = make_worker()
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "string"):
                        await worker.manual_send_text(  # type: ignore[arg-type]
                            GROUP_ID,
                            invalid,
                        )

            for invalid in ("", " \r\n\t"):
                worker = make_worker()
                with self.subTest(invalid=repr(invalid)):
                    with self.assertRaisesRegex(ValueError, "empty"):
                        await worker.manual_send_text(GROUP_ID, invalid)

        asyncio.run(scenario())

    def test_manual_text_is_not_screened_or_role_audited(self) -> None:
        async def scenario() -> None:
            worker = make_worker()
            text = "這裡包含禁止詞，請洽管理員加入"

            result = await worker.manual_send_text(GROUP_ID, text)

            self.assertEqual(result["message_count"], 1)
            worker.client.send_message.assert_awaited_once_with(
                GROUP_ID,
                text,
                parse_mode=None,
                link_preview=False,
            )
            self.assertEqual(worker.policy_rejections, 0)
            self.assertEqual(worker.blocked_messages, 0)

        asyncio.run(scenario())

    def test_success_uses_own_client_group_lock_memory_and_stats(self) -> None:
        async def scenario() -> None:
            worker = make_worker()
            sent = telegram_message(4321)

            async def send_message(
                group_id: int,
                text: str,
                **kwargs: object,
            ) -> Message:
                self.assertTrue(worker.group_locks[GROUP_ID].locked())
                self.assertEqual(group_id, GROUP_ID)
                self.assertEqual(text, "  大家晚安  ")
                self.assertEqual(
                    kwargs,
                    {"parse_mode": None, "link_preview": False},
                )
                return sent

            worker.client.send_message = AsyncMock(side_effect=send_message)

            result = await worker.manual_send_text(
                GROUP_ID,
                "  大家晚安  ",
            )

            self.assertEqual(
                result,
                {
                    "ok": True,
                    "partial": False,
                    "account_id": "account-1",
                    "group_id": GROUP_ID,
                    "message_ids": [4321],
                    "message_count": 1,
                    "sent_utf16_units": len(
                        "  大家晚安  ".encode("utf-16-le")
                    )
                    // 2,
                },
            )
            self.assertNotIn("session", result)
            self.assertNotIn("api_key", result)
            worker.store.add.assert_awaited_once_with(
                "account-1",
                GROUP_ID,
                987654,
                "固定角色帳號",
                "assistant",
                "  大家晚安  ",
                created_at=int(sent.date.timestamp()),
            )
            self.assertEqual(worker.replies_sent, 1)
            self.assertIn(GROUP_ID, worker.last_activity)

        asyncio.run(scenario())

    def test_long_text_is_split_without_changing_its_content(self) -> None:
        async def scenario() -> None:
            worker = make_worker()
            text = ("A" * 3999) + "🙂" + ("B" * 4200)
            messages = [
                telegram_message(100),
                telegram_message(101),
                telegram_message(102),
            ]
            worker.client.send_message.side_effect = messages

            result = await worker.manual_send_text(GROUP_ID, text)

            sent_chunks = [
                call.args[1]
                for call in worker.client.send_message.await_args_list
            ]
            self.assertEqual("".join(sent_chunks), text)
            self.assertTrue(
                all(
                    len(chunk.encode("utf-16-le")) // 2 <= 4096
                    for chunk in sent_chunks
                )
            )
            self.assertEqual(result["message_ids"], [100, 101, 102])
            self.assertEqual(result["message_count"], 3)
            self.assertFalse(result["partial"])
            self.assertEqual(
                result["sent_utf16_units"],
                len(text.encode("utf-16-le")) // 2,
            )
            self.assertEqual(worker.store.add.await_count, 3)
            stored_chunks = [
                call.args[5] for call in worker.store.add.await_args_list
            ]
            self.assertEqual(stored_chunks, sent_chunks)
            for call, message in zip(
                worker.store.add.await_args_list,
                messages,
                strict=True,
            ):
                self.assertEqual(
                    call.kwargs["created_at"],
                    int(message.date.timestamp()),
                )
            self.assertEqual(worker.replies_sent, 3)

        asyncio.run(scenario())

    def test_invalid_telegram_result_is_not_recorded(self) -> None:
        async def scenario() -> None:
            worker = make_worker()
            worker.client.send_message.return_value = SimpleNamespace(
                id=99,
                date=datetime.now(timezone.utc),
            )

            with self.assertRaisesRegex(RuntimeError, "invalid text message"):
                await worker.manual_send_text(GROUP_ID, "大家晚安")

            worker.store.add.assert_not_awaited()
            self.assertEqual(worker.replies_sent, 0)

            missing_date = make_worker()
            missing_date.client.send_message.return_value = Message(
                id=99,
                peer_id=PeerChannel(1001),
                date=None,
            )
            with self.assertRaisesRegex(RuntimeError, "invalid text message"):
                await missing_date.manual_send_text(GROUP_ID, "大家晚安")
            missing_date.store.add.assert_not_awaited()
            self.assertEqual(missing_date.replies_sent, 0)

        asyncio.run(scenario())

    def test_partial_failure_reports_only_sent_prefix_for_safe_retry(self) -> None:
        async def scenario() -> None:
            worker = make_worker()
            text = ("A" * 4000) + ("B" * 200)
            worker.client.send_message.side_effect = [
                telegram_message(100),
                RuntimeError("temporary Telegram failure"),
            ]

            result = await worker.manual_send_text(GROUP_ID, text)

            self.assertFalse(result["ok"])
            self.assertTrue(result["partial"])
            self.assertEqual(result["message_ids"], [100])
            self.assertEqual(result["message_count"], 1)
            self.assertEqual(result["sent_utf16_units"], 4000)
            worker.store.add.assert_awaited_once()
            self.assertEqual(worker.replies_sent, 1)

        asyncio.run(scenario())

    def test_rejects_invalid_unicode_before_sending(self) -> None:
        async def scenario() -> None:
            worker = make_worker()

            with self.assertRaisesRegex(ValueError, "invalid Unicode"):
                await worker.manual_send_text(GROUP_ID, "\ud800")

            worker.client.send_message.assert_not_awaited()
            worker.store.add.assert_not_awaited()

        asyncio.run(scenario())


class ManagerManualSendTests(unittest.TestCase):
    @staticmethod
    def make_manager(
        worker: object | None,
        *,
        enabled: bool = True,
    ) -> AccountManager:
        manager = AccountManager.__new__(AccountManager)
        manager.store = SimpleNamespace(
            get_account=AsyncMock(
                return_value=SimpleNamespace(
                    id="account-1",
                    enabled=enabled,
                    revision=3,
                )
            )
        )
        manager.workers = {} if worker is None else {"account-1": worker}
        manager._account_operation_locks = defaultdict(asyncio.Lock)
        return manager

    def test_routes_only_to_the_selected_connected_account(self) -> None:
        async def scenario() -> None:
            expected = {
                "ok": True,
                "partial": False,
                "account_id": "account-1",
                "group_id": GROUP_ID,
                "message_ids": [77],
                "message_count": 1,
                "sent_utf16_units": 4,
            }
            selected_worker = SimpleNamespace(
                account=SimpleNamespace(revision=3),
                state="online",
                client=SimpleNamespace(is_connected=lambda: True),
                manual_send_text=AsyncMock(return_value=expected),
            )
            other_worker = SimpleNamespace(
                account=SimpleNamespace(revision=3),
                state="online",
                client=SimpleNamespace(is_connected=lambda: True),
                manual_send_text=AsyncMock(),
            )
            manager = self.make_manager(selected_worker)
            manager.workers["account-2"] = other_worker

            result = await manager.manual_send_text(
                "account-1",
                GROUP_ID,
                "大家晚安",
            )

            self.assertEqual(result, expected)
            selected_worker.manual_send_text.assert_awaited_once_with(
                GROUP_ID,
                "大家晚安",
            )
            other_worker.manual_send_text.assert_not_awaited()

        asyncio.run(scenario())

    def test_rejects_missing_disabled_offline_or_disconnected_worker(self) -> None:
        async def scenario() -> None:
            cases = (
                self.make_manager(None),
                self.make_manager(
                    SimpleNamespace(
                        account=SimpleNamespace(revision=3),
                        state="online",
                        client=SimpleNamespace(is_connected=lambda: True),
                        manual_send_text=AsyncMock(),
                    ),
                    enabled=False,
                ),
                self.make_manager(
                    SimpleNamespace(
                        account=SimpleNamespace(revision=3),
                        state="stopped",
                        client=SimpleNamespace(is_connected=lambda: True),
                        manual_send_text=AsyncMock(),
                    )
                ),
                self.make_manager(
                    SimpleNamespace(
                        account=SimpleNamespace(revision=3),
                        state="online",
                        client=SimpleNamespace(is_connected=lambda: False),
                        manual_send_text=AsyncMock(),
                    )
                ),
            )
            for manager in cases:
                with self.subTest(manager=manager):
                    with self.assertRaisesRegex(
                        AccountConflictError,
                        "not connected",
                    ):
                        await manager.manual_send_text(
                            "account-1",
                            GROUP_ID,
                            "大家晚安",
                        )

        asyncio.run(scenario())

    def test_rejects_a_worker_from_an_older_account_revision(self) -> None:
        async def scenario() -> None:
            worker = SimpleNamespace(
                account=SimpleNamespace(revision=2),
                state="online",
                client=SimpleNamespace(is_connected=lambda: True),
                manual_send_text=AsyncMock(),
            )
            manager = self.make_manager(worker)

            with self.assertRaisesRegex(
                AccountConflictError,
                "settings are restarting",
            ):
                await manager.manual_send_text(
                    "account-1",
                    GROUP_ID,
                    "大家晚安",
                )
            worker.manual_send_text.assert_not_awaited()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
