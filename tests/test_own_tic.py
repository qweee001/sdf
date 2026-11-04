"""同號語體慣性（_own_tic）：自稱用到爛、場景詞與上一則重疊就該被抓。

實測 2026-11-04 13:44~14:08 Chen 連續四則自稱「姊姊」，最後兩則連場景都同一個
（14:03「姊姊店裡忙完下午茶…」→ 14:08「請姊姊喝杯…在店裡摸魚」）。中間夾了 12 則
真人，群層疲勞的 8 句窗早被洗掉，所以這層只看本號自己。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.worker import AccountWorker


class _FakeDB:
    async def claim_group_text(self, *_args, **_kwargs):
        return True

    async def add_message(self, *args, **kwargs):
        pass

    async def touch_activity(self, *args):
        pass

    async def record_reply_event(self, *args, **kwargs):
        pass


def _worker() -> AccountWorker:
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
    return AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=_FakeDB(),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[-1001],
    )


def _feed(w: AccountWorker, *rows: tuple[str, float], gid: int = -1001) -> None:
    now = time.time()
    w._own_echo_by_group[gid] = [(body, now - ago) for body, ago in rows]


def test_self_ref_dominant_is_tic():
    w = _worker()
    _feed(
        w,
        ("頭腦不清晰，還是看到姊姊暈船了", 240),
        ("小妹妹才只挑烤肉吃，姊姊店裡忙完下午茶全包的", 120),
        ("先請姊姊喝杯手搖嘛", 30),
    )
    tic, _frame = w._own_tic(-1001)
    assert tic == "姊姊"
    assert "又自稱「姊姊」" in w._own_tic_hit("薪水是身外之物，先請姊姊喝一杯", -1001)


def test_single_use_is_not_tic():
    w = _worker()
    _feed(w, ("剛下班累死", 200), ("看到姊姊暈船了", 100), ("明天還要上班", 20))
    tic, _frame = w._own_tic(-1001)
    assert tic == ""


def test_expired_rows_ignored():
    w = _worker()
    _feed(w, ("姊姊店裡忙", 2000), ("姊姊先喝一杯", 1500), ("請姊姊吃", 900))
    tic, _frame = w._own_tic(-1001)
    assert tic == ""


def test_prev_scene_bigram_blocks_next_line():
    w = _worker()
    _feed(w, ("姊姊店裡忙完下午茶", 120), ("先請姊姊喝杯微冰手搖嘛在店裡摸魚", 30))
    # 群裡大家都用「先請」「一杯」，只有「店裡」「手搖」算她的場景詞
    w._recent_texts_by_group[-1001] = ["先請你喝一杯", "這杯先請", "一杯不夠", "先請再說"] * 5
    tic, frame = w._own_tic(-1001)
    assert tic == "姊姊"
    assert frame[0] in ("店裡", "手搖", "摸魚", "微冰")
    assert w._own_tic_hit("下午在櫃上跟客人吵架", -1001) == ""


def test_frame_excludes_function_words():
    w = _worker()
    _feed(w, ("我們跟他都在店裡", 20))
    _tic, frame = w._own_tic(-1001)
    assert "我們" not in frame
    assert "跟他" not in frame


def test_note_lists_tic_and_scene():
    w = _worker()
    _feed(w, ("姊姊店裡忙", 100), ("姊姊手搖喝完了", 40))
    note = w._own_tic_note(-1001)
    assert "自稱「姊姊」" in note
    assert "上一則剛用過" in note


def test_no_history_no_note():
    w = _worker()
    assert w._own_tic_note(-1001) == ""
    assert w._own_tic_hit("隨便一句", -1001) == ""


def test_groups_are_isolated():
    w = _worker()
    _feed(w, ("姊姊店裡忙", 100), ("姊姊手搖喝完", 40), ("姊姊想睡", 20), gid=-1001)
    assert w._own_tic(-2002) == ("", [])


@pytest.mark.asyncio
async def test_record_sent_text_feeds_tic():
    w = _worker()
    for body in ("先讓姊姊看看", "姊姊店裡剛忙完", "請姊姊喝一杯"):
        w._record_sent_text(-1001, body)
    tic, _frame = w._own_tic(-1001)
    assert tic == "姊姊"


def test_hot_char_core_catches_topic_avalanche():
    """15:00 前後四則水軍全在腳背／腳趾／腳底／腳香，雙字各不同但真人看到的是「又是腳」。"""
    w = _worker()
    gid = -1001
    pool = ["今天天氣真好", "下班要回家了", "這部片好看嗎", "捷運人好多", "晚餐吃什麼",
            "明天還要上班", "冷氣壞掉了", "剛睡醒", "衣服還沒洗", "想喝飲料"]
    w._recent_texts_by_group[gid] = [pool[i % len(pool)] for i in range(40)] + [
        "腳背蠻漂亮的，很適合架在肩膀上",
        "腳趾夾住慢慢磨，撐不到一分鐘",
        "你腳香是香，但你們也太誇張",
        "琪琪做紅色美甲喔，等等被咬腳的時候剛好配",
        "真人插一句今天好熱",
        "這雙鞋哪裡買的",
        "真想去捏捏看",
        "我今天穿涼鞋耶",
        "妹妹的腿真好看",
        "腳底按摩一下很舒服",
        "你說呢",
        "看起來很不錯",
    ]
    assert "腳" in w._hot_char_core(gid)
    assert "腳" in w._phrase_fatigue_hint(gid)


def test_hot_char_core_skips_grammar_chars():
    w = _worker()
    gid = -1001
    # 每句都有「的」和「你」，但那是文法不是主題
    w._recent_texts_by_group[gid] = [f"你的第{i}句都很普通" for i in range(30)]
    hot = w._hot_char_core(gid)
    assert "的" not in hot and "你" not in hot


def test_hot_char_core_needs_enough_texts():
    w = _worker()
    w._recent_texts_by_group[-1001] = ["腳背好漂亮", "腳趾很癢"]
    assert w._hot_char_core(-1001) == []
