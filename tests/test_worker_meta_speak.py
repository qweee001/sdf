"""元話防線：模型在講「自己被交代了什麼」，不是在跟群裡講話。

真實失敗模式：19:43:56 小小送出的
「你的要求是讓我在群聊情境中扮演特定角色」——文法正確、沒有拒答腔、
沒有簡體、長度也過關，既有校驗鏈全部放行，群裡看到的是提示詞在自我介紹。

防線收的是角色扮演才有的句型（要求＋是＋扮演／群組＋扮演／人設是…），
不是單詞黑名單：「我的人設有點騷」「扮演開放一點的女生」是真人會講的，
必須放行。抓到時先重寫一次，重寫還中才丟。
"""

import asyncio
from unittest.mock import AsyncMock

from test_worker_reply_arbitration import MANAGED, _event, _worker  # noqa: E402

META_LINES = [
    "你的要求是讓我在群聊情境中扮演特定角色",
    "在這個群組裡扮演的角色是陪大家聊天",
    "人設是台北上班族",
    "以女朋友的身分回應你",
    "任務就是扮演一個剛下班的女生",
]

NOT_META_LINES = [
    "我的人設有點騷",
    "扮演開放一點的女生也可以啊",
    "剛下班脫高跟鞋，誰敢仔細看嘔吐物，叫外送比較省事啦",
    "試試看就知道我有多濕了😏",
    "你說會喜歡我，硬成這樣不打手槍難道要找我洩火嗎",
    "你的要求很高欸",
    "今天不能去喔 我臨時要加班",
]


def test_meta_speak_phrases_are_detected():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        for text in META_LINES:
            assert worker._is_meta_speak(text) is True, text

    asyncio.run(main())


def test_real_chat_lines_are_not_meta_speak():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        for text in NOT_META_LINES:
            assert worker._is_meta_speak(text) is False, text

    asyncio.run(main())


def test_generation_rewrites_meta_speak_and_keeps_good_retry():
    """元話是可修的：重寫一次拿到正常台詞就發，理由記 meta_speak。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(
            side_effect=[
                "你的要求是讓我在群聊情境中扮演特定角色",
                "冷氣開太強 腳都冰了",
            ]
        )
        text = await worker._generate_reply(_event())
        assert text == "冷氣開太強 腳都冰了"
        retry_prompt = worker._call_ai.await_args_list[1].args[1]
        assert "角色" in retry_prompt
        assert "第一人稱" in retry_prompt

    asyncio.run(main())


def test_generation_drops_reply_when_meta_speak_twice():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(
            return_value="你的要求是讓我在群聊情境中扮演特定角色"
        )
        event = _event()
        text = await worker._generate_reply(event)
        assert text == ""
        assert worker._generation_reasons.get(worker._generation_key(event)) == (
            "meta_speak"
        )

    asyncio.run(main())


def test_gate_rejects_meta_speak_before_spending_a_judge_call():
    """決策層不必花裁判：句型抓到就直接退，重寫要帶上元話的指示。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._review_candidate = AsyncMock(
            return_value={"issue": "none", "sendable": 0.9}
        )
        seen_hints: list[str] = []

        async def fake_generate(_event, *, extra_hint: str = "", **_kw) -> str:
            seen_hints.append(extra_hint)
            return "冷氣開太強 腳都冰了"

        worker._generate_reply = AsyncMock(side_effect=fake_generate)
        text = await worker._gate_reply(
            _event(), "你的要求是讓我在群聊情境中扮演特定角色"
        )
        assert text == "冷氣開太強 腳都冰了"
        # 原句不送裁判（句型已判定），只有重寫後的候選才過裁判。
        assert worker._review_candidate.await_args_list[0].args[1] != (
            "你的要求是讓我在群聊情境中扮演特定角色"
        )
        assert any("工作" in h for h in seen_hints)

    asyncio.run(main())


def test_burst_heat_counts_only_recent_humans():
    """熱度只算近兩分鐘的真人訊息，水軍自己的不算、舊訊息不算。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        now = 1_700_000_000.0
        history = [
            {"role": "assistant", "timestamp": now - 5},
            {"role": "user", "timestamp": now - 10},
            {"role": "user", "timestamp": now - 30},
            {"role": "user", "timestamp": now - 90},
            {"role": "user", "timestamp": now - 200},
        ]
        assert worker._burst_heat(history) == 3

    asyncio.run(main())


def test_hot_group_skips_the_judge():
    """洗屏時不再多花一次裁判，直接放行本機自檢過關的句子。"""

    async def main():
        import time as _t

        worker = _worker(sorted(MANAGED)[0])
        worker._review_candidate = AsyncMock(
            return_value={"issue": "none", "sendable": 0.9}
        )
        now = _t.time()
        rows = [
            {"role": "user", "timestamp": now - i, "sender_name": "群友", "content": f"話{i}"}
            for i in range(14)
        ]

        async def fake_rows(*_a, **_k):
            return rows

        async def fake_own(*_a, **_k):
            return []

        worker.db.get_group_messages = fake_rows
        worker.db.recent_replies_by_name = fake_own
        text = await worker._gate_reply(_event(), "冷氣開太強 腳都冰了")
        assert text == "冷氣開太強 腳都冰了"
        assert worker._review_candidate.await_count == 0
        assert worker.stats["gate_hot_skip"] == 1

    asyncio.run(main())
