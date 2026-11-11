"""普通回覆的讓位秒數吃「水軍佔比赤字」，不是固定 90 秒。

實測 11-10 10:00–10:20（群組剛開）：76 則真人只換到 1 次認領。
主動迴圈每兩分鐘必有一隻開口，`_ordinary_reply_allowed` 的「90 秒內有水軍
講過就讓位」因此永遠成立，而 starved 豁免要自己 200 秒沒講話＋全群 45 秒
沒水軍——主動密度下兩條都不成立。水軍在群裡只剩廣播、幾乎不回話。
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

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


def _event(text: str = "這句是真人講的普通訊息") -> SimpleNamespace:
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


def _stub(w: AccountWorker, monkeypatch, *, bots: int, total: int, others: list[float], mine_age: float = 0.0) -> None:
    async def _float(_key, default=0.0):
        return default

    async def _pressure(_gid):
        return {"human_5m": 0}

    async def _window(_gid, _window=180.0):
        return [
            {"role": "assistant" if i < bots else "user", "sender_id": i}
            for i in range(total)
        ]

    now = time.time()
    spoke = {int(w.tg_user_id or 0): (now - mine_age) if mine_age else 0.0}
    for index, ts in enumerate(others):
        spoke[300 + index] = ts

    async def _spoke(_gid):
        return spoke

    w.db = cast(
        Any,
        SimpleNamespace(group_traffic_window=_window, group_bot_last_spoke=_spoke),
    )
    monkeypatch.setattr(w, "_get_runtime_float", _float)
    monkeypatch.setattr(w, "_interaction_pressure", _pressure)
    monkeypatch.setattr(
        w, "_active_owner", lambda *_a, **_k: 0, raising=False
    )


def test_deficit_opens_the_gate_despite_recent_bot(monkeypatch):
    """群裡 40 則真人 0 則水軍、別人 30 秒前才講過 → 舊版擋死，新版要放行。"""
    w = _worker()
    _stub(w, monkeypatch, bots=0, total=40, others=[time.time() - 30], mine_age=25)
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is True


def test_share_met_restores_ninety_second_yield(monkeypatch):
    """水軍佔比已達標（40%），自己 40 秒前才講過 → 照舊讓位。"""
    w = _worker()
    _stub(w, monkeypatch, bots=8, total=20, others=[time.time() - 40], mine_age=40)
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is False
    assert int(w.stats.get("reply_block_gap", 0)) >= 1


def test_deficit_midway_scales_gap(monkeypatch):
    """佔比 15%＝赤字一半 → 讓位約 55 秒：30 秒前有人要讓、70 秒前則可答。"""
    w = _worker()
    _stub(w, monkeypatch, bots=3, total=20, others=[time.time() - 30], mine_age=30)
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is False

    w2 = _worker()
    _stub(w2, monkeypatch, bots=3, total=20, others=[time.time() - 70], mine_age=70)
    assert asyncio.run(w2._ordinary_reply_allowed(_event())) is True


def test_other_accounts_speaking_do_not_block_me(monkeypatch):
    """姊妹 15 秒前才講過，但我自己 70 秒沒開口 → 我照樣可以接（各講各的）。"""
    w = _worker()
    _stub(w, monkeypatch, bots=4, total=20, others=[time.time() - 15], mine_age=70)
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is True


def test_probability_lifts_when_deficit_high(monkeypatch):
    """熱場的 0.15 擲骰上限要隨赤字放大，否則缺口大也抽不到。"""
    w = _worker()
    _stub(w, monkeypatch, bots=1, total=30, others=[time.time() - 120])

    async def _pressure(_gid):
        return {"human_5m": 30, "ordinary_claimed_20s": 0, "ordinary_claimed_5m": 0, "human_sent_5m": 0}

    monkeypatch.setattr(w, "_interaction_pressure", _pressure)
    allowed = 0
    for message_id in range(1, 41):
        event = _event()
        event.id = message_id
        if asyncio.run(w._ordinary_reply_allowed(event)):
            allowed += 1
    # 舊上限 0.15 大約只剩 6 則；赤字大（佔比 3%）應該明顯更多
    assert allowed >= 12


def _other_reply(text: str = "他昨天不是才講過同一句") -> SimpleNamespace:
    event = _event(text)
    event.is_reply = True
    return event


def test_reply_to_other_is_not_a_dead_end(monkeypatch):
    """回覆別人的長句＝群裡的一般對話，缺口大時要能接。"""
    w = _worker()
    _stub(w, monkeypatch, bots=0, total=30, others=[time.time() - 60])

    async def _not_me(_event):
        return False

    async def _pick(_event):
        return 123

    async def _claim(*_a, **_k):
        return True

    monkeypatch.setattr(w, "_is_directed_at_me", _not_me)
    monkeypatch.setattr(w, "_pick_group_responder", _pick)
    monkeypatch.setattr(w, "_claim_human_reply", _claim)
    event = _other_reply()
    assert asyncio.run(w._should_reply(event)) is True
    assert int(w.stats.get("reply_soft_other", 0)) == 1


def test_short_agreement_reply_stays_with_humans(monkeypatch):
    """「真假」「超頂」這種附和句字太少，接起來像插嘴。"""
    w = _worker()
    _stub(w, monkeypatch, bots=0, total=30, others=[time.time() - 60])

    async def _not_me(_event):
        return False

    monkeypatch.setattr(w, "_is_directed_at_me", _not_me)
    assert asyncio.run(w._should_reply(_other_reply("真假"))) is False
    assert int(w.stats.get("reply_skip_directed_other", 0)) == 1


def test_soft_other_is_discounted_not_free(monkeypatch):
    """同樣的佔比下，插話的過關率要低於接球（打折 0.55）。"""
    def _passes(soft: bool) -> int:
        w = _worker()
        _stub(w, monkeypatch, bots=4, total=20, others=[time.time() - 120])
        count = 0
        for message_id in range(1, 61):
            event = _event("這句是別人對話裡的一句話")
            event.id = message_id
            if asyncio.run(w._ordinary_reply_allowed(event, soft=soft)):
                count += 1
        return count

    hard = _passes(False)
    soft = _passes(True)
    assert soft < hard
    assert soft > 0


def test_starved_bypasses_hot_budget(monkeypatch):
    """熱場 5 分鐘額度用光、自己 7 分鐘沒講話 → 回場保底要能穿過熱場預算。"""
    w = _worker()
    _stub(w, monkeypatch, bots=30, total=40, others=[time.time() - 50], mine_age=420)

    async def _pressure(_gid):
        return {"human_5m": 30, "ordinary_claimed_20s": 0, "ordinary_claimed_5m": 99, "human_sent_5m": 99}

    monkeypatch.setattr(w, "_interaction_pressure", _pressure)

    def _allowed() -> int:
        n = 0
        for message_id in range(1, 21):
            event = _event()
            event.id = message_id
            if asyncio.run(w._ordinary_reply_allowed(event)):
                n += 1
        return n

    # 熱場預算若還擋著，這裡會是 0；星號回場必須至少放行一則
    assert _allowed() >= 1


def test_group_vacuum_rejoins_after_five_minutes(monkeypatch):
    """整群五分鐘沒有水軍、真人還在講 → 真空回場，連 20 秒群級鎖都讓開。"""
    w = _worker()
    _stub(w, monkeypatch, bots=2, total=40, others=[time.time() - 400], mine_age=420)

    async def _pressure(_gid):
        return {"human_5m": 6, "ordinary_claimed_20s": 1, "ordinary_claimed_5m": 20, "human_sent_5m": 20}

    monkeypatch.setattr(w, "_interaction_pressure", _pressure)
    allowed = 0
    for message_id in range(1, 21):
        event = _event()
        event.id = message_id
        if asyncio.run(w._ordinary_reply_allowed(event)):
            allowed += 1
    assert allowed >= 1
    assert int(w.stats.get("vacuum_rejoin", 0)) >= 1
