"""碎字通道、點名/前情比例軸、發送前新鮮度的活鎖解除。

17:07–19:19 實測：50 則水軍最短 8 字、零則 <8（真人四成是三字以內的反應）；
點名開場 17 則＝34%（真人 8%，比修前 19% 還高，因為「引述軸」寫成 elif 把它遮掉）；
17:31–17:53 二十二分鐘零輸出、期間真人 107 句——生成期間累積四句就被
_context_still_fresh 丟掉，claim 已放、沒有人接回去，形成活鎖。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import load_settings
from app.worker import AccountWorker


def _worker() -> AccountWorker:
    config = load_settings()
    config.min_typing_delay = 0.0
    config.max_typing_delay = 0.0
    return AccountWorker(
        account_id="101",
        session_key="session",
        tg_api_id=1,
        tg_api_hash="hash",
        ai_client=SimpleNamespace(),
        db=SimpleNamespace(),
        config=config,
        managed_ids={101},
        active_ids={101},
        managed_origins={},
        human_owners={},
        recent_proactive_owners={},
        last_human_activity={},
        on_status_change=None,
    )


# ---------- 碎字通道 ----------


def test_frag_band_has_hard_six_cap():
    worker = _worker()
    worker._struct_mode = "frag"
    worker._human_ref = 8
    worker._short_tight = False
    worker._len_tail = False
    # 6 字是硬頂：範圍給寬模型就寫滿，短碎永遠出不來
    assert worker._struct_char_cap(6) == 6
    # 真人中位再低也不會把 6 抬上去
    worker._human_ref = 20
    assert worker._struct_char_cap(6) == 6
    # 其他模式不受影響
    worker._struct_mode = "short"
    assert worker._struct_char_cap(30) == 20


def test_frag_syntax_carries_example_reactions():
    from app.worker import _STRUCT_SYNTAX

    assert "真假" in _STRUCT_SYNTAX["frag"]
    assert "不准逗號" in _STRUCT_SYNTAX["frag"]


# ---------- 點名軸判比例 ----------


def test_name_open_dense_needs_rate_not_adjacent():
    worker = _worker()
    worker._speaker_names = lambda gid: ["戰豆", "鈴鐺", "規範"]

    def run(texts):
        worker._cross_recent_texts = lambda gid, seconds=1800: texts[-10:]
        return worker._name_open_dense(1)

    # 五則裡兩則點名＝40%，近三則只有一則是常態，仍要判中
    assert run(["戰豆這婚別結了", "我剛洗完澡", "鈴鐺嘴巴真毒", "誰要跟他", "規範別看戲"])
    # 一則都沒有
    assert not run(["我剛洗完澡", "這鍋好香", "換我吃", "你先去", "吃飽再說"])
    # 只有兩則、湊不到比例 → 不亂開
    assert not run(["戰豆這婚別結了", "鈴鐺嘴巴真毒"])


def test_preamble_streak_counts_gang_opens():
    worker = _worker()
    texts = ["剛洗完澡", "剛下班累死", "才剛吃飽", "這鍋好香", "換我"]
    worker._cross_recent_texts = lambda gid, seconds=1500: texts
    assert worker._preamble_streak(1) == 3
    worker._cross_recent_texts = lambda gid, seconds=1500: ["這鍋好香", "換我", "你先去"]
    assert worker._preamble_streak(1) == 0


def test_preamble_note_added_to_sentence_notes():
    worker = _worker()
    worker._speaker_names = lambda gid: []
    worker._cross_recent_texts = lambda gid, seconds=1800: ["剛洗完澡", "剛下班", "才剛吃飽"]
    worker._recent_bot_pool = lambda *a, **k: ["換你上場了吧"]
    worker._recent_texts_by_group = {}
    worker._recent_texts_at = {}
    worker._has_self_disclosure = lambda t: True
    worker._consecutive_press_box = lambda gid: 0
    worker._broadcast_hits = lambda gid: 0
    worker._dirty_judge_hits = lambda *a, **k: 0
    worker._quote_assert_hits = lambda gid: 0
    worker._is_preamble_beat = lambda t, gid: False
    note = worker._sentence_type_note([], 1, [])
    assert "「剛」拿掉" in note


# ---------- 新鮮度活鎖 ----------


def _fresh_worker(newer: list[str], human_per_min: int, starved: float):
    worker = _worker()
    rows = [
        {"role": "user", "content": c, "timestamp": time.time(), "sender_id": 9}
        for c in newer
    ]
    worker.db = SimpleNamespace(
        get_group_messages=AsyncMock(return_value=rows),
    )
    worker._recent_human_ts_by_group = {
        1: [time.time() - i for i in range(human_per_min)],
    }

    async def _starved(_gid):
        return starved

    worker._seconds_since_any_bot = _starved
    event = SimpleNamespace(
        chat_id=1,
        raw_text="這鍋好香",
        _sdf_seen_at=time.time() - 30,
        _sdf_target_sender="",
    )
    worker._is_directed_at_me = AsyncMock(return_value=False)
    return worker, event


def test_freshness_keeps_reply_when_group_starved():
    # 二十二分鐘沒人講話：已經五句新話、毫無共同字串，還是要送
    worker, event = _fresh_worker(
        ["完全不同一"] * 5, human_per_min=1, starved=1300.0
    )
    assert asyncio.run(worker._context_still_fresh(event)) is True


def test_freshness_threshold_follows_rate():
    newer = ["完全不同一", "完全不同二", "完全不同三", "完全不同四"]
    # 一分鐘五句的場，生成二十秒必然超過三句 → 四句仍放行
    worker, event = _fresh_worker(newer, human_per_min=5, starved=60.0)
    assert asyncio.run(worker._context_still_fresh(event)) is True
    # 冷場（一分鐘一句）四句就是真的過時
    worker, event = _fresh_worker(newer, human_per_min=1, starved=60.0)
    assert asyncio.run(worker._context_still_fresh(event)) is False
