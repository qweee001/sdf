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


def test_opencc_char_level_detection():
    """P1-1：OpenCC s2t 字級判定，抓 Big5 逐字檢查漏掉的簡體字。

    「体」(U+4F53) 在 Big5 是合法字（古/罕用），舊版逐字檢查全放行；
    OpenCC s2t 會把「体」轉成「體」，字級比對攔下。同樣「天→天、气→氣」
    這類 Big5 編得出的簡體字也該抓。
    """

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        # Big5 合法但實為簡體：舊版漏、新版攔
        assert worker._has_simplified_chars("我嘴巴說不騷身体很誠實") is True
        assert worker._has_simplified_chars("今天天气真好") is True
        assert worker._has_simplified_chars("下次有机会我親自下廚") is True
        # 純繁體（含 s2t 誤殺白名單 吃/台/里/么/游）不誤殺
        assert worker._has_simplified_chars("我們一起去游泳吧") is False
        assert worker._has_simplified_chars("我住台中") is False
        assert worker._has_simplified_chars("好吃") is False
        assert worker._has_simplified_chars("去旅遊") is False
        assert worker._has_simplified_chars("我吃飯了") is False
        assert worker._has_simplified_chars("他是我們里長") is False
        assert worker._has_simplified_chars("我是家裡的么女") is False
        # 人名罕用字（Big5 缺收例外）不誤殺
        assert worker._has_simplified_chars("喆堃") is False

    asyncio.run(main())


def test_gender_term_exclusion():
    """性別詞彙排他（實測 10-14 小小「我前女友」→ 女號講前女友＝性別倒錯）。

    女號禁：前女友/女朋友/我老婆/我太太/老子；男號禁：前男友/我老公。
    第三方敘述（「她前女友」是講別人）不算穿幫。
    """

    async def main():
        # 預設 persona 是女號
        worker = _worker(sorted(MANAGED)[0])
        assert worker.persona["gender"] == "女"
        assert worker._gender_term_violation("我前女友就愛吃甜的") == "前女友"
        assert worker._gender_term_violation("我女朋友超愛吃甜的") == "女朋友"
        assert worker._gender_term_violation("我老婆說要回家") == "我老婆"
        assert worker._gender_term_violation("我太太說要回家") == "我太太"
        assert worker._gender_term_violation("老子不去了") == "老子"
        # 女號講「前男友/前任」正確，不攔
        assert worker._gender_term_violation("我前男友超爛") == ""
        assert worker._gender_term_violation("我跟前任沒了") == ""
        # 第三方敘述不攔（講別人不是自認性別）
        assert worker._gender_term_violation("她前女友又來找她了") == ""

        # 男號
        male_worker = _worker(sorted(MANAGED)[1])
        male_worker.persona = dict(male_worker.persona, gender="男")
        assert male_worker._gender_term_violation("我前男友超愛吃甜的") == "前男友"
        assert male_worker._gender_term_violation("我老公說要回家") == "我老公"
        # 男號講「前女友」正確
        assert male_worker._gender_term_violation("我前女友超愛吃甜的") == ""

    asyncio.run(main())


