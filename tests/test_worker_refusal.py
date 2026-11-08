"""拒答防線測試：模型的「我不能參與」不該被當成角色台詞發進群裡。

真實失敗模式：拒答句不帶簡體字、不含格式標籤、長度往往也過關、跟近期
文案也不像，既有校驗鏈會全部放行，最後群裡看到的是「抱歉，我無法參與
這類對話」這種當場破戲的台詞。成人人設越露骨，模型拒答機率越高，
所以這條防線是常態路徑，不是邊角案例。
"""

import asyncio
from unittest.mock import AsyncMock

from test_worker_reply_arbitration import MANAGED, _event, _worker  # noqa: E402

REFUSALS = [
    "抱歉，我無法參與這類對話。",
    "作為一個 AI，我沒有辦法回應這個請求。",
    "這樣的要求違反了我的使用準則，請你理解。",
    "我沒有辦法幫你生成這種內容。",
    "不好意思，我不適合進行這樣的討論。",
    "我不能參與這類的討論。",
    "我無法提供你想要的內容。",
    "AI 服务拒绝回答此请求",
    "AI服務拒絕回答此請求",
]

# 台灣口語裡「抱歉」「不能」是日常詞，拿單詞去擋會把真實閒聊一起擋掉；
# 「換個話題」更是角色自己會做的動作，不是拒答。這些必須全部放行。
NOT_REFUSALS = [
    "抱歉啦 剛在忙 現在才回",
    "今天不能去喔 我臨時要加班",
    "好啊 你來台中我帶你吃那間",
    "我等等想被操到腿軟 你來不來",
    "笑死 你這句太扯了吧",
    "明天要早起 今天先睡啦 掰",
    "我癢了 你現在想不想過來",
    "先別理他，我們聊點別的。",
    "我今天不能繼續聊了 要睡了",
    "你不尊重我 那就算了",
]


def test_refusal_phrases_are_detected():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        for text in REFUSALS:
            assert worker._is_refusal(text) is True, text

    asyncio.run(main())


def test_casual_and_explicit_lines_are_not_refusals():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        for text in NOT_REFUSALS:
            assert worker._is_refusal(text) is False, text

    asyncio.run(main())


def test_generation_drops_reply_when_model_refuses_twice():
    """兩次都拒答就不發送，且理由記為 refusal 而不是當成正常回覆送出去。

    現在全拒時會多打一次「極短提示」逃生，所以呼叫次數是三次以上；
    逃生也拒答才丟，行為不變。
    """

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(return_value="抱歉，我無法參與這類對話。")
        event = _event()
        text = await worker._generate_reply(event)
        assert text == ""
        assert worker._call_ai.await_count >= 3
        assert worker._generation_reasons.get(worker._generation_key(event)) == (
            "refusal"
        )

    asyncio.run(main())


def test_refusal_retry_asks_for_role_line_instead_of_apology():
    """重試提示要把它拉回「角色會打什麼字」，而不是再說一次『不符合要求』。

    主模型拒答 → 極短提示逃生也拒答 → 才進到重寫那一次。
    """

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(
            side_effect=[
                "抱歉，我無法參與這類對話。",
                "抱歉，我無法參與這類對話。",
                "好啊 你來台中我帶你吃那間",
            ]
        )
        text = await worker._generate_reply(_event())
        assert text == "好啊 你來台中我帶你吃那間"
        retry_prompt = worker._call_ai.await_args_list[2].args[1]
        assert "角色" in retry_prompt
        assert "不要道歉" in retry_prompt

    asyncio.run(main())


def test_short_prompt_escape_rescues_a_refused_reply():
    """長提示被拒答時，極短角色卡那一次要救回一則可發的台詞。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])

        async def fake_call(system_prompt, user_message, model=None, purpose="text"):
            if "成人約會群組" in system_prompt:  # 極短角色卡
                return "好啊 你來台中我帶你吃那間"
            return "作為一個 AI，我沒有辦法回應這個請求。"

        worker._call_ai = AsyncMock(side_effect=fake_call)
        text = await worker._generate_reply(_event())
        assert text == "好啊 你來台中我帶你吃那間"
        assert worker.stats["refusal_escape"] == 1
        assert worker._call_ai.await_count == 2

    asyncio.run(main())


def test_escape_is_not_spent_when_fallback_model_answers():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._fallback_models = ("backup-a",)

        async def fake_call(system_prompt, user_message, model=None, purpose="text"):
            if "成人約會群組" in system_prompt:
                return "極短提示來的句子"
            if model:
                return "好啊 你來台中我帶你吃那間"
            return "作為一個 AI，我沒有辦法回應這個請求。"

        worker._call_ai = AsyncMock(side_effect=fake_call)
        text = await worker._generate_reply(_event())
        assert text == "好啊 你來台中我帶你吃那間"
        assert "refusal_escape" not in worker.stats

    asyncio.run(main())


def test_escape_rejects_meta_text_and_counts_nothing():
    """逃生那一次若自己也回拒答／英文，不可當成台詞用。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(
            return_value="The prompt could not be submitted."
        )
        assert await worker._refusal_escape(["a：b"]) == ""
        assert "refusal_escape" not in worker.stats
        assert worker.stats["refusal_escape_try"] == 1

    asyncio.run(main())


def test_escape_sweeps_fallback_model_when_primary_still_refuses():
    """拒答多來自 Gemini 系：短提示在原模型若還是拒，就要換備援模型。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._fallback_models = ("backup-a",)
        seen: list[str | None] = []

        async def fake_call(system_prompt, user_message, model=None, purpose="text"):
            seen.append(model)
            if model:
                return "好啊 你來台中我帶你吃那間"
            return "The prompt could not be submitted."

        worker._call_ai = AsyncMock(side_effect=fake_call)
        assert await worker._refusal_escape(["a：b"]) == "好啊 你來台中我帶你吃那間"
        assert seen == [None, "backup-a"]
        assert worker.stats["refusal_escape"] == 1

    asyncio.run(main())


def test_fallback_model_replaces_refused_reply():
    """主模型拒答時直接換備援模型，並在統計裡留下紀錄。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._fallback_models = ("backup-a",)
        seen: list[str | None] = []

        async def fake_call(system_prompt, user_message, model=None, purpose="text"):
            seen.append(model)
            if model is None:
                return "作為一個 AI，我沒有辦法回應這個請求。"
            return "好啊 你來台中我帶你吃那間"

        worker._call_ai = AsyncMock(side_effect=fake_call)
        text = await worker._generate_reply(_event())
        assert text == "好啊 你來台中我帶你吃那間"
        assert seen == [None, "backup-a"]
        assert worker.stats["refusal_fallbacks"] == 1

    asyncio.run(main())


def test_fallback_is_not_used_when_primary_reply_is_usable():
    """正常回覆不該多打一次備援模型，否則每次發言都翻倍計費。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._fallback_models = ("backup-a",)
        worker._call_ai = AsyncMock(return_value="好啊 你來台中我帶你吃那間")
        text = await worker._generate_reply(_event())
        assert text == "好啊 你來台中我帶你吃那間"
        assert worker._call_ai.await_count == 1

    asyncio.run(main())
