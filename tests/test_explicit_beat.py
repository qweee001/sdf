"""露骨尺度通道：決策檔位要能決定長相、色句要吃到現場色度、冷卻要分階。

實測（11-11 15:00–18:00 桃花源）：2.9 小時水軍 61 則，**單則露骨詞最多 1 個、
0 則有兩個軸以上**，而現場真人早就在講「用穴夾你雞雞嗎」「可以舔舔噴嗎」。
根因是決策層的 flirty 只變成提示文字、長度由 random 決定，加上冷卻規則在
最色的時候下令「絕對禁止再聊性」。這裡把三件事釘住：

1. flirty ≥2 時不准再出碎字／短句
2. 露骨節拍只看露骨軸數（部位／動作／反應），不看字數
3. 冷卻分兩階：真人最後一句還在講色＝只降半檔
"""

import random
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.worker import AccountWorker, _STRUCT_BANDS

GID = -1002


def _worker(name: str = "小小") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 22}
    for n in ("規范", "縍羣 怠", "小天後", "RICH"):
        w._remember_speaker_name(GID, n)
    return w


def _rows(texts, role="user", step=20.0):
    now = time.time()
    return [
        {"role": role, "content": t, "timestamp": now - (len(texts) - i) * step}
        for i, t in enumerate(texts)
    ]


def _event(text="用穴夾你雞雞嗎", flirty=None):
    ev = SimpleNamespace(
        chat_id=GID,
        sender_id=111,
        raw_text=text,
        mentioned=False,
        is_reply=False,
        id=9001,
        media=None,
    )
    if flirty is not None:
        ev._sdf_decision = {"action": "reply", "flirty": flirty}
    return ev


# ---- 1. 露骨軸計數 ------------------------------------------------------


def test_explicit_axes_counts_part_act_react_once_each():
    w = _worker()
    assert w._explicit_axes("") == 0
    assert w._explicit_axes("真假") == 0
    # 只有一個軸
    assert w._explicit_axes("小穴") == 1
    assert w._explicit_axes("插") == 1
    # 部位＋反應：兩軸（「好濕」已經有身體反應，不是只有點名器官）
    assert w._explicit_axes("小穴好濕") == 2
    # 部位＋動作：兩軸
    assert w._explicit_axes("小穴被你插到") == 2
    # 部位＋動作＋反應：三軸
    assert w._explicit_axes("小穴被插到發抖") == 3


def test_human_explicit_ratio_reads_the_field_not_own_lines():
    w = _worker()
    hot = _rows(["用穴夾你雞雞嗎", "想舔噴", "好濕"] * 4)
    cold = _rows(["真假", "吃飽沒", "下班了"] * 4)
    assert w._human_explicit_ratio(hot) > 0.8
    assert w._human_explicit_ratio(cold) == 0.0
    assert w._human_explicit_ratio(_rows(["真假"])) == 0.0


# ---- 2. 節拍機率 --------------------------------------------------------


def test_beat_probability_follows_field_and_flirty():
    w = _worker()
    assert w._beat_probability(0.0, 0) == pytest.approx(0.0)
    low = w._beat_probability(0.2, 0)
    mid = w._beat_probability(0.2, 2)
    top = w._beat_probability(0.6, 3)
    assert low < mid < top
    # 上限 0.45：超過就變成四個人輪流演講
    assert w._beat_probability(1.0, 3) == pytest.approx(0.45)


# ---- 3. 決策檔位覆蓋隨機 ------------------------------------------------


def test_flirty_two_kills_frag_and_short(monkeypatch):
    """他明確在撩你（2/3 檔）時，這則不准是碎字或短句。"""
    w = _worker()
    history = _rows(["用穴夾你雞雞嗎", "想舔噴", "好濕", "夾得住嗎"])
    ev = _event(flirty=3)
    monkeypatch.setattr(random, "random", lambda: 0.05)  # 基礎 roll 會落在 short
    w._build_directors_note(ev, history, own_texts=["咬回去"], last_human="用穴夾你雞雞嗎")
    # 長鏡頭也算（他正在撩，一段到底比碎字更對）
    assert w._struct_mode in ("beat", "dirty", "long")