def test_generation_drops_gender_term_reply():
    """女號回覆含「前女友」→ 重生一次仍違規 → 拒發，drop 原因 gender_term。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(
            side_effect=lambda *_a, **_k: "我前女友就愛吃甜的"
        )
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=92,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="你以前談過幾個",
            media=None,
        )
        text = await worker._generate_reply(event)
        assert text == ""
        reason = worker._generation_reasons.get(worker._generation_key(event))
        assert reason == "gender_term"

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
        # 炮友 不擋：學習資料 v1.0 把「炮友」列為台灣用法，硬擋成「砲友」會露怯
        assert worker._mainland_term_hint("想找炮友") == ""
        # 美女 不擋：實測群裡真人講「美女」3 次、「正妹」0 次，硬擋會露怯
        # （改成 persona 引導兩者皆可，見 persona 語言硬規則）
        assert worker._mainland_term_hint("你是大美女") == ""
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

        # 新增高危大陸詞攔截驗證
        assert worker._mainland_term_hint("小姐姐你好") == "「小姐姐」應講「妹子」"
        assert worker._mainland_term_hint("別口嗨了") == "「口嗨」應講「嘴砲」"
        assert worker._mainland_term_hint("這部影片太牛逼了") == "「牛逼」應講「超強」"
        assert worker._mainland_term_hint("叫外賣吃") == "「外賣」應講「外送」"
        assert worker._mainland_term_hint("搭地鐵去") == "「地鐵」應講「捷運」"
        assert worker._mainland_term_hint("我剛吃完晚飯") == "「晚飯」應講「晚餐」"
        assert worker._mainland_term_hint("吃午飯了嗎") == "「午飯」應講「午餐」"

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


def test_starts_with_gan_blocks_opener_allows_compounds_and_mid_sentence():
    """句首「幹／干」硬擋；幹嘛／幹什麼放行；句中幹不攔。

    實測小小「幹，光講舌頭就讓我濕了」：persona 寫了「不要用幹當開頭」，
    模型當建議無視。
    """

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        assert worker._starts_with_gan("幹，光講舌頭就讓我濕了") is True
        assert worker._starts_with_gan("幹光講就濕了") is True
        assert worker._starts_with_gan("  幹這也太誇張") is True
        assert worker._starts_with_gan("干，这也太夸张") is True
        assert worker._starts_with_gan("幹嘛還不去睡") is False
        assert worker._starts_with_gan("幹什麼啦") is False
        assert worker._starts_with_gan("感覺不如抱起來幹") is False
        assert worker._starts_with_gan("那你是被幹的少") is False
        assert worker._starts_with_gan("") is False

    asyncio.run(main())


def test_generation_rewrites_gan_opener():
    """句首幹 → 帶 correction 重生一次，重生後正常放行。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        replies = iter(["幹，光講舌頭就讓我濕了", "光講就讓我濕了"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=93,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="舌頭那個",
            media=None,
        )
        text = await worker._generate_reply(event)
        assert text == "光講就讓我濕了"
        assert worker._call_ai.await_count == 2
        retry_prompt = worker._call_ai.await_args_list[1].args[1]
        assert "幹" in retry_prompt and "開頭" in retry_prompt

    asyncio.run(main())


def test_generation_drops_reply_when_gan_opener_survives_retry():
    """兩次都以幹開頭 → 不發送，drop 原因 gan_opener。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker._call_ai = AsyncMock(
            side_effect=lambda *_a, **_k: "幹，光講舌頭就讓我濕了"
        )
        event = AsyncMock(
            sender_id=999,
            chat_id=-5428680940,
            id=94,
            mentioned=False,
            is_reply=False,
            reply_to=None,
            raw_text="舌頭那個",
            media=None,
        )
        text = await worker._generate_reply(event)
        assert text == ""
        reason = worker._generation_reasons.get(worker._generation_key(event))
        assert reason == "gan_opener"

    asyncio.run(main())


def test_proactive_drops_gan_opener():
    """主動發言句首幹 → drop 後換一句。"""

    async def main():
        import time

        worker = _worker(sorted(MANAGED)[0])

        class _TopicDB:
            async def get_group_messages(self, group_id, limit=100):
                return [
                    {
                        "sender_name": "風口浪尖",
                        "sender_id": 5,
                        "role": "user",
                        "content": "今晚有人嗎",
                        "timestamp": time.time(),
                    }
                ]

            async def get_group_shared_notes(self, group_id, account_id, limit=10):
                return []

            async def recent_bot_texts_by_group(self, group_id):
                return []

        worker.db = _TopicDB()
        replies = iter(["幹，今晚好想被幹", "今晚想出門晃晃"])
        worker._call_ai = AsyncMock(side_effect=lambda *_a, **_k: next(replies))
        topic = await worker._generate_context_topic(-5428680940)
        assert topic == "今晚想出門晃晃"
        assert worker._call_ai.await_count == 2

    asyncio.run(main())
