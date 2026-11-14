"""句型模式＝同時是長度軸（2026-11-07 16:44~17:43 桃花源實測破口）。

破口：那一小時 14 則水軍平均 31 字、最短 20 字，8~16 字的一則都沒有，12 則
還是「剛在做什麼＋撩一句」的開場。4be200c 已經抽了 35% 短碎模式，但字數寫死
在導演指令第一行的 15~45、生成端又是固定 6~50 加硬上限 90，模式只換內容不換
長度，短碎永遠被拉回 30 字的長句模子。

修法：模式自己帶字數帶（_STRUCT_BANDS），導演指令、常駐生成指令、硬上限三處
都吃同一組數字；短碎／色誘另外明講「長度優先於心境」，免得心境的生活場景又把
前情塞回去。

之後又長了碎字（frag）、留白（halt）、露骨節拍（beat）、長鏡頭（long）幾條
通道，骰子從一顆變成好幾顆（模式 roll → 長鏡頭 roll → 碎字 roll → 窄帶／尾巴），
把 random.random 釘成單一常數已經控不住走哪條，也讓「roll 0.2 就是 short」這種
假設悄悄失效。這裡改成腳本化骰子（_rolls 依序發牌、發完回 default），每支測試
自己指定模式 roll 與長鏡頭 roll，驗的才是它宣稱要驗的那件事。
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


def _rolls(*values: float, default: float = 0.5):
    """依序發牌的 random.random 替身；發完回 default（不再全場同一個值）。

    發牌順序就是 _build_directors_note 的抽取順序：模式 roll → 長鏡頭 roll →
    碎字 roll → 窄帶 roll → 尾巴 roll → 尾巴模式 roll → 留白 roll。default 0.5
    讓沒指定的後段骰子全部「不中」。
    """
    seq = list(values)

    def _roll() -> float:
        return seq.pop(0) if seq else default

    return _roll


def _note(w: AccountWorker, rolls, *, history=None) -> str:
    old = random.random
    random.random = rolls
    try:
        return w._build_directors_note(
            _event(), history=history or [], own_texts=[], last_human=""
        )
    finally:
        random.random = old


def _note_at(*values: float, history=None) -> tuple[AccountWorker, str]:
    w = _worker()
    return w, _note(w, _rolls(*values), history=history)


# 冷場（沒人講話）的模式 roll 對照：<0.26 short、<0.56 dirty、<0.84 scene、其餘 beat。
# 長鏡頭 roll 給 0.99＝這一則不抽長鏡頭，才驗得到模式本身。
SHORT_ROLL, DIRTY_ROLL, SCENE_ROLL, NO_LONG = 0.2, 0.5, 0.99, 0.99


def test_bands_are_distinct_and_cover_every_mode():
    """每個模式都要有帶，短的比長的窄；少一個 key 會在 _STRUCT_BANDS[mode] 直接 KeyError。"""
    assert set(_STRUCT_BANDS) == {"frag", "short", "dirty", "scene", "beat", "long", "halt"}
    # halt／frag 都是兩三字的反應，天花板可以比 short 更低；再上去要嚴格遞增
    assert _STRUCT_BANDS["frag"][1] < _STRUCT_BANDS["short"][1]
    assert _STRUCT_BANDS["halt"][0] < _STRUCT_BANDS["short"][0]
    assert _STRUCT_BANDS["short"][1] <= _STRUCT_BANDS["dirty"][1] < _STRUCT_BANDS["scene"][1]
    assert _STRUCT_BANDS["scene"][1] < _STRUCT_BANDS["beat"][1]
    assert _STRUCT_BANDS["long"][1] <= _STRUCT_BANDS["beat"][1]
    assert _STRUCT_BANDS["short"] == (8, 16)


def test_frag_roll_puts_frag_band_in_director_note():
    # 模式 roll 0.0 → 先落短碎；不抽長鏡頭；碎字 roll 0.0 → 改走碎字通道
    w, note = _note_at(0.0, NO_LONG, 0.0)
    assert w._struct_mode == "frag"
    assert "2~6 字" in note
    assert "15~45" not in note


def test_short_band_stays_when_frag_roll_misses():
    w: AccountWorker = _worker()
    # 發牌：模式 roll → 長鏡頭 roll → 碎字 roll → 留白 roll → 窄帶 roll（抽中）
    note = _note(w, _rolls(SHORT_ROLL, NO_LONG, 0.99, 0.99, 0.2))
    assert w._struct_mode == "short"
    # 窄帶抽中：天花板是帶底 +6，沒有真人語料時就是 14
    assert "8~14 字內" in note


def test_short_band_ceiling_follows_human_median():
    """真人只講 8 字時，提示詞的天花板要跟著壓下來，不能還給 16。"""
    w = _worker()
    history = [{"role": "user", "content": "字" * n} for n in (6, 7, 7, 8, 8, 9)]
    note = _note(w, _rolls(SHORT_ROLL, NO_LONG, 0.99, 0.99, 0.2), history=history)
    assert w._struct_mode == "short"
    assert "8~16 字內" not in note
    # 窄帶上限 14 再被真人中位的 1.25 倍壓下去，提示詞把壓後的天花板寫成硬上限
    cap = w._struct_char_cap(90)
    assert cap < 16
    assert f"{cap} 字是硬上限" in note


def test_dirty_roll_uses_mid_band():
    w, note = _note_at(DIRTY_ROLL, NO_LONG)
    assert w._struct_mode == "dirty"
    assert "8~16 字內" in note


def test_high_roll_beat_drops_back_to_scene():
    # 極高 roll 先落 beat，冷場而且沒在撩時再降回 scene（beat 在 0 檔太露骨）
    w, note = _note_at(SCENE_ROLL, NO_LONG)
    assert w._struct_mode == "scene"
    assert "10~20 字內" in note


def test_short_and_dirty_override_mood_scene_pressure():
    """心境輪替常抽到生活瑣事，它會叫模型用場景開場；短碎／色誘要能壓過它。"""
    _, short = _note_at(SHORT_ROLL, NO_LONG)
    _, dirty = _note_at(DIRTY_ROLL, NO_LONG)
    _, scene = _note_at(SCENE_ROLL, NO_LONG)
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
    assert w._struct_char_cap(90) == 20          # 帶頂 16 + 4 緩衝
    w._struct_mode = "dirty"
    assert w._struct_char_cap(90) == 18          # 帶頂 16 + 2
    w._struct_mode = "scene"
    assert w._struct_char_cap(90) == 24          # 帶頂 20 + 4
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


def _note_with_short_flow(*values: float) -> tuple[AccountWorker, str]:
    w = _worker()
    w._recent_texts_by_group[GID] = list(SHORT_FLOW)
    w._recent_texts_at[GID] = [time.time() - 60] * len(SHORT_FLOW)
    return w, _note(w, _rolls(*values))


def test_short_flow_rebounds_off_short_band():
    # 模式 roll 0.1 → short；不抽長鏡頭；碎字不中 → 棘輪把 short 頂去 dirty
    w, note = _note_with_short_flow(0.1, NO_LONG, 0.99)
    assert w._struct_mode != "short"
    assert w._struct_mode in {"dirty", "scene"}
    assert "8~16 字內" in note or "10~20 字內" in note


def test_short_flow_rebound_can_still_take_the_tail_channel():
    """反彈成 dirty 之後尾巴通道仍能把它推去 scene：棘輪不是死路。"""
    # 反彈在留白／窄帶之前就把 short 換掉，所以第 4 顆骰子就是尾巴 roll
    w, _note_text = _note_with_short_flow(0.1, NO_LONG, 0.99, 0.0, 0.0)
    assert w._struct_mode == "scene"


def test_short_flow_rebound_is_counted():
    w, _note_text = _note_with_short_flow(0.1, NO_LONG, 0.99)
    assert w.stats["band_flow_short_rebound"] == 1
