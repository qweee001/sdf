"""餓死帳號回場／併發撞點／人名開場遮蔽（2026-11-04 10:10–10:41 桃花源實測）。

- 小小 11-03 21:58 之後 12h43m 零發言，整場 decision_calls=1（其他號 8~12）：
  owner 認領可繞過「90 秒內有水軍講過就讓位」，沒有 owner 的帳號被熱場 burst 永久鎖死。
- 10:30:29 小天後「香腸也能拿來比」→ 10:30:33 RICH「我看是香腸贏了吧」（同時生成，同一點）。
- Chen 10:14／10:16／10:25 三則全是「喊人名＋催」：起手詞取前兩字全是名字，字面永不撞。
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast


from app.worker import AccountWorker

GROUP = -1002229799107


def _worker(name: str = "Chen") -> AccountWorker:
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
        base_reply_probability=1.0,
        reply_enabled=True,
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
        selected_groups=[GROUP],
    )
    w.persona = {"name": name, "gender": "女", "age": 38}
    w.tg_user_id = 111
    return w


def _event(text: str = "你們這樣講我聽不懂耶") -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=GROUP,
        id=9001,
        sender_id=555,
        raw_text=text,
        media=None,
        is_reply=False,
        reply_to=None,
        mentioned=False,
        is_bot=False,
        get_sender=None,
    )


def _stub_gates(w: AccountWorker, monkeypatch) -> None:
    async def _float(_key, default=0.0):
        return default

    async def _pressure(_gid):
        return {}

    monkeypatch.setattr(w, "_get_runtime_float", _float)
    monkeypatch.setattr(w, "_interaction_pressure", _pressure)


def _stub_spoke(w: AccountWorker, mine: float, others: list[float]) -> None:
    spoke = {int(w.tg_user_id or 0): mine if mine else 0.0}
    for index, ts in enumerate(others):
        spoke[300 + index] = ts
    async def _spoke(_gid):
        return spoke

    w.db = cast(Any, SimpleNamespace(group_bot_last_spoke=_spoke))


def _as_await(value):
    async def _inner(*_a, **_k):
        return value

    return _inner


def test_starved_account_rejoins_despite_recent_bot(monkeypatch):
    """自己 600 秒沒講話、群裡 50 秒前才有人講 → 應該放行回場。"""
    w = _worker("小小")
    _stub_gates(w, monkeypatch)
    now = time.time()
    _stub_spoke(w, mine=now - 700, others=[now - 50])
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is True
    assert w.stats.get("starved_rejoin") == 1


def test_recent_speaker_still_yields(monkeypatch):
    """自己 2 分鐘前才講過、別人 30 秒前也講 → 照舊讓位。"""
    w = _worker("Chen")
    _stub_gates(w, monkeypatch)
    now = time.time()
    _stub_spoke(w, mine=now - 120, others=[now - 30])
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is False


def test_starved_but_burst_under_min_gap_waits(monkeypatch):
    """餓歸餓，別人 10 秒前才發過還是要等（不疊在同一拍）。"""
    w = _worker("小小")
    _stub_gates(w, monkeypatch)
    now = time.time()
    _stub_spoke(w, mine=now - 4000, others=[now - 10])
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is False


def _rows(*items):
    now = time.time()
    out = []
    for sender_id, name, body, age in items:
        out.append(
            {
                "role": "assistant",
                "sender_id": sender_id,
                "sender_name": name,
                "content": body,
                "timestamp": now - age,
            }
        )
    return out


def test_sister_burst_lines_pick_recent_other_accounts():
    """4 秒前姊妹那句要被抓出來，自己的與超過 15 秒的都排除。"""
    w = _worker("RICH")
    # 依發送順序（舊→新）：自己那句最新
    rows = _rows(
        (444, "Chen", "舊的，超過窗", 40),
        (333, "小天後", "香腸也能拿來比，是對自己多有信心", 4),
        (111, "RICH", "自己講的", 2),
    )
    w.db = cast(Any, SimpleNamespace(get_group_messages=_as_await(rows)))
    lines = asyncio.run(w._sister_burst_lines(_event()))
    assert "香腸也能拿來比" in lines
    assert "舊的，超過窗" not in lines
    assert "自己講的" not in lines
    assert "香腸也能拿來比" in lines
    assert "舊的，超過窗" not in lines
    assert "自己講的" not in lines


def test_no_sister_line_when_quiet():
    w = _worker("RICH")
    w.db = cast(Any, SimpleNamespace(get_group_messages=_as_await([])))
    assert asyncio.run(w._sister_burst_lines(_event())) == ""
    assert asyncio.run(w._sister_burst_lines(_event())) == ""


def test_opening_token_strips_leading_member_name():
    """「琪琪都想看了…」撞的是「都想」，不是名字。"""
    w = _worker("Chen")
    w._remember_speaker_name(GROUP, "琪琪")
    w._remember_speaker_name(GROUP, "後天")
    assert w._opening_token("琪琪都想看了小萬還不快掏出來", GROUP) == "都想"
    assert w._opening_token("後天你雞雞當棍子先量量", GROUP) == "你雞"


def test_name_opening_streak_vetoes_third_name_opener():
    """Chen 10:14/10:16 都先喊人名 → 第三則再喊就要退。"""
    w = _worker("Chen")
    w._remember_speaker_name(GROUP, "後天")
    w._remember_speaker_name(GROUP, "琪琪")
    own = ["琪琪都想看了小萬還不快掏出來", "後天你雞雞當棍子先量量"]
    note = w._opening_repeat_note(own, GROUP)
    assert "人名" in note
    veto = w._local_veto("琪琪快點掏出來啦", own, "", GROUP)
    assert "喊人名" in veto


def test_name_opening_quiet_when_not_a_streak():
    w = _worker("Chen")
    w._remember_speaker_name(GROUP, "琪琪")
    own = ["琪琪都想看了小萬還不快掏出來", "剛在店裡燙新進的貼身洋裝"]
    assert w._opening_repeat_note(own, GROUP) == ""
