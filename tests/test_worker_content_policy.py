import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from app.worker import AccountWorker


class _FakeDB:
    def __init__(self):
        self.messages = []
        self.activities = []
        self.recent_group_replies = []

    async def get_recent_messages(self, *_args):
        return []

    async def get_recent_group_replies(self, *_args, **_kwargs):
        return list(self.recent_group_replies)

    async def add_message(self, *args):
        self.messages.append(args)

    async def touch_activity(self, *args):
        self.activities.append(args)


class _FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeEvent:
    chat_id = -1001
    sender_id = 123
    raw_text = "今天要聊什麼"
    sender = SimpleNamespace(first_name="小王", last_name=None)


def _worker():
    config = SimpleNamespace(
        ai_model="test-model",
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
    )
    return AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=_FakeDB(),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_args, **_kwargs: None,
    )


def test_video_topic_reply_is_regenerated_instead_of_rewritten():
    async def main():
        worker = _worker()
        worker._call_ai = AsyncMock(side_effect=[
            "要不要打視訊聊聊",
            "先換 LINE 啊，之後有空約出來見面",
        ])

        reply = await worker._generate_reply(_FakeEvent())

        assert reply == "先換 LINE 啊，之後有空約出來見面"
        assert worker._call_ai.await_count == 2

    asyncio.run(main())


def test_reply_at_exactly_sixty_characters_is_accepted_without_retry():
    async def main():
        worker = _worker()
        text = "字" * 60
        worker._call_ai = AsyncMock(return_value=text)

        reply = await worker._generate_reply(_FakeEvent())

        assert reply == text
        assert len(reply) == 60
        assert worker._call_ai.await_count == 1

    asyncio.run(main())


def test_reply_over_sixty_characters_is_regenerated_once():
    async def main():
        worker = _worker()
        valid_retry = "這次控制在六十個字元內"
        worker._call_ai = AsyncMock(side_effect=["字" * 61, valid_retry])

        reply = await worker._generate_reply(_FakeEvent())

        assert reply == valid_retry
        assert worker._call_ai.await_count == 2
        retry_prompt = worker._call_ai.await_args_list[1].args[1]
        assert "最多 60 個字元" in retry_prompt

    asyncio.run(main())


def test_reply_is_dropped_when_retry_is_still_over_sixty_characters():
    async def main():
        worker = _worker()
        worker._call_ai = AsyncMock(side_effect=["甲" * 61, "乙" * 61])

        reply = await worker._generate_reply(_FakeEvent())

        assert reply == ""
        assert worker._call_ai.await_count == 2

    asyncio.run(main())


def test_length_and_video_policies_share_one_retry():
    async def main():
        worker = _worker()
        worker._call_ai = AsyncMock(side_effect=[
            "要不要開鏡頭聊聊" + "字" * 61,
            "丙" * 61,
        ])

        reply = await worker._generate_reply(_FakeEvent())

        assert reply == ""
        assert worker._call_ai.await_count == 2

    asyncio.run(main())


def test_generation_prompt_requires_at_most_sixty_characters():
    worker = _worker()

    prompt = worker._build_user_message(_FakeEvent(), [])

    assert "最多 60 個字元" in prompt
    assert "標點、空格也算" in prompt


def test_video_topic_variants_are_detected():
    variants = [
        "要不要打視訊聊聊",
        "不然開鏡頭看一下",
        "把鏡頭打開讓我看看你",
        "要不要開 camera 聊？",
        "要不要打開相機聊聊",
        "把攝像頭打開讓我看看你",
        "開個 cam 聊一下",
        "camera on, I want to see you",
        "用视频通话吧",
        "Let's do a video call",
        "Lets have a video-call",
        "要不要用 Google Meet？",
        "Lets use GoogleMeet",
        "我們用 Microsoft Teams 聊",
        "不然開直播給你看",
        "Let's start a live stream",
        "開視像聊一下",
        "我今天用 Zoom 上課",
        "螢幕上看你比較有感覺",
    ]
    assert all(AccountWorker._mentions_video_topic(text) for text in variants)


