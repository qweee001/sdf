"""被切斷的半句：14:18:36 RICH 只送出「變態才」三個字。

群友上一句是「兄弟我哪裡變態了」，回三個字就斷在連接詞上，一眼就是機器截斷。
句子不可能停在「才／就／都／又／也／還／卻／而／但／跟／和／與／讓／把／被／從／比」，
停在這些字上就退回重寫。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001


def _worker(name: str = "RICH") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 28}
    return w


def test_half_sentence_is_dangling():
    assert AccountWorker._is_dangling("變態才")
    assert AccountWorker._is_dangling("我剛在")
    assert AccountWorker._is_dangling("你這樣跟")


def test_finished_short_line_is_not_dangling():
    assert not AccountWorker._is_dangling("真假啦")
    assert not AccountWorker._is_dangling("超頂")
    assert not AccountWorker._is_dangling("這種我最愛")


def test_trailing_comma_before_the_cut_counts_once():
    assert AccountWorker._is_dangling("變態才，")


def test_veto_names_the_dangling_char():
    w = _worker()
    reason = w._local_veto("變態才", [], "", GID)
    assert "斷在" in reason
    assert w._veto_code(reason) == "dangle"


def test_complete_line_passes_the_veto():
    w = _worker()
    now = time.time()
    w._recent_texts_by_group[GID] = []
    w._recent_texts_at[GID] = [now]
    assert "斷在" not in w._local_veto("變態才有人愛啊", [], "", GID)
