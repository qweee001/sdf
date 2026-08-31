"""內容硬規則測試：<answer> 格式洩漏與簡體字必須被擋下（觀察到真實失敗模式）。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from test_worker_reply_arbitration import MANAGED, _worker  # noqa: E402


def test_reply_with_answer_tag_is_blocked():
    """模型輸出 <answer>…</answer> 標記必須被檢測為格式洩漏。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        assert worker._has_format_leak("好的 <answer> 是啊 </answer>") is True
        assert worker._has_format_leak("好的，累了就早點休息啊") is False

    asyncio.run(main())


def test_reply_with_simplified_chars_is_blocked():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        assert (
            worker._has_simplified_chars(
                "哥哥们這寵愛，我直接覺得自己是被捧在手心的人了。"
            )
            is True
        )
        assert (
            worker._has_simplified_chars(
                "哥哥們這寵愛，我直接覺得自己是被捧在手心的人了。"
            )
            is False
        )
        assert worker._has_simplified_chars("下次有机会我親自下廚") is True

    asyncio.run(main())


def test_generation_rejects_answer_tag_and_simplified():
    """_generate_reply 校驗鏈必須包含格式洩漏與簡體檢查，違規時重生一次仍違規則拒發。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        replies = iter(["<answer> 累了就早點休息啊 </answer>", "哥哥们寵愛你啦"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=77,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="今天好累喔",
            media=None,
        )
        text = await worker._generate_reply(event)
        assert text == "", "兩次違規都應拒發"
        reason = worker._generation_reasons.get(worker._generation_key(event))
        assert reason in {"format_leak", "simplified_chars", "policy"}

    asyncio.run(main())
