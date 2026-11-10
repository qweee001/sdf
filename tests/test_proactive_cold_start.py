"""冷啟動第一輪不睡滿 tick（2026-11-10 15:11 部署實測）。

破口：_proactive_loop 開頭無條件先 sleep(5~15 分鐘) 才做第一次判斷，整組重啟後
群裡就空掉一整個 tick。15:11:54 起好四號、15:25 才有第一句，中間 10 分鐘是純空等
（部署停機只佔 3 分鐘）。同一小時 14:39 手動全停也是同一個形狀：一停就 18 分鐘
沒有人回來。

修法：只改第一輪，抽 10~50 秒並依帳號錯開 0/7/14/21，之後照原來 5~15 分鐘。
"""

import asyncio
import time
from unittest.mock import AsyncMock, Mock

from test_worker_reply_arbitration import _worker


def _prep(worker, group):
    worker.is_running = True
    worker._last_activity = {group: time.time()}
    worker._known_groups = {group}
    worker.config.proactive_enabled = True
    worker.config.proactive_loop_min_seconds = 300.0
    worker.config.proactive_loop_max_seconds = 900.0
    worker.config.proactive_max_per_day = 10
    worker.config.proactive_min_interval_minutes = 1
    worker._is_sleeping = Mock(return_value=False)
    worker._is_busy_hour = Mock(return_value=False)
    worker._should_suppress_proactive = Mock(return_value=False)
    worker._proactive_gate_blocks = Mock(return_value=False)
    worker.db.claim_proactive_slot = AsyncMock(return_value=True)
    worker.db.get_group_messages = AsyncMock(return_value=[])
    worker.db.release_proactive_slot = AsyncMock()
    worker.db.recent_bot_texts_by_group = AsyncMock(return_value=[])
    worker.db.get_group_shared_notes = AsyncMock(return_value=[])
    worker._generate_context_topic = AsyncMock(return_value="")


def test_first_tick_sleeps_short_and_later_ticks_use_loop_range(monkeypatch):
    async def main():
        worker = _worker(101)
        group = -5428680940
        _prep(worker, group)
        delays = []

        async def controlled_sleep(delay):
            delays.append(delay)
            if len(delays) > 2:
                raise asyncio.CancelledError

        monkeypatch.setattr("app.worker.asyncio.sleep", controlled_sleep)
        await worker._proactive_loop()

        assert delays[0] <= 50 + 21, delays
        # 第二輪起回到 loop 帶（赤字提示會再打 0.55 折，下界放寬）
        assert 150 <= delays[1] <= 900, delays

    asyncio.run(main())


def test_first_tick_flag_cleared_after_one_round(monkeypatch):
    async def main():
        worker = _worker(101)
        assert worker._first_proactive_tick is True
        _prep(worker, -5428680940)

        async def stop(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr("app.worker.asyncio.sleep", stop)
        await worker._proactive_loop()
        assert worker._first_proactive_tick is False

    asyncio.run(main())