def test_flirty_zero_turns_beat_back_into_life_scene(monkeypatch):
    w = _worker()
    history = _rows(["吃飽沒", "真假", "下班了", "好累"])
    ev = _event(text="吃飽沒", flirty=0)
    monkeypatch.setattr(random, "random", lambda: 0.95)  # 基礎 roll 會落在 beat
    w._build_directors_note(ev, history, own_texts=["笑死"], last_human="吃飽沒")
    assert w._struct_mode == "scene"


def test_beat_cap_is_not_crushed_by_human_median():
    """真人中位 8 字時，節拍仍要拿到 45 字的額度，否則三段擠不進去。"""
    w = _worker()
    w._struct_mode = "beat"
    w._human_ref = 8
    w._len_tail = False
    assert w._struct_char_cap(90) == _STRUCT_BANDS["beat"][1]


# ---- 4. 冷卻分階 --------------------------------------------------------


def test_cooldown_half_when_last_human_is_still_explicit():
    w = _worker()
    history = _rows(["小穴好濕", "想被插穴", "舔噴嗎", "好硬", "騷穴夾緊"])
    note = w._topic_heat_hint(GID, history)
    assert "降半檔" in note
    assert "絕對禁止" not in note


def test_cooldown_hard_when_field_already_moved_on():
    w = _worker()
    history = _rows(["小穴好濕", "舔噴", "好硬", "騷穴夾緊", "吃飽沒", "今天好累"])
    note = w._topic_heat_hint(GID, history)
    assert "絕對禁止" in note


# ---- 5. 軟性退回 --------------------------------------------------------


def test_soft_veto_is_marked_and_hard_veto_is_not():
    w = _worker()
    w._record_sent_text(GID, "內衣貼在胸口快悶死")
    soft = w._local_veto("胸口好燙", ["笑死"], "真假", GID)
    assert w._split_veto(soft)[1] is True

    w2 = _worker()
    hard = w2._local_veto("他剛說想吃火鍋", [], "他剛說想吃火鍋", GID)
    assert hard
    assert w2._split_veto(hard)[1] is False


# ---- 6. 名字字形距離 ----------------------------------------------------


def test_garbled_two_char_nickname_is_repaired():
    """16:56 實測「靐羣莫以為舔一句就有獎」：本尊叫「縍羣 怠」。"""
    w = _worker()
    assert w._fix_name_address("靐羣莫以為舔一句就有獎", GID) == "縍羣，莫以為舔一句就有獎"


# ---- 7. 尺度要跟著現場，不是背詞表 ---------------------------------------


def test_tension_ladder_follows_corpus_density_not_wordlist():
    """階梯本來用「洗澡」這種生活詞當 L3 訊號，密度才是真的色度。"""
    w = _worker()
    hot = _rows(["小穴被插到發抖", "舔到腿軟", "夾緊一點", "想被插"])
    cold = _rows(["吃飽沒", "剛洗完澡", "下班了"])
    assert "L3" in w._tension_ladder_hint(GID, "夾得住嗎", hot)
    # 「洗澡」以前會被判成 L3，這裡應該還是日常
    assert "L1" in w._tension_ladder_hint(GID, "吃飽沒", cold)


def test_water_only_cap_keeps_second_level_unless_three_in_a_row():
    """純水軍串才壓到「稍微暗示」；一兩則時仍給「明顯勾引」。"""
    from app.worker import _WATER_ONLY_FLIRTY_CAP

    assert _WATER_ONLY_FLIRTY_CAP == 1


def test_reviewer_prompt_has_intensity_alignment_rule():
    """審核要看得出「對方在講身體、候選卻回天氣」＝沒接上。"""
    import inspect

    from app.worker import AccountWorker

    src = inspect.getsource(AccountWorker._review_candidate_gateway)
    assert "色度對齊" in src
    assert "offtopic" in src


