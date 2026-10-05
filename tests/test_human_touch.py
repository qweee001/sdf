"""人味三件套：reaction 挑選、帳號私有話題池、跨帳號話題去重。"""
import asyncio
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


def test_send_group_reaction_uses_client_and_stats():
    w = _worker()
    w.persona = {"name": "t", "chat_style": "內斂反問"}
    sent = []

    async def send_reaction(chat_id, message_id, reaction=None):
        sent.append((chat_id, message_id, reaction))

    w.tg_client = SimpleNamespace(send_reaction=send_reaction)
    event = SimpleNamespace(
        id=99, chat_id=-1001, raw_text="剛下班", media=None
    )
    ok = asyncio.run(w._send_group_reaction(event))
    assert ok is True
    assert len(sent) == 1
    assert sent[0][0] == -1001
    assert sent[0][1] == 99
    assert sent[0][2] in worker_mod._REACTION_SETS["內斂反問"]
    assert w.stats["reactions_sent"] == 1
