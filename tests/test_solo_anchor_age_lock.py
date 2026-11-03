"""純自曝場景與年齡鎖貫穿（2026-11-03 深夜桃花源實測破口）。

破口一：四個帳號的 Life Anchor 全部長成同一個形狀「剛做完某件事＋身體狀態＋
想要／看看哪個男人」，所以心境就算輪替，隨性放鬆那一型照樣收在「找男人」，
21:35~21:58 四則水軍零自曝就是這樣來的。

破口二：年齡鎖 9c78cac 只鎖在 get_system_prompt，決策輸入與裁判 prompt 仍讀
DB 原始值（小小 29），寫手拿 22、裁判拿 29，問年齡會自打嘴巴。

修法：每個帳號多一條「自｜」純自曝場景，整段沒人講自己時只從那條挑；
年齡一律走 persona.resolve_age()。
"""

import asyncio
import random
import time
from types import SimpleNamespace
from typing import Any, cast

from app.persona import NAME_AGE_LOCK, resolve_age
from app.worker import AccountWorker

GID = -1001
NOW = time.time()

# 當晚破口窗口的四則真實水軍發言（末三則＝零自曝）
REAL = [
    ("21:35:38", "Chen", "幫忙？先讓我看看你有幾兩重再說"),
    ("21:41:06", "Chen", "舔粉是什麼新招，弟弟你舌頭有這麼萬能嗎"),
    ("21:55:54", "RICH", "18公分不含頭？那實戰到底多長啊"),
    ("21:58:30", "小小", "出一張嘴誰不會，有種讓姊姊鑑定一下長短啊"),
]


def _worker(name: str = "小小", age: int = 29) -> AccountWorker:
    config = SimpleNamespace(
        ai_model="m",
        ai_temperature=0.8,
        ai_max_tokens=200,
        ai_timeout=17,
        ai_disable_thinking=True,
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        media_max_input_bytes=8 * 1024 * 1024,
        decision_enabled=False,
    )
    w = AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=cast(Any, SimpleNamespace()),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[GID],
    )
    w.persona = {"name": name, "gender": "女", "age": age}
    return w


def _minutes(clock: str) -> float:
    h, m, s = (int(x) for x in clock.split(":"))
    return h * 60 + m + s / 60


def _seed(w: AccountWorker, texts: list[str], base_min: float = 0) -> None:
    """時間戳一律貼著 NOW 往回排（窗是看牆上時鐘的 30 分鐘）。"""
    n = len(texts)
    w._recent_texts_by_group[GID] = list(texts)
    w._recent_texts_at[GID] = [NOW - (n - i) * 60 for i in range(n)]


def _event() -> SimpleNamespace:
    return SimpleNamespace(chat_id=GID, sender_id=1, message_id=1, text="")


def _force_first_pick():
    """random.choice 固定回第一個候選（心境 0＝隨性放鬆）。"""
    old = random.choice
    random.choice = lambda seq: seq[0]
    return lambda: setattr(random, "choice", old)


def test_solo_anchor_used_when_window_has_no_self_disclosure():
    w = _worker()
    _seed(w, [t for _, _, t in REAL[-3:]], base_min=_minutes(REAL[-1][0]))
    restore = _force_first_pick()
    try:
        note = w._build_directors_note(_event(), history=[], own_texts=[], last_human="")
    finally:
        restore()
    assert "追劇" in note  # 小小那條純自曝場景
    assert "跑業務" not in note  # 一般場景（會收在找男人）
    assert "先把自己的狀態講出來" in note


def test_normal_anchor_when_someone_already_talked_about_herself():
    w = _worker()
    _seed(
        w,
        ["幫忙？先讓我看看你有幾兩重再說", "舌頭有這麼萬能嗎", "剛洗完澡全身滑溜溜的，冷氣吹到發冷"],
        base_min=_minutes(REAL[-1][0]),
    )
    restore = _force_first_pick()
    try:
        note = w._build_directors_note(_event(), history=[], own_texts=[], last_human="")
    finally:
        restore()
    assert "追劇" not in note
    assert "先把自己的狀態講出來" not in note


def test_every_account_has_a_solo_anchor():
    """四號都要有純自曝場景，否則該帳號永遠只會收在找男人。"""
    w = _worker()
    for name in NAME_AGE_LOCK:
        w.persona = {"name": name, "gender": "女", "age": 30}
        w._recent_texts_by_group[GID] = [t for _, _, t in REAL[-3:]]
        w._recent_texts_at[GID] = [NOW - 60 * (3 - i) for i in range(3)]
        restore = _force_first_pick()
        try:
            note = w._build_directors_note(_event(), history=[], own_texts=[], last_human="")
        finally:
            restore()
        assert "自｜" not in note  # 標籤不該露到 prompt
        assert "先把自己的狀態講出來" in note, name


def test_anchor_no_longer_recycles_chu_yi_zhang_zui():
    """小天後「櫃上」那條原本寫死「只會出一張嘴」，跟小小撞同一個慣用語。"""
    for name in ("小小", "小天後", "RICH", "Chen"):
        w = _worker(name, 30)
        seen_anchors = []
        orig = w._pick_life_anchor

        def spy(anchors, *a, _orig=orig, _acc=seen_anchors, **k):
            _acc.extend(anchors)
            return _orig(anchors, *a, **k)

        w._pick_life_anchor = spy  # type: ignore[method-assign]
        w._build_directors_note(_event(), history=[], own_texts=[], last_human="")
        table = " ".join(sc for _, sc in seen_anchors)
        assert "出一張嘴" not in table, name
        if name == "小天後":
            assert "奧客" in table  # 「櫃上」那條還在，只是換了講法


def test_resolve_age_locks_named_accounts():
    assert resolve_age({"name": "小小", "age": 29}) == 22
    assert resolve_age({"name": "Chen", "age": 31}) == 38
    assert resolve_age({"name": "王雅婷", "age": 31}) == 31
    assert resolve_age({"name": "王雅婷"}) == 25


def test_decision_input_carries_locked_age():
    w = _worker("小小", 29)
    w.db = cast(Any, SimpleNamespace(
        get_recent_messages=_async([]),
        get_group_shared_notes=_async([]),
    ))
    state, _opts = asyncio.run(w._decision_input(_event()))
    assert "22 歲" in state
    assert "29 歲" not in state


def test_gateway_reviewer_gets_locked_age():
    w = _worker("小小", 29)
    captured: dict = {}

    async def fake_create(**req):
        captured.update(req)
        msg = SimpleNamespace(content='{"sendable":0.95,"issue":"none","reason":"ok"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    w.ai_client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))))
    w.config.vision_model = ""
    res = asyncio.run(
        w._review_candidate_gateway("上下文", "剛洗完澡，腿還軟著", "", "", "")
    )
    user_msg = captured["messages"][1]["content"]
    assert "22歲" in user_msg
    assert "29歲" not in user_msg
    assert res and res["issue"] == "none"


def _async(value):
    async def _f(*_a, **_k):
        return value
    return _f
