"""emoji 疲勞偵測：同一個 emoji 連發是明顯的機器節奏。

生產實測（群 111）：三號連續五句都以 🤭 收尾。
"""

import asyncio

from test_worker_reply_arbitration import _worker


def test_emoji_fatigue_hint_after_repeats():
    async def main():
        worker = _worker(101)
        group = -5565520321
        assert worker._emoji_fatigue_hint(group) == ""
        for _ in range(3):
            worker._record_sent_emojis(group, "誰帶你？帶去我家阿🤭")
        hint = worker._emoji_fatigue_hint(group)
        assert "🤭" in hint
        assert "3 次" in hint

    asyncio.run(main())


def test_emoji_fatigue_hint_absent_when_emojis_vary():
    async def main():
        worker = _worker(101)
        group = -5565520321
        for text in ("先去吃飯🍜", "你今天很閒喔😏", "晚點聊🫦", "被發現了🥺"):
            worker._record_sent_emojis(group, text)
        assert worker._emoji_fatigue_hint(group) == ""

    asyncio.run(main())


def test_emoji_fatigue_hint_isolated_per_group():
    async def main():
        worker = _worker(101)
        for _ in range(3):
            worker._record_sent_emojis(-1001, "哈哈🤭")
        assert "🤭" in worker._emoji_fatigue_hint(-1001)
        assert worker._emoji_fatigue_hint(-2002) == ""

    asyncio.run(main())


def test_emoji_history_is_capped():
    async def main():
        worker = _worker(101)
        group = -1001
        for i in range(20):
            worker._record_sent_emojis(group, f"訊息{i}😏")
        # 只留最近 6 個，且都是同一個 emoji → 仍會給提示（累積在同一個值上）
        assert len(worker._recent_emojis_by_group[group]) <= 6
        assert "😏" in worker._emoji_fatigue_hint(group)

    asyncio.run(main())


def test_generation_prompt_carries_emoji_hint():
    """生成回覆時要把 emoji 疲勞提示帶進 prompt。"""

    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(101)
        group = -5565520321
        for _ in range(3):
            worker._record_sent_emojis(group, "陪誰🤭")
        worker._call_ai = AsyncMock(return_value="那你想找誰陪😏")
        event = AsyncMock(
            sender_id=999,
            chat_id=group,
            id=88,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="想找個人陪我",
            media=None,
        )
        event.sender = None
        assert await worker._generate_reply(event) == "那你想找誰陪😏"
        prompt = worker._call_ai.await_args.args[1]
        assert "🤭" in prompt

    asyncio.run(main())
