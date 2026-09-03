"""反重复 P1-1：群内长时间无真人时，主动话题降频/暂停。

规则（按用户定案「群是给真人看的，真人不在就别自嗨」）：
- <6h 有真人活动：正常节奏（现有逻辑不变）
- 6-24h 无真人：每账号每天最多 2 条主动话题（大幅降频）
- >24h 无真人：完全暂停主动话题
- 从未有过真人活动（last_human_activity 空）：按 24h 档处理（保守暂停）
"""

import asyncio
import time

from test_worker_reply_arbitration import _ClaimDB, _worker


def _ts(hours_ago: float) -> float:
    return time.time() - hours_ago * 3600


def test_proactive_allowed_when_human_recent():
    async def main():
        worker = _worker(101, db=_ClaimDB(), last_human_activity={-100: _ts(0.5)})
        assert worker._proactive_rate_limit_ok(-100) == "normal"
        assert worker._proactive_rate_limit_ok(-100, hours_since_human=1.0) == "normal"

    asyncio.run(main())


def test_proactive_reduced_when_human_absent_6_to_24h():
    async def main():
        worker = _worker(101, db=_ClaimDB(), last_human_activity={-100: _ts(8)})
        assert worker._proactive_rate_limit_ok(-100) == "reduced"

    asyncio.run(main())


def test_proactive_paused_when_human_absent_over_24h():
    async def main():
        worker = _worker(101, db=_ClaimDB(), last_human_activity={-100: _ts(30)})
        assert worker._proactive_rate_limit_ok(-100) == "paused"

    asyncio.run(main())


def test_proactive_paused_when_never_any_human():
    """从未见过真人 → fail-closed 按 paused 处理，不自嗨。"""
    async def main():
        worker = _worker(101, db=_ClaimDB(), last_human_activity={})
        assert worker._proactive_rate_limit_ok(-100) == "paused"

    asyncio.run(main())


def test_proactive_loop_respects_pause():
    """>24h 无真人时 _proactive_loop 直接跳过发送。"""
    async def main():
        worker = _worker(202, db=_ClaimDB(), last_human_activity={-5428680940: _ts(48)})
        assert worker._proactive_rate_limit_ok(-5428680940) == "paused"
        # 应被 gate 挡下
        assert worker._proactive_gate_blocks(-5428680940) is True

    asyncio.run(main())


def test_proactive_reduced_cap_two_per_day():
    """6-24h 无真人：当天主动话题最多 2 条。"""
    async def main():
        worker = _worker(303, db=_ClaimDB(), last_human_activity={-100: _ts(10)})
        assert worker._proactive_rate_limit_ok(-100) == "reduced"
        worker._proactive_today = 2
        assert worker._proactive_gate_blocks(-100) is True
        worker._proactive_today = 1
        assert worker._proactive_gate_blocks(-100) is False

    asyncio.run(main())