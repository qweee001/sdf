"""人味系列：reaction／sticker、時段提示、事實記憶、即時話題生成、跨帳號去重。"""
import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast

from app import worker as worker_mod
from app.worker import AccountWorker


class _FakeDB:
    def __init__(self):
        self.recent_bot_texts = []

    async def recent_bot_texts_by_group(self, *_args, **_kwargs):
        return list(self.recent_bot_texts)


def _worker(account_id: str = "w1") -> AccountWorker:
    config = SimpleNamespace(
        ai_model="test-model",
        ai_temperature=0.8,
        ai_max_tokens=200,
        ai_timeout=17,
        ai_disable_thinking=True,
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        media_max_input_bytes=8 * 1024 * 1024,
    )
    return AccountWorker(
        account_id=account_id,
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=_FakeDB(),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[-1001],
    )


def test_pick_reaction_stays_in_style_set():
    w = _worker()
    w.persona = {"name": "t", "chat_style": "俏皮少量表情"}
    allowed = set(worker_mod._REACTION_SETS["俏皮少量表情"])
    for _ in range(50):
        assert w._pick_reaction("現在在幹嘛", False) in allowed


def test_pick_reaction_photo_uses_photo_set():
    w = _worker()
    w.persona = {"name": "t", "chat_style": "俏皮少量表情"}
    photo_allowed = set(worker_mod._REACTION_PHOTO)
    for _ in range(50):
        assert w._pick_reaction("", True) in photo_allowed


def test_pick_reaction_unknown_style_uses_fallback():
    w = _worker()
    w.persona = {"name": "t", "chat_style": "未知風格"}
    allowed = set(worker_mod._REACTION_FALLBACK)
    for _ in range(30):
        assert w._pick_reaction("嗨", False) in allowed


def test_pick_reaction_laugh_text_prefers_haha():
    w = _worker()
    w.persona = {"name": "t", "chat_style": "俏皮少量表情"}
    for _ in range(20):
        assert w._pick_reaction("哈哈笑死", False) == "😂"


def test_per_account_rng_is_deterministic_and_private():
    """每個帳號一把私有 RNG：同 id 重跑序列一致，不同 id 序列不同（reaction／貼圖錯開）。"""
    a1 = _worker("acc-x")
    a2 = _worker("acc-x")
    b = _worker("acc-y")
    seq_a1 = [a1._rng.random() for _ in range(5)]
    seq_a2 = [a2._rng.random() for _ in range(5)]
    seq_b = [b._rng.random() for _ in range(5)]
    assert seq_a1 == seq_a2
    assert seq_a1 != seq_b


def test_context_topic_uses_recent_group_messages_and_dedupes():
    """主動話題即時生成：prompt 要帶上群裡真正的上文，且同一句不重複發。"""

    class _CtxDB(_FakeDB):
        def __init__(self):
            super().__init__()
            self.msgs = [
                {"sender_id": 5, "sender_name": "阿宏", "role": "user", "content": "今天加班到十點"},
                {"sender_id": 6, "sender_name": "美玲", "role": "user", "content": "你也太拼了吧"},
            ]

        async def get_group_messages(self, *_args, **_kwargs):
            return list(self.msgs)

        async def get_group_shared_notes(self, *_args, **_kwargs):
            return []

    w = _worker("acc-ctx")
    w.db = _CtxDB()
    prompts = []

    async def fake_call(system_prompt, message, **kwargs):
        prompts.append(message)
        return "你也早點休息啦"

    w._call_ai = fake_call
    topic = asyncio.run(w._generate_context_topic(-1001))
    assert topic == "你也早點休息啦"
    assert "今天加班到十點" in prompts[0]
    assert "你也太拼了吧" in prompts[0]
    # 同一句再生成一次 → 去重擋下
    assert asyncio.run(w._generate_context_topic(-1001)) == ""


def test_sticker_assets_loaded():
    w = _worker()
    assert len(w._stickers) >= 1
    for path in w._stickers:
        assert path.endswith(".webp")
        assert os.path.exists(path)


def test_acknowledge_group_routes_to_sticker(monkeypatch):
    import asyncio

    from types import SimpleNamespace

    w = _worker()
    # 強制走 sticker 分支
    monkeypatch.setattr(worker_mod, "_STICKER_PROBABILITY", 1.0)
    sent = []

    class _FakeClient:
        def __init__(self):
            self._uploaded = None

        async def upload_file(self, path):
            self._uploaded = path
            return f"<file:{path}>"

        async def send_message(self, chat_id, media=None):
            # 真實 Telethon 沒有 media 參數；這裡留著是為了讓舊寫法當場失敗
            assert media is None, "貼圖不該再走 send_message(media=…)"
            sent.append(("text", chat_id, media))

        async def __call__(self, request):
            from telethon.tl.functions.messages import SendMediaRequest

            if isinstance(request, SendMediaRequest):
                sent.append(("sticker", request.peer, self._uploaded))
            else:
                sent.append(("reaction", getattr(request, "peer", None)))

    w.tg_client = _FakeClient()
    w.is_running = True
    event = SimpleNamespace(chat_id=-1001, id=77, raw_text="早安")
    ok = asyncio.run(w._acknowledge_group(event))
    assert ok is True
    assert len(sent) == 1
    kind, chat_id, path = sent[0]
    assert kind == "sticker"
    assert chat_id == -1001
    assert path in w._stickers
    assert w.stats["stickers_sent"] == 1


