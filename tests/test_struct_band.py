"""句型模式＝同時是長度軸（2026-11-07 16:44~17:43 桃花源實測破口）。

破口：那一小時 14 則水軍平均 31 字、最短 20 字，8~16 字的一則都沒有，12 則
還是「剛在做什麼＋撩一句」的開場。4be200c 已經抽了 35% 短碎模式，但字數寫死
在導演指令第一行的 15~45、生成端又是固定 6~50 加硬上限 90，模式只換內容不換
長度，短碎永遠被拉回 30 字的長句模子。

修法：模式自己帶字數帶（_STRUCT_BANDS），導演指令、常駐生成指令、硬上限三處
都吃同一組數字；短碎／色誘另外明講「長度優先於心境」，免得心境的生活場景又把
前情塞回去。
"""

import random
import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker, _STRUCT_BANDS

GID = -1001


def _worker(name: str = "Chen") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 28}
    return w


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=GID, sender_id=1, message_id=1, text="", raw_text="",
        media=None, is_reply=False, sender=None, reply_to=None, message=None,
    )


def _note_at(roll: float) -> tuple[AccountWorker, str]:
    w = _worker()
    old = random.random
    random.random = lambda: roll
    try:
        note = w._build_directors_note(_event(), history=[], own_texts=[], last_human="")
    finally:
        random.random = old
    return w, note


def test_bands_are_three_distinct_widths():
    assert _STRUCT_BANDS["short"][1] < _STRUCT_BANDS["dirty"][1] < _STRUCT_BANDS["scene"][1]
    assert _STRUCT_BANDS["short"] == (8, 16)


def test_short_roll_puts_short_band_in_director_note():
    w, note = _note_at(0.0)
    assert w._struct_mode == "short"
    # 沒有真人語料時仍走固定帶；短碎這則抽到窄帶，天花板就是 8+6
    assert "8~14 字內" in note
    assert "15~45" not in note


def test_short_band_ceiling_follows_human_median():
    """真人只講 8 字時，提示詞的天花板要跟著壓下來，不能還給 16。"""
    w = _worker()
    old = random.random
    random.random = lambda: 0.2          # 短碎＋窄帶，天花板只剩真人倍率
    try:
        history = [{"role": "user", "content": "字" * n} for n in (6, 7, 7, 8, 8, 9)]
        note = w._build_directors_note(_event(), history=history, own_texts=[], last_human="")
    finally:
        random.random = old
    assert w._struct_mode == "short"
    assert "8~16" not in note
    assert "11 字是硬上限" in note


def test_dirty_roll_uses_mid_band():
    w, note = _note_at(0.5)
    assert w._struct_mode == "dirty"
    assert "12~24" in note


def test_scene_roll_keeps_old_shape():
    w, note = _note_at(0.99)
    assert w._struct_mode == "scene"
    assert "15~30" in note


def test_short_and_dirty_override_mood_scene_pressure():
    """心境輪替常抽到生活瑣事，它會叫模型用場景開場；短碎／色誘要能壓過它。"""
    _, short = _note_at(0.0)
    _, dirty = _note_at(0.5)
    _, scene = _note_at(0.99)
    assert "長度優先於心境" in short
    assert "長度優先於心境" in dirty
    assert "長度優先於心境" not in scene


def test_reply_prompt_carries_mode_band():
    w = _worker()
    w._struct_mode = "short"
    prompt = w._build_user_message(_event(), history=[], char_cap=40)
    assert "8~16" in prompt
    assert "6~50 個字元" not in prompt


def test_reply_prompt_without_mode_falls_back_to_human_band():
    w = _worker()
    prompt = w._build_user_message(_event(), history=[], char_cap=40)
    assert "6~50 個字元" in prompt


def test_cap_is_clamped_for_short_mode_only():
    w = _worker()
    w._struct_mode = "short"
    assert w._struct_char_cap(90) == 20
    w._struct_mode = "dirty"
    assert w._struct_char_cap(90) == 26
    w._struct_mode = "scene"
    assert w._struct_char_cap(90) == 34
    w._struct_mode = ""
    assert w._struct_char_cap(90) == 90


def test_cap_never_raises_an_already_lower_cap():
    w = _worker()
    w._struct_mode = "short"
    assert w._struct_char_cap(14) == 14


# ---- 反向棘輪：整批縮死時不准再抽短碎 -----------------------------------
# flow_long 只在偏長時踩煞車，「沒變長」永遠不觸發，short 會一路吃掉 dirty 與
# scene（15:13–16:13 一小時 45 則全在 8~17、零長句）。

SHORT_FLOW = ["聽得我背脊發涼欸", "這數字看得我想睡", "下午犯睏，想被人按在床上", "也太衰了吧"]


def _note_with_short_flow(roll: float) -> tuple[AccountWorker, str]:
    w = _worker()
    w._recent_texts_by_group[GID] = list(SHORT_FLOW)
    w._recent_texts_at[GID] = [time.time() - 60] * len(SHORT_FLOW)
    old = random.random
    random.random = lambda: roll
    try:
        note = w._build_directors_note(_event(), history=[], own_texts=[], last_human="")
    finally:
        random.random = old
    return w, note


def test_short_flow_rebounds_off_short_band():
    w, note = _note_with_short_flow(0.1)
    assert w._struct_mode != "short"
    # roll 0.1 同時抽到尾通道，模式可能被尾巴改寫成 scene；重點是離開短碎。
    assert w._struct_mode in {"dirty", "scene"}
    assert "12~24" in note or "15~" in note


def test_short_flow_rebound_falls_back_to_scene_when_roll_high():
    w, _note = _note_with_short_flow(0.9)
    assert w._struct_mode == "scene"


def test_short_flow_rebound_is_counted():
    w, _note = _note_with_short_flow(0.1)
    assert w.stats["band_flow_short_rebound"] == 1
