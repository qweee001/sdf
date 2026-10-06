"""純水軍自演剎車：實測三隻在沒有真人的群裡一路接力升級。

時間軸（群 111，4 個成員＝你＋三隻水軍）：
10:29 RICH → 10:35 小小 → 10:43 RICH → 10:51 小天後 → 11:00 小小 → 11:10 RICH → 11:17 小小
七則連續沒有真人插入，最後演成「來中壢讓我檢查／不如士林讓我檢查」這種沒對象的邀約。
"""

import asyncio
import time

from app.worker import _MAX_WATER_ONLY_STREAK, AccountWorker
from test_worker_reply_arbitration import _worker

GROUP = -5565520321
ME = 101
OTHER = 202
THIRD = 303


def _msg(role, sender_id, age=120.0, content="訊息"):
    return {
        "role": role,
        "sender_id": sender_id,
        "sender_name": f"帳號{sender_id}",
        "content": content,
        "timestamp": time.time() - age,
    }


class _StreakDB:
    def __init__(self, rows):
        self.rows = list(rows)

    async def get_group_messages(self, *_a, **_k):
        return list(self.rows)

    async def group_bot_last_spoke(self, group_id):
        return {OTHER: time.time() - 600, THIRD: time.time() - 900}


def test_water_only_streak_counter():
    assert AccountWorker._water_only_streak([]) == 0
    assert AccountWorker._water_only_streak([_msg("user", 999)]) == 0
    assert AccountWorker._water_only_streak([_msg("user", 999), _msg("assistant", 1)]) == 1
    assert (
        AccountWorker._water_only_streak(
            [_msg("user", 999), _msg("assistant", 1), _msg("assistant", 2)]
        )
        == 2
    )


def test_proactive_stops_after_two_water_only_messages():
    """尾端連續兩則都是水軍 → 這輪不主動開口（真人講話後才恢復）。"""

    async def main():
        db = _StreakDB([_msg("user", 999, age=3600), _msg("assistant", OTHER), _msg("assistant", THIRD)])
        worker = _worker(ME, db=db)
        worker.tg_user_id = ME
        assert await worker._proactive_rotation_ok(GROUP, _msg("assistant", THIRD)) is False

    asyncio.run(main())


def test_proactive_resumes_after_human_speaks():
    async def main():
        db = _StreakDB([_msg("assistant", OTHER), _msg("user", 999, age=30)])
        worker = _worker(ME, db=db)
        worker.tg_user_id = ME
        # 上一則是真人 → 直接放行（輪替與剎車都不適用）
        assert await worker._proactive_rotation_ok(GROUP, _msg("user", 999, age=30)) is True

    asyncio.run(main())


def test_single_water_message_still_allows_rotation():
    """只有一則水軍時照舊輪替（新規則不該把三隻都鎖死）。"""

    async def main():
        db = _StreakDB([_msg("user", 999, age=1800), _msg("assistant", OTHER)])
        worker = _worker(ME, db=db)
        worker.tg_user_id = ME
        assert await worker._proactive_rotation_ok(GROUP, _msg("assistant", OTHER)) is True

    asyncio.run(main())


def test_topic_prompt_caps_explicitness_when_no_human_around():
    """沒有真人時：prompt 要壓低尺度、禁止無對象邀約。"""

    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(101)
        worker.db.get_group_shared_notes = AsyncMock(return_value=[])
        worker.db.get_group_messages = AsyncMock(
            return_value=[_msg("assistant", OTHER, age=120, content="先練練舌頭功")]
        )
        worker._call_ai = AsyncMock(return_value="今天好累喔")
        await worker._generate_context_topic(GROUP)
        prompt = worker._call_ai.await_args.args[1]
        assert "沒有真人講話" in prompt
        assert "不要邀約見面" in prompt
        assert "不要升級" in prompt

    asyncio.run(main())


def test_topic_prompt_keeps_full_scale_when_human_is_active():
    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(101)
        worker.db.get_group_shared_notes = AsyncMock(return_value=[])
        worker.db.get_group_messages = AsyncMock(
            return_value=[_msg("user", 999, age=60, content="今天好想找人陪我")]
        )
        worker._call_ai = AsyncMock(return_value="我在這啊🫦")
        await worker._generate_context_topic(GROUP)
        prompt = worker._call_ai.await_args.args[1]
        assert "沒有真人講話" not in prompt

    asyncio.run(main())
