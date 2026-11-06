"""互嗆軸：兩個男人短句對打，帳號只補自己的狀態、一個名字都沒有。

2026-11-06 19:01–19:19 實測（桃花源）：

  19:01:07 萬英尺「你怎麼沒完沒了了，你這條煩人公狗」
  19:02:13 小小「喝點小酒臉超燙，他只會嘴你生什麼氣嗎」
  19:08:42 RICH「這裡老司機這麼多，不知道操作我身上的敏感帶是不是也一樣熟練」

「他」「你」都在指人，卻沒有名字，群友看不出在對誰講；兩人合計 27 則、
句子幾乎都在二十字內、沒有問句。判的是「這兩個人在對打」這個行為，
不是名字也不是關鍵詞——換一批人互嗆照樣成立。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker, _duel_pair

GID = -1001


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


def _row(name: str, text: str, age: float) -> dict:
    return {
        "role": "user",
        "sender_name": name,
        "content": text,
        "timestamp": time.time() - age,
    }


_BRAWL = [
    _row("萬英尺", "你明明知道我比較喜歡蘿莉", 20),
    _row("你說會喜歡我", "這應該是開玩笑", 18),
    _row("和進出口", "我說實話你破防什麼", 16),
    _row("萬英尺", "你怎麼沒完沒了了", 14),
    _row("和進出口", "敢不敢與我一戰", 12),
    _row("萬英尺", "你攻擊力有點差", 10),
    _row("和進出口", "都說你對幼稚園的小孩打手槍", 8),
    _row("萬英尺", "我是什麼人你應該清楚", 6),
    _row("和進出口", "你想吃多少子彈", 4),
]


# ---- 偵測 ----------------------------------------------------------------


def test_two_men_trading_short_jabs_is_a_duel():
    assert set(_duel_pair(_BRAWL)) == {"萬英尺", "和進出口"}


def test_question_heavy_thread_is_not_a_duel():
    rows = [
        _row("風聲", "所以辦完入會就可以約妹妹了？", 30 - i)
        for i in range(5)
    ] + [
        _row("你說會喜歡我", "對啊，完成之後再請助理安排？", 20 - i)
        for i in range(5)
    ]
    assert _duel_pair(rows) == ()


def test_one_person_monologue_is_not_a_duel():
    rows = [_row("萬英尺", f"第{i}句罵人的話", 30 - i) for i in range(9)]
    assert _duel_pair(rows) == ()


def test_old_messages_fall_out_of_the_window():
    rows = [_row("萬英尺", "舊帳一", 40 * 60), _row("和進出口", "舊帳二", 39 * 60)]
    assert _duel_pair(rows) == ()


def test_small_talk_in_the_middle_does_not_break_the_pair():
    """旁人插花不影響：兩人比例 0.35 就成立，不必是多數。"""
    rows = _BRAWL + [_row("玥兒", "蠻可憐的", 5), _row("琪琪", "安安啦", 3)]
    assert set(_duel_pair(rows)) == {"萬英尺", "和進出口"}


# ---- 閘門 ----------------------------------------------------------------


_UNNAMED = "喝點小酒臉超燙，他只會嘴你生什麼氣嗎"
_NAMED = "和進出口你嘴這麼兇，結果自己先破防"


def test_unnamed_line_in_a_duel_is_vetoed():
    veto = _worker()._local_veto(_UNNAMED, [], "", GID, duel_names=("萬英尺", "和進出口"))
    assert "互嗆" in veto
    assert "和進出口" in veto


def test_named_line_in_a_duel_passes():
    assert _worker()._local_veto(_NAMED, [], "", GID, duel_names=("萬英尺", "和進出口")) == ""


def test_line_with_name_in_the_tail_also_counts():
    """名字放句尾也是對著他講，只比有沒有出現，不比位置。"""
    line = "嘴這麼硬，真要開打記得叫上我啊和進出口"
    assert _worker()._local_veto(line, [], "", GID, duel_names=("萬英尺", "和進出口")) == ""


def test_no_duel_means_no_name_requirement():
    assert _worker()._local_veto(_UNNAMED, [], "", GID, duel_names=()) == ""


def test_duel_veto_gets_its_own_code():
    assert AccountWorker._veto_code("萬英尺、和進出口正在互嗆，這則沒點到名") == "duel"