# ---- 8. 長鏡頭通道 -------------------------------------------------------


def test_long_band_only_in_slow_field(monkeypatch):
    """熱場抽長鏡頭＝遲到，只有兩分鐘 <4 則真人時才准。"""
    w = _worker()
    hot = _rows(["用穴夾你雞雞嗎"] * 14, step=3.0)     # 兩分鐘 14 則（>熱場門檻）
    slow = _rows(["吃飽沒", "真假"], step=60.0)          # 兩分鐘 2 則
    ev = _event(flirty=0)
    # 碎字機率歸零（否則 frag 會先吃掉這一骰），random 全給 0 讓 long 那骰必中
    monkeypatch.setattr(w, "_frag_probability", lambda *a: 0.0)
    monkeypatch.setattr(random, "random", lambda: 0.0)
    w._build_directors_note(ev, hot, own_texts=[], last_human="用穴夾你雞雞嗎")
    hot_mode = w._struct_mode
    w._build_directors_note(ev, slow, own_texts=[], last_human="吃飽沒")
    slow_mode = w._struct_mode
    assert slow_mode == "long"
    assert hot_mode != "long"


def test_long_cap_is_not_dragged_down_by_human_median():
    w = _worker()
    w._struct_mode = "long"
    w._human_ref = 8
    w._len_tail = False
    # 模型實際會寫到 300 字，帶頂跟著抬（90 字時代 82 次抽中 0 則落地）
    assert w._struct_char_cap(90) == 190


# ---- 9. 同一拍撞名 -------------------------------------------------------


def test_same_beat_name_vetoes_second_line(monkeypatch):
    """20:20 RICH 點名規范，20:21 小小也點規范 → 第二則退回。"""
    w = _worker()
    w._record_sent_text(GID, "規范，先脫褲子再來說")
    # 直接驗這條軸（走 _local_veto 時起手詞那條軟軸會先命中，兩條都對）
    assert w._same_beat_name("規范，褲子脫了直接驗貨", GID) == "規范"
    assert w._same_beat_name("剛剛規范那個褲子", GID) == "規范"
    hard = w._local_veto("剛剛規范那個褲子脫了", [], "真假", GID)
    assert w._veto_code(hard) == "same_beat_name"


def test_same_beat_window_expires():
    from app.worker import _SCENE_FADE_SELF  # noqa: F401  (只確認常數存在)

    w = _worker()
    w._recent_texts_by_group[GID] = ["規范，先脫褲子再來說"]
    w._recent_texts_at[GID] = [time.time() - 600]  # 十分鐘前
    assert w._same_beat_name("規范，褲子脫了", GID) == ""


# ---- 10. 主動與回覆同一套判斷 -------------------------------------------


def test_proactive_review_also_requires_a_target():
    import inspect

    from app.worker import AccountWorker

    src = inspect.getsource(AccountWorker._proactive_loop)
    assert "require_target=_p_need_target" in src


def test_pending_name_blocks_the_concurrent_second_line():
    """兩號同時生成時誰都看不到對方成品，所以過了守門就先佔名。"""
    w = _worker()
    w._note_pending_names("規范，先脫褲子再來說", GID)   # 第一則已過守門、還沒落地
    assert w._same_beat_name("規范，褲子脫了直接驗貨", GID) == "規范"


def test_pending_name_expires():
    w = _worker()
    w._pending_names[(GID, "規范")] = time.time() - 200
    assert w._same_beat_name("規范，褲子脫了", GID) == ""


def test_recent_named_note_lists_names_and_pending():
    w = _worker()
    w._record_sent_text(GID, "規范，先脫褲子再來說")
    w._note_pending_names("小天後，你敢嗎", GID)
    note = w._recent_named_note(GID)
    assert "規范" in note and "小天後" in note
    assert "換人" in note


# ---- 7. 留白半句（halt）-------------------------------------------------

def test_halt_band_is_half_sentence_length():
    assert _STRUCT_BANDS["halt"] == (3, 9)


