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
        # 實測 10-09 小小「餓到想乾飯」：「幹飯」是大陸口語（吃飯的意思），
        # 單字「乾」在 Big5 是合法繁體，逐字檢查全放行 → 走片語黑名單攔
        assert worker._has_simplified_chars("餓到想乾飯") is True
        assert worker._has_simplified_chars("餓到想吃飯") is False

    asyncio.run(main())


def test_big5_shared_char_phrases_are_blocked():
    """「么」在 Big5 有收錄，但「什么／怎么」是大陸寫法（實測生成過「想吃什么我陪你」）。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        assert worker._has_simplified_chars("想吃什么我陪你🤤") is True
        assert worker._has_simplified_chars("我在家里等你") is True
        assert worker._has_simplified_chars("你怎麼這麼晚") is False
        assert worker._has_simplified_chars("這麼晚了還不睡") is False
        # 台灣也在用的字不誤殺：里（里長）、么（么女）
        assert worker._has_simplified_chars("我是家裡的么女") is False
        assert worker._has_simplified_chars("他是我們里長") is False

    asyncio.run(main())


def test_taipei_hour_tracks_wall_clock():
    """內部時鐘必須跟著真實台北時間：曾經偏移 0~24 小時，導致 21:48 說出「早安」。"""

    async def main():
        import time as _time

        worker = _worker(sorted(MANAGED)[0])
        real = (_time.time() / 3600 + 8) % 24
        perceived = worker._taipei_hour()
        # 只允許 ±45 分鐘的作息錯峰
        delta = min((perceived - real) % 24, (real - perceived) % 24)
        assert delta <= 0.76, f"內部時鐘偏了 {delta:.2f} 小時"

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


def test_generation_rewrites_time_mismatched_reply():
    """回覆路徑也要擋時段穿幫：實測抓到 20:04 發出「早安～」。

    夜裡生成早安 → 帶問題重生一次 → 重生後正常就放行。
    """

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        replies = iter(["早安～大腸麵線超推🍜", "大腸麵線超推，你帶我去吃呀"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=78,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="好想去吃那家大腸麵線",
            media=None,
        )
        # 固定「台北時間 20 點」：早安不在允許時段（5-11）
        worker._taipei_hour = lambda: 20.0
        text = await worker._generate_reply(event)
        assert text == "大腸麵線超推，你帶我去吃呀"

    asyncio.run(main())


def test_generation_drops_reply_when_time_mismatch_survives_retry():
    """兩次都講早安 → 不發送，drop 原因記為 time_mismatch。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(
            side_effect=lambda *_a, **_k: "早安～今天天氣好好"
        )
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=79,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="好想吃早餐",
            media=None,
        )
        worker._taipei_hour = lambda: 20.0
        text = await worker._generate_reply(event)
        assert text == ""
        reason = worker._generation_reasons.get(worker._generation_key(event))
        assert reason == "time_mismatch"

    asyncio.run(main())


def test_reply_with_meal_word_is_not_treated_as_time_mismatch():
    """回覆裡接對方話題的「午餐／晚餐」不算穿幫，只有問候語才擋。

    （實際放行還得過語意政策分類器，這裡只鎖定時段判定本身的分野。）
    """

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._taipei_hour = lambda: 20.0
        assert worker._has_time_mismatch("付兩百買午餐") is True
        assert worker._has_time_mismatch("付兩百買午餐", greetings_only=True) is False
        # 問候語兩條路都要擋
        assert worker._has_time_mismatch("早安～今天天氣好好", greetings_only=True) is True
        # 白天講早安則放行
        worker._taipei_hour = lambda: 8.0
        assert worker._has_time_mismatch("早安～今天天氣好好", greetings_only=True) is False

    asyncio.run(main())


def test_reply_generation_allows_time_appropriate_greeting():
    """時段相符的問候不能被誤擋（20 點說晚安可以）。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: "晚安，早點睡囉")
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=80,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="先睡囉",
            media=None,
        )
        worker._taipei_hour = lambda: 20.0
        text = await worker._generate_reply(event)
        assert text == "晚安，早點睡囉"

    asyncio.run(main())


def test_mainland_term_hint_names_the_replacement():
    """大陸講法偵測＋指名替代：「方便面」應講「泡麵」。

    這些詞不是簡體字（Big5 逐字檢查過關），而是大陸講法，所以獨立一層。
    群組語境兩邊都用的詞（超市／信息／群主）不擋，避免誤殺。
    """

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        assert (
            worker._mainland_term_hint("方便面很好吃")
            == "「方便面」應講「泡麵」"
        )
        assert worker._mainland_term_hint("我住酒店") == "「酒店」應講「飯店」"
        assert worker._mainland_term_hint("想找炮友") == "「炮友」應講「砲友」"
        # 美女→正妹（實測研究：台灣人講正妹，美女是大陸講法）
        assert worker._mainland_term_hint("你是大美女") == "「美女」應講「正妹」"
        # 多個命中要並列（correction 才能一次改完）
        assert (
            worker._mainland_term_hint("方便面酸奶一起買")
            == "「方便面」應講「泡麵」；「酸奶」應講「優酪乳」"
        )
        # 群組語境兩邊都用的不誤殺
        assert worker._mainland_term_hint("超市買東西") == ""
        assert worker._mainland_term_hint("信息很正確") == ""
        assert worker._mainland_term_hint("哥哥們好") == ""
        assert worker._mainland_term_hint("") == ""

    asyncio.run(main())


def test_generation_rewrites_mainland_term():
    """_generate_reply 校驗鏈要含大陸講法檢查：違規重生一次，重生後正常放行。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        replies = iter(["明天一起吃方便面吧", "明天一起吃泡麵吧"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=90,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="晚上餓了想吃東西",
            media=None,
        )
        text = await worker._generate_reply(event)
        assert text == "明天一起吃泡麵吧"

    asyncio.run(main())


def test_generation_drops_reply_when_mainland_term_survives_retry():
    """兩次都用大陸講法 → 不發送，drop 原因記為 mainland_term。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(
            side_effect=lambda *_a, **_k: "明天一起吃方便面吧"
        )
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=91,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="晚上餓了想吃東西",
            media=None,
        )
        text = await worker._generate_reply(event)
        assert text == ""
        reason = worker._generation_reasons.get(worker._generation_key(event))
        assert reason == "mainland_term"

    asyncio.run(main())