def test_non_video_screen_and_camera_context_is_not_blocked():
    normal_texts = [
        "我在螢幕上看文字",
        "我在螢幕上看到你的訊息了",
        "螢幕上看你的文字比較清楚",
        "這支相機的鏡頭很貴",
        "我平常都用這顆鏡頭拍照",
        "打開鏡頭蓋就能拍了",
        "這顆鏡頭看起來很貴",
        "我使用 camera 拍照",
        "打開相機設定調整光圈",
        "camera on this phone is broken and needs repair",
        "把畫面放大 zoom in 看細節",
    ]
    assert all(not AccountWorker._mentions_video_topic(text) for text in normal_texts)


def test_screen_person_intent_is_blocked_even_with_text_or_reversed_word_order():
    variants = [
        "不想只看文字，我想在螢幕上看到你本人",
        "我看著螢幕裡的你",
    ]
    assert all(AccountWorker._mentions_video_topic(text) for text in variants)


def test_ascii_video_terms_are_detected_when_adjacent_to_chinese_text():
    variants = [
        "開cam聊一下",
        "我們用Zoom聊吧",
        "要用GoogleMeet嗎",
        "我們用Microsoft Teams聊吧",
    ]
    assert all(AccountWorker._mentions_video_topic(text) for text in variants)


def test_equipment_discussion_and_zoom_optics_are_not_blocked():
    normal_texts = [
        "我們來聊相機設定和攝影",
        "I like to chat about camera settings",
        "zoom in看細節",
        "這支 zoom lens 的焦距很實用",
        "我們聊聊該買哪台相機",
        "我想看一下這台相機的規格",
        "這家新開的相機店不錯",
        "the camera on this phone is excellent",
        "最近在玩 video game",
        "電腦的 video card 該升級了",
        "我把網頁 zoom 到 150%",
        "zoom 放大一點看細節",
        "我在螢幕上看你的照片，你的臉很可愛",
        "先開相機權限掃 QR code",
        "用 camera 掃一下條碼",
        "這支相機開不了，我拿去修",
        "我要關相機電源換電池",
        "螢幕裡有你的名字",
        "我看你對相機很有研究",
        "這台相機看起來很適合你",
        "這支 camera 我看你應該會喜歡",
        "我在螢幕上看到你的頭像了",
        "我在螢幕上看到你剛傳的訊息",
        "我用 Teams 傳檔案給你",
        "先開 Teams 裡的 Word 檔",
        "可以幫我把網頁 zoom 大一點嗎",
        "這張圖 zoom 近一點會比較清楚",
        "我看你有空再幫我挑相機就好",
        "這台相機我看你一定會喜歡",
        "相機設定好了嗎？我們來聊晚餐",
        "這台相機好用嗎！有空再聊",
        "開相機拍張照給我看",
        "相機打開拍張照就好",
        "開相機的夜間模式拍照",
        "螢幕上正在看你推薦的文章",
        "我在螢幕上看你上週傳的照片",
        "我在螢幕上看妳分享的文件",
        "幫我 zoom 一下這張圖",
        "zoom 一下地圖",
        "This camera has 10x optical zoom",
        "這顆鏡頭的 zoom range 很廣",
        "這手機支援 pinch zoom 手勢",
        "Use pinch zoom on the map",
        "Zoom the webpage to 150%",
        "用滑鼠滾輪 zoom 畫面",
        "CSS zoom property is deprecated",
        "上課教怎麼 zoom in 看地圖",
        "I need to zoom in before the call",
        "Zoom in then meet me downstairs",
        "這顆鏡頭開箱後質感不錯",
        "鏡頭關係到畫質",
        "The camera is on sale",
        "Open the camera app",
        "螢幕上看到你的名字",
        "螢幕上看到你的留言",
    ]
    assert all(not AccountWorker._mentions_video_topic(text) for text in normal_texts)


