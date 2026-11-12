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


def _stub_spoke(w: AccountWorker, mine: float, others: list[float], *, bots: int = 3, total: int = 10) -> None:
    spoke = {int(w.tg_user_id or 0): mine if mine else 0.0}
    for index, ts in enumerate(others):
        spoke[300 + index] = ts
    async def _spoke(_gid):
        return spoke

    async def _window(_gid, _window=180.0):
        return [
            {"role": "assistant" if i < bots else "user", "sender_id": i}
            for i in range(max(total, 1))
        ]

    w.db = cast(Any, SimpleNamespace(group_bot_last_spoke=_spoke, group_traffic_window=_window))


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
    """自己 20 秒前才講過（佔比達標的場）→ 照舊讓位給群友發酵。"""
    w = _worker("Chen")
    _stub_gates(w, monkeypatch)
    now = time.time()
    _stub_spoke(w, mine=now - 60, others=[now - 30])
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is False


def test_starved_but_burst_under_min_gap_waits(monkeypatch):
    """餓很久的號可以插回來，但同一拍仍由群級 20 秒鎖擋著（這關過、下一關擋）。"""
    w = _worker("小小")
    _stub_gates(w, monkeypatch)
    now = time.time()
    _stub_spoke(w, mine=now - 4000, others=[now - 10])
    assert asyncio.run(w._ordinary_reply_allowed(_event())) is True


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


# ---------- 併發空窗：閘門放行到 DB 寫入之間（11:54:13／11:54:17） ----------


def _pair_workers() -> tuple[AccountWorker, AccountWorker, dict]:
    """兩號共用同一份 pending_burst，重現 manager 注入的共享狀態。"""
    shared: dict = {}
    a = _worker("小天後")
    b = _worker("Chen")
    a.tg_user_id, b.tg_user_id = 333, 444
    a.pending_burst = shared
    b.pending_burst = shared
    empty = cast(Any, SimpleNamespace(get_group_messages=_as_await([])))
    a.db = b.db = empty
    return a, b, shared


def test_pending_candidate_visible_before_it_hits_db():
    """A 剛放行、還沒送出（DB 空的），B 的閘門就必須看得到 A 那句。"""
    a, b, _shared = _pair_workers()
    a._burst_register(GROUP, "結婚了還在這邊喊要吃乾淨，你老婆知道嗎")
    lines = asyncio.run(b._sister_burst_lines(_event()))
    assert "你老婆知道嗎" in lines
    assert b.stats.get("burst_pending_seen") == 1


def test_pending_candidate_expires_after_window():
    a, b, _shared = _pair_workers()
    a._burst_register(GROUP, "結婚了還在這邊喊要吃乾淨")
    for entry in _shared[GROUP]:
        _shared[GROUP][_shared[GROUP].index(entry)] = (
            entry[0], entry[1], entry[2], time.time() - 31,
        )
    assert asyncio.run(b._sister_burst_lines(_event())) == ""


def test_own_pending_not_reported_as_sister():
    a, _b, _shared = _pair_workers()
    a._burst_register(GROUP, "結婚了還在這邊喊要吃乾淨")
    assert asyncio.run(a._sister_burst_lines(_event())) == ""


def test_burst_release_drops_entry():
    a, b, _shared = _pair_workers()
    a._burst_register(GROUP, "結婚了還在這邊喊要吃乾淨")
    a._burst_release(GROUP, "結婚了還在這邊喊要吃乾淨")
    assert asyncio.run(b._sister_burst_lines(_event())) == ""


# ---------- 開頭照搬真人（12:04:33 抄「你想試一下嗎」、12:29:21 抄「打腫了」） ----------


def test_echo_human_opening_vetoes():
    w = _worker("小小")
    humans = "你想試一下嗎\n為什麼排在小萬後面\n舔狗還不如小萬"
    assert "想試一" in w._local_veto("想試一下？先問問後面排隊的舔狗同不同意", [], humans, GROUP)


def test_echo_opening_caught_mid_sentence():
    w = _worker("RICH")
    humans = "誰把你奶打腫了\n昨晚被我打腫了\n開始意淫了嗎"
    assert "打腫了" in w._local_veto("打腫了還不冰敷，是想讓它一直腫著嗎", [], humans, GROUP)


def test_non_echo_opening_passes():
    w = _worker("RICH")
    humans = "誰把你奶打腫了\n昨晚被我打腫了"
    assert w._local_veto("看不出來萬哥這麼會吹", [], humans, GROUP) == ""
