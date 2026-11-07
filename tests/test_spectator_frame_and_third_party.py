"""11:30–11:37 實測破口：無對象軸修好之後，四號一起滑進同一個旁觀骨架。

11:30 小天後「站櫃整理專櫃衣服看你們吵這齣」／11:31 RICH「看你們吵成這樣我拿鐵都差點
噴出來」／11:37 Chen「喝著冰拿鐵看你們吵架」——字面全不同，起手詞軸、消耗物軸、場地軸
全部放行（拿鐵隔了六分鐘，消耗物門檻是二十分鐘但候選被判回後仍被 starvation 強制照發），
群友看到的是同一台機器。另一條：11:34–11:51 琪琪在講午餐、膝蓋，九則水軍全黏在
萬英尺與和進出口的互咬上，零則接她。

兩條都走動態判定：看本群近四則實際骨架、看近十二分鐘誰在講話，不寫死句子。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1002


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
        decision_enabled=True,
    )
    w = AccountWorker(
        account_id="w2",
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
    w.persona = {"name": name, "gender": "女", "age": 38}
    return w


def _seed(w: AccountWorker, pairs) -> None:
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


def test_spectator_frame_is_detected_only_on_the_opening_clause():
    assert AccountWorker._is_spectator_frame("站櫃整理專櫃衣服看你們吵這齣，小萬裝什麼無辜")
    assert AccountWorker._is_spectator_frame("喝著冰拿鐵看你們吵架，你褲子裡那根是有多粗")
    assert AccountWorker._is_spectator_frame("看你們吵成這樣我拿鐵都差點噴出來")
    # 「看你們」落在後半的評語不算骨架，不然正常接話全被攔
    assert not AccountWorker._is_spectator_frame("萬哥嘴這麼硬，看你們吵半天也沒結論")


def test_third_spectator_frame_in_a_row_is_vetoed():
    w = _worker()
    _seed(
        w,
        [
            (7 * 60, "站櫃整理專櫃衣服看你們吵這齣，小萬裝什麼無辜"),
            (6 * 60, "看你們吵成這樣我拿鐵都差點噴出來，萬哥到底多壞"),
            (2 * 60, "方sir管真寬，人家搶著去表現"),
        ],
    )
    veto = w._local_veto(
        "喝著冰拿鐵看你們吵架，你那根是有多粗", [], "", GID
    )
    assert "旁觀骨架" in veto


def test_two_frames_is_enough_and_a_distinct_opening_passes():
    w = _worker()
    _seed(
        w,
        [
            (7 * 60, "站櫃整理專櫃衣服看你們吵這齣，小萬裝什麼無辜"),
            (6 * 60, "看你們吵成這樣我拿鐵都差點噴出來，萬哥到底多壞"),
        ],
    )
    assert "旁觀骨架" in w._local_veto("喝著冰拿鐵看你們吵架，你那根有多粗", [], "", GID)
    assert "旁觀骨架" not in w._local_veto(
        "萬哥這嘴硬得可以，我剛在櫃上笑得手抖", [], "", GID
    )


def _rows(pairs):
    now = time.time()
    return [
        {
            "role": "user",
            "sender_name": nm,
            "content": "x",
            "timestamp": now - age,
        }
        for age, nm in pairs
    ]


def test_hot_third_party_skips_the_duel_pair():
    w = _worker()
    duel = ("萬英尺", "和進出口")
    rows = _rows(
        [
            (10, "萬英尺"),
            (20, "和進出口"),
            (30, "萬英尺"),
            (40, "琪琪"),
            (60, "琪琪"),
            (90, "琪琪"),
        ]
    )
    assert w._hot_third_party(GID, rows, duel) == "琪琪"


def test_hot_third_party_needs_three_lines_and_bot_silence():
    w = _worker()
    duel = ("萬英尺", "和進出口")
    two_lines = _rows([(10, "萬英尺"), (20, "琪琪"), (30, "琪琪")])
    assert w._hot_third_party(GID, two_lines, duel) == ""
    # 水軍二十五分鐘內點過名就不算被冷落
    w2 = _worker()
    _seed(w2, [(5 * 60, "琪琪你自己一個人吃也太慘")])
    rows = _rows([(20, "琪琪"), (30, "琪琪"), (40, "琪琪")])
    assert w2._hot_third_party(GID, rows, duel) == ""


def test_third_party_forces_the_name_into_the_candidate():
    w = _worker()
    veto = w._local_veto(
        "萬哥這嘴硬得可以，我剛在櫃上笑得手抖", [], "", GID,
        duel_names=("萬英尺", "和進出口"), third_party="琪琪",
    )
    assert "琪琪" in veto
    # 已經點到她就放行，不再重寫
    ok = w._local_veto(
        "琪琪一個人吃火鍋也太慘，下次揪我", [], "", GID,
        duel_names=("萬英尺", "和進出口"), third_party="琪琪",
    )
    assert ok == ""
