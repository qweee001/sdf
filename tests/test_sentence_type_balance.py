"""語氣同質化（2026-11-03 21:35~21:58 桃花源實測破口）。

破口：短短 23 分鐘四則水軍發言（Chen 21:35:38／Chen 21:41:06／RICH 21:55:54／
小小 21:58:30）全部落在「疑問＋鑑定對方」同一個語域，沒有一則講自己的身體或
當下狀態。心境明明抽到不同選項，四個心境卻有三個都寫成挑釁，模型照樣只會審問
男人；而舊的連續反問偵測只看「自己上一則」、又漏掉「啊」收尾，跨號三分鐘內
撞同一語域完全抓不到。

修法：看本群最近三則的句型分布（跨帳號、看時鐘），問句 ≥2 就硬轉斷言，
整段零自曝再加一句「講自己」。判定走結構，不吃封殺詞表。
"""

import random
import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001
NOW = time.time()

# 當晚破口窗口的真實四則（末三則就是應該被判定為語氣同質的那三則）
REAL = [
    ("21:35:38", "Chen", "幫忙？先讓我看看你有幾兩重再說"),
    ("21:41:06", "Chen", "舔粉是什麼新招，弟弟你舌頭有這麼萬能嗎"),
    ("21:55:54", "RICH", "18公分不含頭？那實戰到底多長啊"),
    ("21:58:30", "小小", "出一張嘴誰不會，有種讓姊姊鑑定一下長短啊"),
]


def _worker(name: str = "小小") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 30}
    return w


def _minutes(clock: str) -> int:
    h, m, s = (int(x) for x in clock.split(":"))
    return h * 60 + m + s / 60


def _seed(w: AccountWorker, upto: int, base: float) -> None:
    w._recent_texts_by_group[GID] = [t for _, _, t in REAL[:upto]]
    w._recent_texts_at[GID] = [
        NOW - (base - _minutes(clock)) * 60 for clock, _, _ in REAL[:upto]
    ]


def _event() -> SimpleNamespace:
    return SimpleNamespace(chat_id=GID, sender_id=1, message_id=1, text="")


def test_question_detection_covers_taiwan_particles_and_emoji():
    w = _worker()
    assert w._is_question("舔粉是什麼新招，弟弟你舌頭有這麼萬能嗎")
    assert w._is_question("18公分不含頭？那實戰到底多長啊")  # 「啊」＋疑問詞
    assert w._is_question("真的？")
    assert w._is_question("你幾歲🤔")  # emoji 收尾要剝掉
    assert not w._is_question("出一張嘴誰不會，有種讓姊姊鑑定一下長短啊")
    assert not w._is_question("鱷魚皮夾克穿起來很帥啊")  # 「啊」但沒有疑問詞
    assert not w._is_question("")


def test_breach_window_fires_and_names_both_faults():
    """21:58:30 當下：近三則 2 問句、全段零自曝，兩個病灶都要點出來。"""
    w = _worker()
    _seed(w, len(REAL) - 1, base=_minutes(REAL[-1][0]))
    note = w._sentence_type_note([], GID)
    assert "不准問" in note
    assert "沒人講到自己" in note


def test_director_note_forces_assertive_syntax_when_breaching():
    """機率關掉（random→0.99）也要硬轉斷言，不能靠抽。"""
    w = _worker()
    _seed(w, len(REAL) - 1, base=_minutes(REAL[-1][0]))
    old = random.random
    random.random = lambda: 0.99
    try:
        note = w._build_directors_note(_event(), history=[], own_texts=[], last_human="")
    finally:
        random.random = old
    assert "絕對禁止" in note
    assert "不准問" in note


def test_no_note_when_window_is_assertive():
    w = _worker()
    w._recent_texts_by_group[GID] = [
        "店裡鐵門剛拉下來，腿還痠著",
        "紅酒倒好了，只穿睡袍在等",
        "腰有點軟，今晚不想自己睡",
    ]
    assert w._sentence_type_note([], GID) == ""


def test_self_disclosure_suppresses_second_note_only():
    """已經有人講自己，就只剩問句那條；不要把「講自己」重複唸。"""
    w = _worker()
    w._recent_texts_by_group[GID] = [
        "我剛洗完澡，皮膚還滑的",
        "你舌頭有這麼萬能嗎",
        "那實戰到底多長啊",
    ]
    note = w._sentence_type_note([], GID)
    assert "不准問" in note
    assert "沒人講到自己" not in note


def test_history_fallback_when_group_history_is_empty():
    """剛重啟、跨號紀錄還空的時候，用本帳號 history 也要抓得到。"""
    w = _worker()
    history = [
        {"role": "assistant", "content": "你舌頭有這麼萬能嗎"},
        {"role": "user", "content": "我舌頭很靈活"},
        {"role": "assistant", "content": "那實戰到底多長啊"},
        {"role": "user", "content": "18"},
        {"role": "assistant", "content": "幾兩重自己心裡有數嗎"},
    ]
    note = w._sentence_type_note([], GID, history)
    assert "不准問" in note


def test_single_assertive_sample_stays_quiet():
    w = _worker()
    w._recent_texts_by_group[GID] = ["店裡鐵門拉下來了，腿還痠著"]
    assert w._sentence_type_note([], GID) == ""


def test_single_question_sample_already_bans_next():
    """只有一則樣本也要能判斷：自己剛問完，下一則就不該再問。"""
    w = _worker()
    w._recent_texts_by_group[GID] = ["你舌頭有這麼萬能嗎"]
    assert "不准問" in w._sentence_type_note([], GID)


def test_falls_back_to_own_texts_without_group_history():
    w = _worker()
    own = [t for _, _, t in REAL[:3]]
    note = w._sentence_type_note(own, GID)
    assert "不准問" in note


def test_real_night_replay_only_fires_after_two_questions():
    """整段真實回放：第一則（單句、無自曝）不吵，之後每一則都要帶到至少一種修正。"""
    fired = []
    for i, (_clock, name, _text) in enumerate(REAL):
        w = _worker(name)
        _seed(w, i, base=_minutes(REAL[-1][0]))
        if w._sentence_type_note([], GID):
            fired.append(i)
    assert fired == [1, 2, 3], f"觸發點不對：{fired}"
