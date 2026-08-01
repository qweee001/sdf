from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

from app.content_guard import ContentGuard
from app.worker import AccountWorker


class WorkerReplyCanonicalizationTests(unittest.TestCase):
    @staticmethod
    def make_worker() -> AccountWorker:
        worker = AccountWorker.__new__(AccountWorker)
        worker.account = SimpleNamespace(
            id="canonical-test-account",
            role_key="male_observer",
            style="",
            task_name="",
            task_info="",
            blocked_terms=(),
            blocked_topics=(),
            adult_text_enabled=False,
        )
        worker.content_guard = ContentGuard()
        worker.policy_rejections = 0
        worker.style_rejections = 0
        return worker

    def test_split_laughter_is_canonicalized_before_every_audit(self) -> None:
        async def scenario(split_laughter: str) -> None:
            worker = self.make_worker()
            original_screen = worker.content_guard.screen
            worker.content_guard.screen = Mock(wraps=original_screen)  # type: ignore[method-assign]
            worker._completion = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    f"{split_laughter}，這樣",
                    "換\n個 開頭",
                ]
            )
            worker._output_policy_allows = AsyncMock(  # type: ignore[method-assign]
                return_value=True
            )
            safety_context = json.dumps(
                [
                    {
                        "role": "assistant",
                        "sender": "測試帳號",
                        "content": "哈哈，前一則也是笑聲開頭",
                    }
                ],
                ensure_ascii=False,
            )

            reply = await worker.generate(
                "請自然回覆",
                safety_context=safety_context,
            )

            self.assertEqual(reply.text, "換個開頭")
            self.assertEqual(worker.style_rejections, 1)
            self.assertEqual(
                worker.content_guard.screen.call_args_list,
                [call("哈哈，這樣"), call("換個開頭")],
            )
            worker._output_policy_allows.assert_awaited_once_with(
                "換個開頭",
                adult_text_context=True,
                safety_context=safety_context,
            )

            sender = AsyncMock(return_value=SimpleNamespace())
            sent_text, _sent = await worker._send_verified(reply, sender)
            self.assertEqual(sent_text, reply.text)
            sender.assert_awaited_once_with(
                reply.text,
                parse_mode=None,
                link_preview=False,
            )
            self.assertEqual(
                worker.content_guard.screen.call_args_list[-1],
                call(reply.text),
            )

        for split_laughter in ("哈\n哈", "哈 哈"):
            with self.subTest(split_laughter=repr(split_laughter)):
                asyncio.run(scenario(split_laughter))

    def test_assistant_laughter_survives_eight_newer_user_messages(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker()
            history = [
                SimpleNamespace(
                    role="assistant",
                    sender_name="測試帳號",
                    content="哈哈，之前說過",
                )
            ] + [
                SimpleNamespace(
                    role="user",
                    sender_name=f"群友 {index}",
                    content=f"後續訊息 {index}",
                )
                for index in range(8)
            ]
            safety_context = worker._bounded_policy_context(history)
            entries = json.loads(safety_context)
            self.assertEqual(len(entries), 9)
            self.assertEqual(entries[0]["role"], "assistant")
            self.assertEqual(
                [entry["content"] for entry in entries[-8:]],
                [f"後續訊息 {index}" for index in range(8)],
            )

            worker._completion = AsyncMock(  # type: ignore[method-assign]
                side_effect=["哈 哈，又用了", "這次換個開頭"]
            )
            worker._output_policy_allows = AsyncMock(  # type: ignore[method-assign]
                return_value=True
            )

            reply = await worker.generate(
                "請自然回覆",
                safety_context=safety_context,
            )

            self.assertEqual(reply.text, "這次換個開頭")
            self.assertEqual(worker.style_rejections, 1)
            worker._output_policy_allows.assert_awaited_once_with(
                "這次換個開頭",
                adult_text_context=True,
                safety_context=safety_context,
            )

        asyncio.run(scenario())

    def test_particle_variants_are_one_opening_family_and_retry(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker()
            self.assertEqual(worker._opening_family("喔～上次的開頭"), "discourse_particle")
            self.assertEqual(worker._opening_family("「～噢，前導標點"), "discourse_particle")
            worker._completion = AsyncMock(  # type: ignore[method-assign]
                side_effect=["哦，這次只是換字", "我覺得可以換個角度聊"]
            )
            worker._output_policy_allows = AsyncMock(  # type: ignore[method-assign]
                return_value=True
            )
            safety_context = json.dumps(
                [
                    {
                        "role": "assistant",
                        "sender": "測試帳號",
                        "content": "喔～上一則也是語助詞開頭",
                    }
                ],
                ensure_ascii=False,
            )

            reply = await worker.generate(
                "請自然回覆",
                safety_context=safety_context,
            )

            self.assertEqual(reply.text, "我覺得可以換個角度聊")
            self.assertEqual(worker.style_rejections, 1)
            retry_messages = worker._completion.await_args_list[1].args[0]
            self.assertIn(
                "不要用喔、哦、噢、嗯、欸或唉等語助詞開場",
                retry_messages[-1]["content"],
            )
            worker._output_policy_allows.assert_awaited_once_with(
                reply.text,
                adult_text_context=True,
                safety_context=safety_context,
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