def test_halt_takes_over_a_short_line(monkeypatch):
    w = _worker()
    w._human_ref = 20
    # 本來要講一句完整的（short）→ 換成半句；碎字先被關掉才輪到留白骰
    monkeypatch.setattr(w, "_frag_probability", lambda *a: 0.0, raising=False)
    # 熱場：長鏡頭被擋掉（它只在慢場抽），這則才會落到短句→留白
    monkeypatch.setattr(w, "_burst_heat", lambda *a: 12, raising=False)
    monkeypatch.setattr(random, "random", lambda: 0.02)
    w._build_directors_note(_event(), _rows(["真假", "超頂"]), own_texts=[], last_human="超頂")
    assert w._struct_mode == "halt"


def test_halt_line_is_allowed_to_be_unfinished():
    w = _worker()
    w._struct_mode_force = "halt"
    note = w._build_directors_note(_event(), _rows(["好累"]), own_texts=[], last_human="好累")
    assert "半句" in note
    # 留白帶不會被長鏡頭的 90 字下限追殺
    w._struct_mode_force = ""
    w._struct_mode = "halt"
    assert w._struct_char_cap(9) <= 12


# ---- 8. 反 rush ---------------------------------------------------------

def test_scene_rush_counts_two_axis_bot_lines(monkeypatch):
    w = _worker()
    monkeypatch.setattr(
        w, "_cross_recent_items",
        lambda gid, sec: [("小穴被你插到發抖", 20.0), ("想舔噴嗎", 40.0), ("早就濕了", 60.0)],
        raising=False,
    )
    assert w._scene_rush(GID) == 1
    monkeypatch.setattr(
        w, "_cross_recent_items",
        lambda gid, sec: [("小穴被你插到發抖", 20.0), ("奶頭被咬到硬", 40.0)],
        raising=False,
    )
    assert w._scene_rush(GID) == 2


def test_rush_block_forces_single_axis(monkeypatch):
    w = _worker()
    w._struct_mode_force = "beat"
    monkeypatch.setattr(w, "_scene_rush", lambda gid: 3, raising=False)
    note = w._build_directors_note(_event(), _rows(["用穴夾你雞雞嗎"]), last_human="用穴夾你雞雞嗎")
    assert "只准一個軸" in note


def test_no_rush_block_when_field_has_room(monkeypatch):
    w = _worker()
    w._struct_mode_force = "beat"
    monkeypatch.setattr(w, "_scene_rush", lambda gid: 2, raising=False)
    note = w._build_directors_note(_event(), _rows(["用穴夾你雞雞嗎"]), last_human="用穴夾你雞雞嗎")
    assert "只准一個軸" not in note


# ---- 9. 姊妹互接 --------------------------------------------------------

def test_sister_pick_returns_recent_speaker(monkeypatch):
    w = _worker()
    w.tg_user_id = 1
    w.personas = {"2": {"name": "小天後"}}

    async def _spoke(_gid):
        return {1: time.time(), 2: time.time()}

    w.db = cast(Any, SimpleNamespace(group_bot_last_spoke=_spoke))
    monkeypatch.setattr(random, "random", lambda: 0.1)
    monkeypatch.setattr(random, "choice", lambda seq: list(seq)[0])
    import asyncio
    assert asyncio.run(w._sister_pick(GID)) == "小天後"


def test_sister_pick_skips_when_unlucky_or_stale(monkeypatch):
    w = _worker()
    w.tg_user_id = 1
    w.personas = {"2": {"name": "小天後"}}

    async def _old(_gid):
        return {2: time.time() - 600}

    w.db = cast(Any, SimpleNamespace(group_bot_last_spoke=_old))
    monkeypatch.setattr(random, "random", lambda: 0.1)
    import asyncio
    assert asyncio.run(w._sister_pick(GID)) == ""
    w.db = cast(Any, SimpleNamespace(group_bot_last_spoke=lambda _g: _spoke_now()))
    monkeypatch.setattr(random, "random", lambda: 0.9)
    assert asyncio.run(w._sister_pick(GID)) == ""


