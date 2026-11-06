"""對象軸第二輪：模型手上沒有人名，五條候選全端回「你們」。

15:02:22 小天後「嘴巴這麼壞，連年輕姊姊都搞不定還想越級打怪嗎🤣」、
15:18:42 小天後「喝完咖啡哪夠，當然要續攤去床上流汗才過癮啊」、
14:48:38 RICH「舔一年舌頭早就麻掉，光看你們誇口我都快睡著了」——
三則橫跨一小時都沒有對象，`target_rewrite` 統計有動，出海的卻還是原句，
因為重寫全部失敗、回 "" 之後呼叫端照發。回退沒有記數，等於沒人知道對象軸失效。

修法兩條：
1. 人名從最近真人訊息自己抓進提示，候選沒帶到名字就當沒指名（真比對，不是提示）。
2. 換不到要記 target_fallback，回退率才量得到。
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1002

_BROADCAST = "喝完咖啡哪夠，當然要續攤去床上流汗才過癮啊"
_BROADCAST_BEFORE = "嘴巴這麼壞，連年輕姊姊都搞不定還想越級打怪嗎🤣"


def _worker(name: str = "小天後") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 30}
    return w


def _seed(w: AccountWorker, pairs) -> None:
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


# ---- 人名抽取 -----------------------------------------------------------


def test_target_names_takes_newest_humans_and_skips_bots_and_admin():
    rows = [
        {"role": "user", "sender_name": "輕輕", "content": "有沒有這麼誇張"},
        {"role": "assistant", "sender_name": "小小", "content": "小小自己"},
        {"role": "user", "sender_name": "管理員", "content": "入會訊息"},
        {"role": "user", "sender_name": "地暖鋪著", "content": "你住哪裡？"},
        {"role": "user", "sender_name": "戰豆", "content": "用兩個茄子是你嗎"},
        {"role": "user", "sender_name": "輕輕", "content": "重複的不算兩次"},
        {"role": "user", "sender_name": "風口浪尖", "content": "我請你喝咖啡呀"},
        {"role": "user", "sender_name": "華發", "content": "第四個以後不取"},
    ]
    assert AccountWorker._target_names(rows, limit=3) == ["華發", "風口浪尖", "輕輕"]


def test_target_names_empty_when_nobody_talked():
    assert AccountWorker._target_names([]) == []
    assert AccountWorker._target_names([{"role": "user", "sender_name": "管理員"}]) == []


# ---- 候選真的要比對名字 ------------------------------------------------


def test_candidate_without_the_name_is_rejected_even_with_a_single_you():
    """「你有練過嗎」有單數的你，還是在問全場，不算指名。"""
    w = _worker()
    bodies = iter([
        "你這句有夠敢講，害我剛喝的美式差點嗆到",
        "戰豆你這句有夠敢講，害我剛喝的美式差點嗆到",
    ])

    async def generate(_hint: str) -> str:
        return next(bodies)

    async def review(_ctx, cand, *_a, **_k):
        return {"sendable": 0.9, "issue": "none"}

    w._review_candidate = review  # type: ignore[assignment]
    out = asyncio.run(
        w._pick_passing_candidate(
            "ctx", "", "hint", generate, group_id=GID, limit=2,
            require_target=True, require_names=("戰豆", "輕輕"),
        )
    )
    assert out == "戰豆你這句有夠敢講，害我剛喝的美式差點嗆到"
    assert w.stats["target_name_rejected"] == 1


# ---- 語氣詞尾不再閃過連問 ----------------------------------------------


def test_soft_tail_after_a_question_counts_as_the_same_register():
    """15:18 接在 15:02 的問句後面，換成「啊」收尾照樣是同一種腔。"""
    w = _worker()
    assert w._soft_tail_repeat([_BROADCAST_BEFORE], _BROADCAST)


def test_soft_tail_after_an_assertion_passes():
    """上一則已經是斷言（結尾 了／掉），這則用「啊」收尾沒問題。"""
    w = _worker()
    assert not w._soft_tail_repeat(["舌頭早就麻掉了"], _BROADCAST)
    assert not w._soft_tail_repeat(["光看你們誇口我就快睡著"], _BROADCAST)


def test_two_soft_tails_after_a_question_are_rejected():
    w = _worker()
    assert w._soft_tail_repeat(["越級打怪嗎", "續攤才過癮啊"], "明天再約一次喔")


def test_soft_tail_shows_up_in_the_local_veto_after_the_idiom_axis():
    """慣用詞撞車的理由更具體，不能被語氣詞軸搶先。"""
    w = _worker()
    own = ["沒舔過還敢嘴硬，弟弟你是怕一舔就回不去嗎"]
    assert "嘴硬" in w._local_veto("嘴硬成這樣，被踩的時候倒是挺享受的嘛", own, "")
    assert "同一個腔" in w._local_veto("這一杯喝下去大概整晚都不用睡了嘛", own, "")


# ---- 主動路徑：名字進提示，換不到要記回退 -------------------------------


def test_proactive_hint_carries_real_names():
    w = _worker()
    _seed(w, [
        (30 * 60, "你們這幾個是要吵到什麼時候"),
        (20 * 60, "舔狗被弄哭也是很可憐欸"),
    ])
    rows = [
        {"role": "user", "sender_name": "地暖鋪著", "content": "你住哪裡？"},
        {"role": "user", "sender_name": "輕輕", "content": "有沒有這麼誇張"},
    ]

    async def fake_rows(_gid, limit=14):
        return rows

    w.db = cast(Any, SimpleNamespace(get_group_messages=fake_rows))
    hints: list[str] = []

    async def fake_pick(_ctx, _directive, hint, *_a, **kwargs):
        hints.append(hint)
        return ""

    w._pick_passing_candidate = fake_pick  # type: ignore[assignment]
    out = asyncio.run(w._proactive_echo_rewrite(GID, _BROADCAST, "", [], ""))
    assert out == ""
    assert "輕輕" in hints[0] and "地暖鋪著" in hints[0]
    assert "輕輕" in hints[1]  # 第二輪只准叫名字開頭
    assert w.stats["target_retry_second"] == 1
    assert w.stats["target_fallback"] == 1


def test_proactive_success_does_not_count_a_fallback():
    w = _worker()
    _seed(w, [
        (30 * 60, "你們這幾個是要吵到什麼時候"),
        (20 * 60, "舔狗被弄哭也是很可憐欸"),
    ])
    rows = [{"role": "user", "sender_name": "輕輕", "content": "有沒有這麼誇張"}]

    async def fake_rows(_gid, limit=14):
        return rows

    w.db = cast(Any, SimpleNamespace(get_group_messages=fake_rows))

    async def fake_pick(*_a, **_k):
        return "輕輕你這句有夠敢講"

    w._pick_passing_candidate = fake_pick  # type: ignore[assignment]
    assert asyncio.run(w._proactive_echo_rewrite(GID, _BROADCAST, "", [], ""))
    assert "target_fallback" not in w.stats
