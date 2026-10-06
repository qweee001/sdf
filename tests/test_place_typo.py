"""地名形近別字偵測：實測把「中壢」寫成「中坢」。

坢（U+5762）本身是 Big5 收錄的合法繁體字，所以：
- 簡體字檢查（Big5 編碼判定）看不到它
- ③ 的審核維度本來也沒有字形正確性
兩邊都補上了：程式層用「這則對話真的在講的地名」比對形近別字，③ 加「用字」維度兜底。
"""

import asyncio

from test_worker_reply_arbitration import _worker


def test_detects_place_typo_from_production():
    async def main():
        worker = _worker(101)
        # 對方訊息提到中壢 → 回覆把中壢寫成中坢
        hint = worker._place_typo_hint("中坢太遠", expected=("中壢",))
        assert "中坢" in hint and "中壢" in hint and "只差一個字" in hint
        # 寫對了就沒事
        assert worker._place_typo_hint("中壢太遠", expected=("中壢",)) == ""

    asyncio.run(main())


def test_without_expected_names_nothing_is_guessed():
    """沒有情境（不知道這則在講哪個地名）時不要亂猜——否則「太遠」會被當「太平」。"""
    async def main():
        worker = _worker(101)
        assert worker._place_typo_hint("中坢太遠") == ""
        assert worker._place_typo_hint("那你來試試看") == ""

    asyncio.run(main())


def test_expected_names_come_from_message_history_and_persona():
    async def main():
        worker = _worker(101)
        worker.persona = dict(worker.persona, city="台北", district="士林")
        names = worker._expected_place_names("來中壢讓我檢查", "昨天去板橋看電影")
        assert "士林" in names and "台北" in names
        assert "中壢" in names and "板橋" in names

    asyncio.run(main())


def test_common_words_are_not_mistaken_for_place_typos():
    """「平台」不能因為跟「台中」差一個字就被判別字（台中不含獨有字，根本不驗）。"""
    async def main():
        worker = _worker(101)
        expected = ("台中", "台北")
        for text in ("平台客服很可靠", "我在看新聞", "今天天氣很好"):
            assert worker._place_typo_hint(text, expected=expected) == "", text
        # 反過來：含獨有字的地名會驗，而且提示是「確認一下」不是斷言寫錯
        hint = worker._place_typo_hint("新閒很多", expected=("新莊",))
        assert "新莊" in hint and "確認" in hint

    asyncio.run(main())


def test_correct_place_names_and_casual_text_are_not_flagged():
    async def main():
        worker = _worker(101)
        expected = ("中壢", "士林", "板橋", "淡水", "內湖")
        for text in (
            "中壢太遠",
            "不如士林讓我檢查🫦",
            "板橋車站等你",
            "淡水夕陽超好看",
            "明天去內湖找我",
            "先練練舌頭功",
            "那你來試試看",
            "腿軟正好沒力氣跑",
            "我剛下班在啃雞排",
        ):
            assert worker._place_typo_hint(text, expected=expected) == "", text

    asyncio.run(main())


def test_generation_rewrites_reply_with_place_typo():
    """回覆含地名別字 → 帶正確用字重生成一次；重生後正確就放行。"""

    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(101)
        replies = iter(["中坢太遠 不如你來士林", "中壢太遠 不如你來士林"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        event = AsyncMock(
            sender_id=999,
            chat_id=-5565520321,
            id=90,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="來中壢讓我檢查",
            media=None,
        )
        event.sender = None
        text = await worker._generate_reply(event)
        assert text == "中壢太遠 不如你來士林"
        correction = worker._call_ai.await_args.args[1]
        assert "地名別字" in correction
        assert "中壢" in correction

    asyncio.run(main())


def test_generation_drops_reply_when_place_typo_survives_retry():
    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(101)
        worker._call_ai = AsyncMock(return_value="中坢太遠")
        event = AsyncMock(
            sender_id=999,
            chat_id=-5565520321,
            id=91,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="來中壢讓我檢查",
            media=None,
        )
        event.sender = None
        assert await worker._generate_reply(event) == ""
        assert worker._generation_reasons.get(worker._generation_key(event)) == "place_typo"

    asyncio.run(main())


def test_proactive_topic_with_place_typo_is_regenerated():
    """主動發言也要擋：模型第一次寫中坢，第二次寫對才放行。"""

    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(101)
        worker.db.get_group_messages = AsyncMock(
            return_value=[
                {"sender_id": 5, "sender_name": "阿宏", "role": "user",
                 "content": "週末想去中壢逛逛", "timestamp": 1.0}
            ]
        )
        worker.db.get_group_shared_notes = AsyncMock(return_value=[])
        replies = iter(["中坢夜市好逛", "中壢夜市好逛"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        topic = await worker._generate_context_topic(-5565520321)
        assert topic == "中壢夜市好逛"

    asyncio.run(main())
