"""連續播報：RICH 20:33-20:42 連四則純評語（11-04 21 點檔實測）。

20:33「不痛是麻藥還沒退吧」20:36「被道友這樣當白痴耍還幫忙講話」
20:39「24歲沒交過女友」20:42「不洗澡那種真的不行」——四則都在講別人，
中間只有一句「醒了」勉強算身體字，六則窗一看到 20:07 的自曝就整段免檢查，
於是同一個帳號當了十分鐘場邊主播。

修法：自曝判定看連續則數與時鐘（連兩則播報、或 20 分鐘沒人講自己），
重寫候選若還是純評語就再換一條，不當成「重寫成功」發出去。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1002
NOW = time.time()

REAL = [
    "不痛是麻藥還沒退吧，明天起床就知道厲害了",
    "被道友這樣當白痴耍還幫忙講話，這朋友也是蠻好笑的",
    "24歲沒交過女友，該不會連女生手都沒牽過吧",
    "不洗澡那種真的不行，連手搖都不想跟他站同排",
]


def _worker() -> AccountWorker:
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
    w.persona = {"name": "RICH", "gender": "女", "age": 28}
    return w


def _seed(w: AccountWorker, texts, gap=90.0):
    """由舊到新排，最近一則在 gap 秒前（時間戳一定比 now 舊）。"""
    w._recent_texts_by_group[GID] = list(texts)
    w._recent_texts_at[GID] = [
        NOW - gap - (len(texts) - 1 - i) * 60 for i in range(len(texts))
    ]


def test_consecutive_press_box_counts_only_until_self_talk():
    # 新到舊數：兩則純評語 → 遇到「我剛洗完澡」就停
    w = _worker()
    _seed(w, ["我剛洗完澡皮膚還滑的"] + REAL[2:4])
    assert w._consecutive_press_box(GID) == 2


def test_third_person_body_words_do_not_count_as_self_talk():
    """「24歲沒交過女友…手都沒牽過」「不洗澡那種…手搖」都有身體字，
    但講的是那個男的，不能當成自己講過。"""
    assert AccountWorker._is_press_box(REAL[2]) is True
    assert AccountWorker._is_press_box(REAL[3]) is True


def test_two_press_box_in_a_row_forces_self_disclosure_even_if_pool_is_warm():
    """六則窗外有自曝（20:07 小天後那則），兩則純評語照樣要逼她講自己。"""
    w = _worker()
    _seed(w, ["鐵門拉下來癱在椅子上，腳底板熱到發燙"] + REAL[1:3], gap=30.0)
    note = w._sentence_type_note([], GID)
    assert "沒人講到自己" in note


def test_long_gap_without_self_talk_also_forces():
    w = _worker()
    _seed(w, REAL[:1], gap=1500.0)
    assert "沒人講到自己" in w._sentence_type_note([], GID)


def test_short_gap_after_self_talk_stays_quiet():
    w = _worker()
    _seed(w, [REAL[0], "剛洗完澡皮膚還滑的"], gap=300.0)
    assert w._sentence_type_note([], GID) == ""
