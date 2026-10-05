"""人味系列：reaction／sticker、時段提示、事實記憶、私有話題池、跨帳號去重。"""
import asyncio
import os
import random
from types import SimpleNamespace
from typing import Any, cast

from app import worker as worker_mod
from app.persona import ADULT_JOKES, generate_proactive_topic
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


def test_private_pools_shuffled_differently_per_account():
    a = _worker("acc-a")
    b = _worker("acc-b")
    assert a._pools["adult"] != b._pools["adult"]
    # 私有池只是公池的亂序副本，內容一致
    assert sorted(a._pools["adult"]) == sorted(ADULT_JOKES)


def test_private_pool_order_deterministic_per_account():
    a1 = _worker("acc-x")
    a2 = _worker("acc-x")
    assert a1._pools["adult"] == a2._pools["adult"]
    assert a1._pools["daily"] == a2._pools["daily"]


def test_generate_proactive_topic_injected_pool_only():
    persona = {
        "gender": "女",
        "age": 30,
        "personality": "大膽風騷",
        "chat_style": "俏皮少量表情",
    }
    pools = {
        "daily": ["池A日常"],
        "girl": ["池B約會"],
        "boy": ["池B約會男"],
        "adult": ["池C露骨"],
        "persona": {"lively": ["池D人設"]},
    }
    rng = random.Random(42)
    seen = set()
    for _ in range(600):
        seen.add(generate_proactive_topic(persona, pools=pools, rng=rng))
    assert seen == {"池A日常", "池B約會", "池C露骨", "池D人設"}


def test_next_proactive_topic_skips_other_accounts_recent_texts(monkeypatch):
    w = _worker()
    w.persona = {
        "name": "t",
        "gender": "女",
        "age": 28,
        "personality": "大膽風騷",
        "chat_style": "俏皮少量表情",
    }
    w.db = _FakeDB()
    w.db.recent_bot_texts = [
        "姐妹A發過的開場",
        "姐妹B發過的開場",
    ]
    sequence = iter(
        [
            "姐妹A發過的開場",
            "姐妹B發過的開場",
            "一條全新開場",
        ]
    )
    monkeypatch.setattr(
        worker_mod,
        "generate_proactive_topic",
        lambda p, pools=None, rng=None: next(sequence),
    )
    topic = asyncio.run(w._next_proactive_topic(-1001))
    assert topic == "一條全新開場"
    assert w._normalized_reply(topic) in w._recent_proactive_topics


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
            sent.append(("sticker", chat_id, self._uploaded))

        async def __call__(self, request):
            sent.append(("reaction", getattr(request, "peer", None)))

    w.tg_client = _FakeClient()
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
            sent.append(("reaction", getattr(request, "peer", None)))

    w.tg_client = _FakeClient()
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


def test_send_group_reaction_uses_client_and_stats():
    w = _worker()
    w.persona = {"name": "t", "chat_style": "內斂反問"}
    sent = []

    class _CallableClient:
        # Telethon 1.44 走「client(TLRequest)」，fake 要可呼叫並收下請求物件
        async def __call__(self, request):
            sent.append(request)

    w.tg_client = _CallableClient()
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
