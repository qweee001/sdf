from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.content_guard import BlockedReplyError, ContentGuard, SafeReply
from app.worker import AccountWorker


class WorkerGuardTests(unittest.TestCase):
    @staticmethod
    def make_worker(
        *,
        blocked_terms: tuple[str, ...] = ("秘密計畫",),
        blocked_topics: tuple[str, ...] = ("限制主題",),
    ) -> AccountWorker:
        worker = AccountWorker.__new__(AccountWorker)
        worker.account = SimpleNamespace(
            id="guard-test-account",
            ai_model="test-model",
            role_key="male_observer",
            style="",
            task_name="",
            task_info="",
            blocked_terms=blocked_terms,
            blocked_topics=blocked_topics,
        )
        worker.content_guard = ContentGuard(blocked_terms, blocked_topics)
        worker.policy_rejections = 0
        return worker

    def test_generate_never_returns_an_exact_blocked_term(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker()
            worker._completion = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    "這裡提到秘密計畫",
                    "再次提到秘密計畫",
                ]
            )

            with self.assertRaises(BlockedReplyError):
                await worker.generate("請自然回覆")

            self.assertEqual(worker.policy_rejections, 2)
            self.assertEqual(worker._completion.await_count, 2)

        asyncio.run(scenario())

    def test_semantic_block_verdict_is_fail_closed(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker()
            worker._completion = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    "第一段表面不含字詞的相關說明",
                    "BLOCK",
                    "第二段仍是相關說明",
                    "BLOCK",
                ]
            )

            with self.assertRaises(BlockedReplyError):
                await worker.generate("請自然回覆")

            self.assertEqual(worker.policy_rejections, 2)
            self.assertEqual(worker._completion.await_count, 4)

        asyncio.run(scenario())

    def test_semantic_classifier_errors_are_fail_closed(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker()
            worker._completion = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    "第一段待分類文字",
                    RuntimeError("classifier unavailable"),
                    "第二段待分類文字",
                    RuntimeError("malformed classifier response"),
                ]
            )

            with self.assertRaises(BlockedReplyError):
                await worker.generate("請自然回覆")

            self.assertEqual(worker.policy_rejections, 2)
            self.assertEqual(worker._completion.await_count, 4)

        asyncio.run(scenario())

    def test_malformed_semantic_verdict_is_fail_closed(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker()
            worker._completion = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    "第一段待分類文字",
                    "ALLOW because it looks fine",
                    "第二段待分類文字",
                    '{"verdict":"ALLOW"}',
                ]
            )

            with self.assertRaises(BlockedReplyError):
                await worker.generate("請自然回覆")

            self.assertEqual(worker.policy_rejections, 2)
            self.assertEqual(worker._completion.await_count, 4)

        asyncio.run(scenario())

    def test_generate_retries_then_returns_only_the_compliant_reply(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker()
            worker._completion = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    "秘密計畫",
                    "今天天氣不錯，晚點想出去走走。",
                    "ALLOW",
                ]
            )

            reply = await worker.generate("請自然回覆")

            self.assertEqual(reply.text, "今天天氣不錯，晚點想出去走走。")
            self.assertEqual(
                reply.policy_digest,
                worker.content_guard.policy_digest,
            )
            self.assertEqual(worker.policy_rejections, 1)
            self.assertEqual(worker._completion.await_count, 3)
            retry_messages = worker._completion.await_args_list[1].args[0]
            self.assertIn("上一個草稿未通過", retry_messages[-1]["content"])

        asyncio.run(scenario())

    def test_send_boundary_rejects_a_stale_policy_digest(self) -> None:
        worker = self.make_worker()
        approved_under_old_policy = SafeReply(
            text="看起來很普通的文字",
            policy_digest=worker.content_guard.policy_digest,
        )
        worker.content_guard = ContentGuard(
            ("秘密計畫", "新增禁詞"),
            ("限制主題",),
        )

        with self.assertRaisesRegex(BlockedReplyError, "policy changed"):
            worker._verified_text(approved_under_old_policy)

    def test_send_boundary_rechecks_text_even_with_current_digest(self) -> None:
        worker = self.make_worker()
        forged = SafeReply(
            text="秘密計畫",
            policy_digest=worker.content_guard.policy_digest,
        )

        with self.assertRaisesRegex(BlockedReplyError, "final content policy"):
            worker._verified_text(forged)

    def test_all_sends_use_plain_text_and_the_final_guard(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker()
            sent_message = SimpleNamespace()
            sender = AsyncMock(return_value=sent_message)
            safe = SafeReply(
                text="今天聊點輕鬆的",
                policy_digest=worker.content_guard.policy_digest,
            )

            text, result = await worker._send_verified(safe, sender)

            self.assertEqual(text, safe.text)
            self.assertIs(result, sent_message)
            sender.assert_awaited_once_with(
                safe.text,
                parse_mode=None,
                link_preview=False,
            )

            sender.reset_mock()
            blocked = SafeReply(
                text="秘密計畫",
                policy_digest=worker.content_guard.policy_digest,
            )
            with self.assertRaises(BlockedReplyError):
                await worker._send_verified(blocked, sender)
            sender.assert_not_awaited()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
