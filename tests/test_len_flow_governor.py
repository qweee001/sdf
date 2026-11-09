"""字數帶失守時，靠「自己最近送出去多長」自己踩煞車。

10:05 全面取消硬剪後，10:05–10:15 八則實測 11~30 字、中位 18.5，8~16 帶歸零。
這裡不剪字，改成看本群水軍最近幾則的實測長度：連續偏長就把句型模式壓回短碎。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001


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
    w.persona = {"name": "小小", "gender": "女", "age": 22}
    return w


def _seed(w: AccountWorker, texts: list[str], age_s: float = 60.0) -> None:
    w._recent_texts_by_group[GID] = list(texts)
    w._recent_texts_at[GID] = [time.time() - age_s] * len(texts)


def test_long_flow_is_detected():
    w = _worker()
    _seed(
        w,
        [
            "啃完飯糰手指還油黏黏的，看人逛賣場腿好美我也想翹班去吹冷氣了",
            "你說會喜歡我，用嘴巴攻擊不如用大雞雞撞進來吧",
            "邊喝冰美式邊等開櫃，光說理解也沒看你行動",
            "華發，養我可很貴，你雞雞先夠硬再說😎",
        ],
    )
    assert w._recent_len_flow_long(GID)


def test_short_flow_does_not_trigger():
    w = _worker()
    _seed(w, ["早安", "想喝手搖摸魚了", "誰來幫姊姊揉一下", "這麼急是要趕著秒射嗎🤣"])
    assert not w._recent_len_flow_long(GID)


def test_too_few_samples_does_not_trigger():
    w = _worker()
    _seed(w, ["啃完飯糰手指還油黏黏的，看人逛賣場腿好美我也想翹班去吹冷氣了"])
    assert not w._recent_len_flow_long(GID)


def test_stale_texts_outside_window_do_not_trigger():
    w = _worker()
    _seed(w, ["啃完飯糰手指還油黏黏的，看人逛賣場腿好美我也想翹班去吹冷氣了"] * 4, age_s=45 * 60)
    assert not w._recent_len_flow_long(GID)


def test_missing_history_is_safe():
    w = _worker()
    assert not w._recent_len_flow_long(GID)