async def _spoke_now():
    return {2: time.time()}


def test_sister_note_is_written_into_director(monkeypatch):
    w = _worker()
    w._struct_mode_force = "short"
    note = w._build_directors_note(
        _event(), _rows(["真假"]), last_human="真假", sister_target="小天後"
    )
    assert "回姊妹 小天後" in note


# ---- 10. 已讀補位 -------------------------------------------------------

def test_ignored_group_holds_same_sender_reply():
    # 群裡九十秒沒水軍出聲：他再補一句，手上這條不砍
    assert (
        AccountWorker._interrupt_action(
            age=30.0, budget=20.0, same_sender=True, text_ready=False,
            starved=120.0, ignored=True,
        )
        == "hold"
    )
    # 沒人晾著就照舊砍（留下來一定答錯句）
    assert (
        AccountWorker._interrupt_action(
            age=30.0, budget=20.0, same_sender=True, text_ready=False,
            starved=5.0, ignored=False,
        )
        == "cancel"
    )
    # 拖太久仍要砍，補位不是無限等
    assert (
        AccountWorker._interrupt_action(
            age=100.0, budget=20.0, same_sender=True, text_ready=False,
            starved=200.0, ignored=True,
        )
        == "cancel"
    )


# ---- 11. 已讀補位：整場被晾著時不准再擋 -------------------------------

def _allowed_worker(monkeypatch, last_bot_ago):
    """組一個 _ordinary_reply_allowed 可跑的 worker：壓力低到不該觸發真空回場。"""
    import asyncio
    w = _worker()
    w.tg_user_id = 1
    w.reply_enabled = True
    spoken = {2: time.time() - last_bot_ago}
    assert asyncio.iscoroutinefunction  # noqa: F821

    async def _pressure(_gid):
        return {"human_5m": 8, "ordinary_claimed_10m": 0, "ordinary_claimed_20s": 1}

    monkeypatch.setattr(w, "_interaction_pressure", _pressure, raising=False)
    w.db = cast(Any, SimpleNamespace(
        group_bot_last_spoke=lambda _g: _spoke_map(spoken),
        group_traffic_window=lambda _g, _s: _traffic(),
    ))
    monkeypatch.setattr(w, "_voice_share", lambda *a: 0.1, raising=False)
    monkeypatch.setattr(w, "_active_owner", lambda *a: 0, raising=False)
    monkeypatch.setattr(w, "_get_runtime_float", _runtime, raising=False)
    return w


async def _spoke_map(d):
    return dict(d)


async def _traffic():
    return {"human": 10, "bots": 2, "managed": set()}


async def _runtime(_k, default):
    return default


def test_ignored_group_skips_the_pile_lock(monkeypatch):
    """群裡兩分鐘沒水軍出聲：20 秒群級鎖（ordinary_claimed_20s=1）不能再擋。"""
    import asyncio
    w = _allowed_worker(monkeypatch, last_bot_ago=200.0)
    ev = _event()
    ev.mentioned = False
    ev.is_reply = False
    monkeypatch.setattr(w, "_reply_claim_key", lambda e: (GID, 5), raising=False)
    monkeypatch.setattr(w, "_is_meaningful_human_message", lambda e: True, raising=False)
    assert asyncio.run(w._ordinary_reply_allowed(ev)) is True
    assert w.stats.get("ignored_fill_pass") == 1


def test_fresh_group_keeps_the_pile_lock(monkeypatch):
    """水軍二十秒前才講過：照舊只准一隻，不准因為補位全擠上來。"""
    import asyncio
    w = _allowed_worker(monkeypatch, last_bot_ago=20.0)
    ev = _event()
    ev.mentioned = False
    ev.is_reply = False
    monkeypatch.setattr(w, "_reply_claim_key", lambda e: (GID, 5), raising=False)
    monkeypatch.setattr(w, "_is_meaningful_human_message", lambda e: True, raising=False)
    assert asyncio.run(w._ordinary_reply_allowed(ev)) is False
    assert "ignored_fill_pass" not in w.stats
