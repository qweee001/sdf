"""場地軸：三則連著都「我在某個地方＋手上的小事」開場，場所字串一個都不重。

2026-11-06 17:10–17:26 實測（桃花源）：

  17:10 小小「切下來太浪費…坐在辦公椅上腿都夾緊在等下班」
  17:21 Chen「我正靠著店裡櫃檯放空揉太陽穴，弟弟你這麼會讓人飛起來」
  17:26 小小「方sir連榴槤味都敢舌吻，我剛在茶水間泡咖啡差點嗆到」

辦公椅／櫃檯／茶水間是三個字串，逐字軸、動作軸、吃喝軸全都中不到，
但群友看到的是同一個骨架。場所本身不是禁用（小天後本來就是店員），
撞的是「開場第一個字就報地點」這個格子，所以只比前十四字。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker, _place_opening

GID = -1001

_DESK = "切下來太浪費，哥哥不如留著今晚塞進來讓我爽一下，坐在辦公椅上腿都夾緊在等下班"
_COUNTER = "我正靠著店裡櫃檯放空揉太陽穴，弟弟你這麼會讓人飛起來"
_PANTRY = "方sir連榴槤味都敢舌吻，我剛在茶水間泡咖啡差點嗆到"


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
    return w


def _seed(w: AccountWorker, pairs) -> None:
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


# ---- 只看開頭 ------------------------------------------------------------


def test_place_in_the_head_is_detected():
    assert _place_opening(_DESK) == "辦公椅"
    assert _place_opening(_COUNTER) == "店裡"
    assert _place_opening(_PANTRY) == "茶水間"


def test_place_in_the_tail_is_not_an_opening():
    """「拿去茶水間」是被拿去的地方，不算她又報一次自己在哪。"""
    assert _place_opening("舌頭伸過來吻到我腿軟再說，等等拿去茶水間") == ""
    assert _place_opening("你們連貓咪的位子都不放過") == ""


def test_place_mid_sentence_counts_when_it_is_the_first_clause():
    """17:10 那句場所在第三個逗號後面，卻是她整句的落點，不能只看前幾個字。"""
    assert _place_opening("切下來太浪費，哥哥不如留著今晚塞進來讓我爽一下，坐在辦公椅上腿都夾緊") == "辦公椅"


# ---- 閘門 ----------------------------------------------------------------


_SHOP = "剛在鐵門邊跟客人拉完貨，帳單還沒對完"
_SEAT = "坐回位子上，帳單還沒對完"


def test_second_place_opening_in_twenty_minutes_is_vetoed():
    w = _worker("Chen")
    _seed(w, [(11 * 60, _SHOP)])
    assert "場地" in w._local_veto(_COUNTER, [], "", GID)


def test_third_place_opening_without_adjacency_is_vetoed():
    """17:26：前兩則在十六分鐘前，前一句貼身窗過了也要接得住。"""
    w = _worker("小小")
    _seed(w, [(16 * 60, _SHOP), (5 * 60, _SEAT)])
    assert "場地" in w._local_veto(_PANTRY, [], "", GID)


def test_single_old_place_does_not_block():
    w = _worker()
    _seed(w, [(70 * 60, _DESK)])
    assert "場地" not in w._local_veto(_COUNTER, [], "", GID)


def test_line_without_place_opening_passes_even_if_others_had_one():
    w = _worker()
    _seed(w, [(3 * 60, _DESK), (9 * 60, _COUNTER)])
    line = "舌頭伸過來吻到腿軟，剩下的交給你了"
    assert _place_opening(line) == ""


def test_place_axis_yields_to_consume_reason():
    """吃喝軸先命中就先回，兩軸同時成立時理由仍看得懂是哪一軸。"""
    w = _worker("Chen")
    _seed(w, [(6 * 60, _DESK), (15 * 60, "中午買的便當我才吃兩口就不想吃了")])
    line = "剛在店裡整理新衣服，簡單吃一點就被嫌很一般"
    veto = w._local_veto(line, [], "", GID)
    assert "喝" in veto


# ---- 統計代碼 ------------------------------------------------------------


def test_veto_reason_gets_its_own_code():
    assert AccountWorker._veto_code("最近幾則水軍都從場地開頭（這則是「櫃檯」）") == "place"
    assert AccountWorker._veto_code("開頭直接搬真人那句的「剛下班」，換個角度起頭") == "opening_echo"
    assert AccountWorker._veto_code("「揉腳」自己九十分鐘內做過了，這一則換一個身體動作") == "own_scene_mid"
    assert AccountWorker._veto_code("最近幾則才有人在喝／吃同一類東西") == "consume"