def test_acknowledge_group_falls_back_to_reaction(monkeypatch):
    import asyncio

    from types import SimpleNamespace

    w = _worker()
    # 沒有 sticker 資產時退回 reaction
    monkeypatch.setattr(w, "_stickers", [])
    monkeypatch.setattr(worker_mod, "_STICKER_PROBABILITY", 1.0)
    sent = []

    class _FakeClient:
        async def send_sticker(self, chat_id, path):
            sent.append(("sticker", chat_id, path))

        async def __call__(self, request):
            # 貼圖走 TL 原語 SendMediaRequest（send_message 沒有 media 參數）；
            # 其它請求（SendReactionRequest 等）記成 reaction。
            from telethon.tl.functions.messages import SendMediaRequest

            if isinstance(request, SendMediaRequest):
                sent.append(("sticker", getattr(request, "peer", None), self._uploaded))
            else:
                sent.append(("reaction", getattr(request, "peer", None)))

    w.tg_client = _FakeClient()
    w.is_running = True
    event = SimpleNamespace(chat_id=-1001, id=77, raw_text="早安")
    ok = asyncio.run(w._acknowledge_group(event))
    assert ok is True
    assert len(sent) == 1
    assert sent[0][0] == "reaction"
    assert w.stats["reactions_sent"] == 1


def test_time_hint_bands():
    w = _worker()
    w._taipei_hour = lambda: 3.5
    assert "凌晨" in w._time_hint()
    w._taipei_hour = lambda: 8.0
    assert "早晨" in w._time_hint()
    w._taipei_hour = lambda: 15.0
    assert "下午" in w._time_hint()
    w._taipei_hour = lambda: 23.5
    assert "深夜" in w._time_hint()


def test_time_mismatch_detection():
    w = _worker()
    # 17 點講早安/早餐＝穿幫
    w._taipei_hour = lambda: 17.0
    assert w._has_time_mismatch("早安呀～想吃什么早餐🥐")
    assert w._has_time_mismatch("早安🥵")
    # 17 點講晚安也怪
    assert w._has_time_mismatch("晚安啦")
    # 時段中立的句子放行
    assert not w._has_time_mismatch("今天好熱喔")
    # 早晨講早安、深夜講晚安都合規
    w._taipei_hour = lambda: 8.0
    assert not w._has_time_mismatch("早安呀～想吃什么早餐🥐")
    w._taipei_hour = lambda: 23.0
    assert not w._has_time_mismatch("晚安啦")


def test_note_is_trivial():
    assert AccountWorker._note_is_trivial("哈哈")
    assert AccountWorker._note_is_trivial("6666666")
    # 含「我」＝自我披露，再短也值得記
    assert not AccountWorker._note_is_trivial("我愛吃辣")
    assert not AccountWorker._note_is_trivial("今天天氣真的好好哦")


def test_outbound_guard_blocks_stopped_account():
    """F03: 帳號不在執行中時，reaction／貼圖在 RPC 前就被攔（RPC 次數=0）。"""
    w = _worker()
    sent = []

    class _FakeClient:
        async def upload_file(self, path):
            return f"<file:{path}>"

        async def __call__(self, request):
            sent.append(request)

    w.tg_client = _FakeClient()
    w.is_running = False
    event = SimpleNamespace(chat_id=-1001, id=77, raw_text="早安", media=None)
    assert asyncio.run(w._send_group_reaction(event)) is False
    assert asyncio.run(w._send_group_sticker(event)) is False
    assert len(sent) == 0
    assert not w.stats.get("reactions_sent")
    assert not w.stats.get("stickers_sent")


def test_outbound_guard_blocks_removed_group():
    """F03: 群組被移出範圍時，實際 RPC 前攔下。"""
    w = _worker()
    sent = []

    class _FakeClient:
        async def __call__(self, request):
            sent.append(request)

    w.tg_client = _FakeClient()
    w.is_running = True
    # selected_groups=[-1001]（_worker 預設），但事件來自別群
    event = SimpleNamespace(chat_id=-9999, id=77, raw_text="早安", media=None)
    assert asyncio.run(w._send_group_reaction(event)) is False
    assert len(sent) == 0


def test_outbound_guard_blocks_disabled_feature():
    """F03: 功能開關（reply_enabled）關閉時，reaction 也過同一道門。"""
    w = _worker()
    sent = []

    class _FakeClient:
        async def __call__(self, request):
            sent.append(request)

    w.tg_client = _FakeClient()
    w.is_running = True
    w.reply_enabled = False
    event = SimpleNamespace(chat_id=-1001, id=77, raw_text="早安", media=None)
    assert asyncio.run(w._send_group_reaction(event)) is False
    assert len(sent) == 0


def test_send_group_reaction_uses_client_and_stats():
    w = _worker()
    w.persona = {"name": "t", "chat_style": "內斂反問"}
    sent = []

    class _CallableClient:
        # Telethon 1.44 走「client(TLRequest)」，fake 要可呼叫並收下請求物件
        async def __call__(self, request):
            sent.append(request)

    w.tg_client = _CallableClient()
    w.is_running = True
    event = SimpleNamespace(
        id=99, chat_id=-1001, raw_text="剛下班", media=None
    )
    ok = asyncio.run(w._send_group_reaction(event))
    assert ok is True
    assert len(sent) == 1
    req = sent[0]
    assert req.peer == -1001
    assert req.msg_id == 99
    emojis = [r.emoticon for r in (req.reaction or [])]
    assert len(emojis) == 1
    assert emojis[0] in worker_mod._REACTION_SETS["內斂反問"]
    assert w.stats["reactions_sent"] == 1