def test_common_video_intents_are_not_hidden_by_text_or_equipment_context():
    variants = [
        "我們不要用鏡頭，打字就好",
        "鏡頭先關掉吧",
        "不想只看文字，我想在螢幕上看到你",
        "我先回完訊息，再在螢幕上看你",
        "我想看你出現在螢幕上",
        "相機設定好了，開鏡頭吧",
        "設定弄好了，開 camera 吧",
        "camera settings are ready, turn the camera on",
        "先拍照，再開鏡頭聊吧",
        "我們用 Teams 聊吧",
        "鏡頭不要開，我們打字就好",
        "相機設定好了再開鏡頭吧",
        "我在螢幕上看你的訊息和你本人",
        "我不想開手機鏡頭，我們打字就好",
        "可以開一下你的鏡頭嗎",
        "你的 camera 方便開一下嗎",
        "鏡頭麻煩開一下",
        "Please turn on the camera",
        "相機設定好了就開鏡頭吧",
        "你鏡頭呢？開一下吧",
        "我不只想在螢幕上看你的訊息也想看到你本人",
        "我們上 Teams 好不好",
        "Can we videocall later?",
        "要不要 videochat 一下",
        "晚點開實況給你看",
        "晚點開 live 給你看",
        "你的鏡頭可以開一下嗎？",
        "鏡頭記得開喔",
        "鏡頭幫我開一下",
        "先把麥克風跟鏡頭都打開",
        "等等開個 live 給你看",
        "Let's go live later",
        "希望你可以出現在我的螢幕上",
        "我先把網頁 zoom 放大再用 Zoom 上課",
        "我先把圖片 zoom in 再用 Zoom 聊",
        "我先 zoom in 看照片再開 Zoom 會議",
        "上課時用 Zoom 放大老師分享的畫面",
        "Let's call on Zoom in an hour",
        "We can meet on Zoom in 10 minutes",
        "透過鏡頭可以看到你",
        "Through the camera I can see you",
        "Let’s Zoom in an hour",
        "Let’s Zoom in 10 minutes",
        "我們 Zoom in 十分鐘後",
        "晚點 Zoom 3 點見",
        "Zoom 9:30 可以嗎",
        "看完圖片就用 Zoom 聽課",
        "先 zoom in 看照片再 Zoom in 10 minutes",
        "先 zoom in 看圖再用 Zoom 聽講座",
        "先把地圖 zoom out 再 Zoom 面試",
        "圖片傳好後用 Zoom 參加講座",
        "我們等下用 Zoom 連線",
        "我想看到鏡頭裡的你",
        "你的鏡頭能不能開一下",
        "I want to see you on camera",
        "I want to see you on my screen",
        "螢幕上是你的臉",
        "螢幕裡有你陪我就好了",
        "webcam 給我看一下",
        "跟螢幕裡的你聊",
    ]
    assert all(AccountWorker._mentions_video_topic(text) for text in variants)


def test_normal_text_is_not_blocked():
    normal_texts = [
        "先換 LINE 聊",
        "改天約出來見面",
        "我今天去電影院看電影",
    ]
    assert all(not AccountWorker._mentions_video_topic(text) for text in normal_texts)


def test_video_topic_reply_is_dropped_if_retry_still_violates_policy():
    async def main():
        worker = _worker()
        worker._call_ai = AsyncMock(side_effect=[
            "要不要打視訊聊聊",
            "那開鏡頭看一下",
        ])

        reply = await worker._generate_reply(_FakeEvent())

        assert reply == ""
        assert worker._call_ai.await_count == 2

    asyncio.run(main())


def test_send_layer_never_rewrites_text():
    """發送層必須原樣送出，避免 Telegram 與 DB 記憶不一致。"""
    async def main():
        worker = _worker()
        client = _FakeClient()
        worker.tg_client = cast(Any, client)
        worker.is_running = True
        text = "我今天用 Zoom 上課"

        await worker._send_message(-1001, text)

        assert client.sent == [(-1001, text)]

    asyncio.run(main())


