"""候選全差一點時取最高分照發，不要整條丟掉。

2026-11-06 17:42–18:42 實測：claimed 94／sent 7，其中 gate_held 34（候選耗盡後暫緩）
比真人搶話取消的 26 還多。裁判只要回非 none 就一定 drop，sendable 0.45 的句子跟
0.05 的句子待遇一樣。ai_leak／contradict 是硬傷照樣丟，其餘取最高分放行。
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001


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
        decision_enabled=True,
        decision_gate_threshold=0.5,
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


def _run(w: AccountWorker, candidates: list[str], reviews: list[dict]) -> str:
    """把生成與裁判都換成排隊回放，跑一次候選比較。"""
    queue = list(candidates)
    verdicts = list(reviews)

    async def gen(_hint: str) -> str:
        return queue.pop(0) if queue else ""

    async def review(*_a, **_k):
        return verdicts.pop(0) if verdicts else None

    w._local_veto = lambda *_a, **_k: ""  # type: ignore[method-assign]
    w._review_candidate = review  # type: ignore[method-assign]
    return asyncio.run(
        w._pick_passing_candidate("ctx", "", "hint", gen, group_id=GID, limit=len(candidates))
    )


def test_best_near_miss_is_released_when_nothing_passes():
    w = _worker()
    out = _run(
        w,
        ["第一句差很多", "第二句只差一點"],
        [
            {"sendable": 0.3, "issue": "repeat"},
            {"sendable": 0.45, "issue": "repeat"},
        ],
    )
    assert out == "第二句只差一點"
    assert w.stats["gate_near_miss"] == 1


def test_fatal_issues_are_still_dropped():
    """系統詞外洩／事實矛盾就算 0.4 也不該照發。"""
    w = _worker()
    out = _run(
        w,
        ["task ready 已回覆", "我昨天才剛從高雄搬上來，今天在高雄店裡"],
        [
            {"sendable": 0.4, "issue": "ai_leak"},
            {"sendable": 0.4, "issue": "contradict"},
        ],
    )
    assert out == ""
    assert "gate_near_miss" not in w.stats


def test_near_miss_below_floor_is_still_dropped():
    w = _worker()
    out = _run(w, ["瞎到看不懂"], [{"sendable": 0.2, "issue": "unnatural"}])
    assert out == ""


def test_passing_candidate_wins_over_near_miss():
    w = _worker()
    out = _run(
        w,
        ["差一點的", "過門檻的"],
        [
            {"sendable": 0.45, "issue": "repeat"},
            {"sendable": 0.6, "issue": "none"},
        ],
    )
    assert out == "過門檻的"
    assert "gate_near_miss" not in w.stats
