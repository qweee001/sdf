"""跨號同對象／同梗秒撞（14:23:04 / 14:23:09 實測）。

Chen 與小小五秒內都對同一個「和進出口」講「煮飯→上床」：
「不穿日料改把我剝光在流理台吃個夠」／「煮給我吃再床上見真章」。
兩句幾乎沒有共用詞，撞詞軸與 `_burst_echo`（只比本號前一個氣泡）都放行，
群友看到的是兩個小姐搶同一個男人講同一件事。

判的是語料不要名單：候選前八字出現過的實詞三連，一分鐘內別的帳號也講過就算。
後到的那則剪掉前情（剪不動才截短），先講的照原文發。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1003
NOW = time.time()


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
        media_max_input_bytes=1024,
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
    w.persona = {"name": "小小", "gender": "女", "age": 22}
    return w


def test_same_target_and_joke_within_a_minute_is_a_burst():
    w = _worker()
    w._recent_texts_by_group[GID] = ["和進出口，不穿日料改把我剝光在流理台吃個夠"]
    w._recent_texts_at[GID] = [NOW - 5]
    assert w._cross_target_burst("和進出口，煮給我吃再床上見真章", GID) == "和進出口"


def test_other_target_is_not_a_burst():
    w = _worker()
    w._recent_texts_by_group[GID] = ["方sir，滷味配啤酒很解"]
    w._recent_texts_at[GID] = [NOW - 5]
    assert w._cross_target_burst("和進出口，煮給我吃再床上見真章", GID) == ""


def test_stale_or_missing_flow_is_not_a_burst():
    w = _worker()
    w._recent_texts_by_group[GID] = ["和進出口，不穿日料改把我剝光在流理台吃個够"]
    w._recent_texts_at[GID] = [NOW - 180]
    assert w._cross_target_burst("和進出口，煮給我吃再床上見真章", GID) == ""
    w._recent_texts_by_group[GID] = []
    assert w._cross_target_burst("和進出口，煮給我吃再床上見真章", GID) == ""