def test_send_layer_fails_closed_for_text_over_sixty_characters():
    """所有帳號共用發送層；任何繞過生成器的超長文字也不能送出。"""
    async def main():
        worker = _worker()
        client = _FakeClient()
        worker.tg_client = cast(Any, client)
        worker.is_running = True

        sent = await worker._send_message(-1001, "字" * 61)

        assert sent is False
        assert client.sent == []

    asyncio.run(main())


def test_reply_later_sends_and_saves_the_same_regenerated_text():
    async def main():
        worker = _worker()
        client = _FakeClient()
        worker.tg_client = cast(Any, client)
        worker.tg_user_id = 456
        worker.is_running = True
        worker._call_ai = AsyncMock(side_effect=[
            "把鏡頭打開讓我看看你",
            "先加 LINE，聊得來再約出來",
        ])

        await worker._reply_later(_FakeEvent(), 0)

        assert client.sent == [(-1001, "先加 LINE，聊得來再約出來")]
        assert worker.db.messages == [
            ("w1", -1001, 456, worker.name, "assistant", "先加 LINE，聊得來再約出來")
        ]
        assert worker.stats["replies_sent"] == 1

    asyncio.run(main())


def test_reply_later_does_not_send_or_save_after_two_violations():
    async def main():
        worker = _worker()
        client = _FakeClient()
        worker.tg_client = cast(Any, client)
        worker.tg_user_id = 456
        worker.is_running = True
        worker._call_ai = AsyncMock(side_effect=[
            "要不要開 camera 聊",
            "那就用 Google Meet",
        ])

        await worker._reply_later(_FakeEvent(), 0)

        assert client.sent == []
        assert worker.db.messages == []
        assert worker.db.activities == []
        assert worker.stats["replies_sent"] == 0

    asyncio.run(main())


def test_reply_later_does_not_save_when_client_stops_during_generation():
    async def main():
        worker = _worker()
        client = _FakeClient()
        worker.tg_client = cast(Any, client)
        worker.tg_user_id = 456
        worker.is_running = True

        async def generate_then_stop(event):
            worker.is_running = False
            worker.tg_client = None
            return "這段文字沒有送到 Telegram"

        worker._generate_reply = generate_then_stop

        await worker._reply_later(_FakeEvent(), 0)

        assert client.sent == []
        assert worker.db.messages == []
        assert worker.db.activities == []
        assert worker.stats["replies_sent"] == 0

    asyncio.run(main())


def test_near_duplicate_reply_is_regenerated_with_recent_examples():
    async def main():
        worker = _worker()
        worker.db.recent_group_replies = [
            "幹，這人嘴巴真髒。管理員快把他丟出去啦。",
            "笑死，你也太會講了吧？",
        ]
        worker._call_ai = AsyncMock(side_effect=[
            "幹，這人嘴真髒，管理員快把他丟出去啦。",
            "先別理他，我們聊點別的。",
        ])

        reply = await worker._generate_reply(_FakeEvent())

        assert reply == "先別理他，我們聊點別的。"
        assert worker._call_ai.await_count == 2
        first_prompt = worker._call_ai.await_args_list[0].args[1]
        retry_prompt = worker._call_ai.await_args_list[1].args[1]
        assert "近期群內已發過" in first_prompt
        assert "換開頭、句型和語氣" in retry_prompt

    asyncio.run(main())


def test_dissimilar_reply_is_accepted_without_retry():
    async def main():
        worker = _worker()
        worker.db.recent_group_replies = ["笑死，你也太會講了吧？"]
        worker._call_ai = AsyncMock(return_value="我剛下班，等等想去買杯咖啡。")

        reply = await worker._generate_reply(_FakeEvent())

        assert reply == "我剛下班，等等想去買杯咖啡。"
        assert worker._call_ai.await_count == 1

    asyncio.run(main())
