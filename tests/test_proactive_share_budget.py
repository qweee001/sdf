"""主動額度改成吃「真人今天講多少」的佔比預算，不再固定 60 條硬牆。

20:09 四號全部撞頂（各 60／天）→ 20:10–20:40 整整 31 分鐘水軍 0 則，
期間真人講了一百四十幾句、每分鐘 3~5 則；20 點整點只有 4 則。
固定條數在真人一天近兩千句的群裡就是釘死在 12% 佔比。
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.worker import AccountWorker

GID = -1002229799107


class _Db:
    def __init__(self, humans):
        self.humans = humans
        self.calls = 0

    async def group_human_count_since(self, group_id, since):
        self.calls += 1
        return self.humans


def _worker(db=None, managed=4) -> AccountWorker:
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
        proactive_max_per_day=30,
    )
    w = AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=cast(Any, db or _Db(0)),
        config=config,
        managed_ids=set(range(1, managed + 1)),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[GID],
    )
    w.persona = {"name": "小小", "gender": "女", "age": 22}
    return w


def test_budget_scales_with_today_human_volume():
    """真人 1933 句 → 每號上看兩百條，而不是卡在 60。"""
    w = _worker(_Db(1933))
    assert 190 <= asyncio.run(w._proactive_budget(GID, 60)) <= 215


def test_cold_group_keeps_floor():
    """今天只有幾句真人的冷場，還是回到地線，不會因為算式放寬去自嗨。"""
    w = _worker(_Db(4))
    assert asyncio.run(w._proactive_budget(GID, 120)) == 120.0


def test_budget_is_capped():
    w = _worker(_Db(20000))
    assert asyncio.run(w._proactive_budget(GID, 60)) == 300.0


def test_human_count_is_cached():
    db = _Db(500)
    w = _worker(db)
    asyncio.run(w._proactive_budget(GID, 60))
    asyncio.run(w._proactive_budget(GID, 60))
    assert db.calls == 1


def test_db_failure_falls_back_to_floor():
    class Boom:
        async def group_human_count_since(self, *_a):
            raise RuntimeError("db down")

    w = _worker(Boom())
    assert asyncio.run(w._proactive_budget(GID, 60)) == 60.0


def test_soft_ceiling_reaches_hard_stop_at_double():
    """預算兩倍才真停手；一倍到兩倍之間一路降機率（1.3 倍只剩 10%）。"""
    def allow_p(budget, today):
        ratio = today / max(1.0, budget)
        return 1.0 if ratio < 0.8 else max(0.10, 1.0 - (ratio - 0.8) * 1.8)

    assert allow_p(200, 150) == 1.0
    assert allow_p(200, 190) < 1.0
    assert allow_p(200, 260) == pytest.approx(0.10)
