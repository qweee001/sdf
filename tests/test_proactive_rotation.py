"""水軍輪替（最少發言優先）：誰最久沒在這個群講話，誰優先接。

舊規則是「上一則是水軍就一律閉嘴」→ 實測群 111 幾乎只有小小在撐場。
"""

import asyncio
import time

from test_worker_reply_arbitration import _worker

GROUP = -5565520321
ME = 101
OTHER = 202
THIRD = 303


class _RotateDB:
    """提供輪替需要的兩個介面；spoke＝sender_id -> 最後發言時間。"""

    def __init__(self, spoke=None):
        self.spoke = dict(spoke or {})
        self.calls = 0

    async def group_bot_last_spoke(self, group_id):
        self.calls += 1
        return dict(self.spoke)


def _row(role, sender_id, age_seconds):
    return {
        "role": role,
        "sender_id": sender_id,
        "sender_name": f"帳號{sender_id}",
        "content": "上一句",
        "timestamp": time.time() - age_seconds,
    }


def test_human_message_always_allowed():
    async def main():
        worker = _worker(ME, db=_RotateDB())
        worker.tg_user_id = ME
        assert await worker._proactive_rotation_ok(GROUP, _row("user", 999, 10)) is True

    asyncio.run(main())


def test_does_not_reply_to_itself():
    async def main():
        worker = _worker(ME, db=_RotateDB())
        worker.tg_user_id = ME
        assert await worker._proactive_rotation_ok(GROUP, _row("assistant", ME, 600)) is False

    asyncio.run(main())


def test_skips_when_another_bot_just_spoke():
    async def main():
        worker = _worker(ME, db=_RotateDB({OTHER: time.time() - 300}))
        worker.tg_user_id = ME
        # 上一則只有 5 秒前 → 先等一下，避免三隻疊字
        assert await worker._proactive_rotation_ok(GROUP, _row("assistant", OTHER, 5)) is False

    asyncio.run(main())


def test_rotates_to_account_that_spoke_least_recently():
    async def main():
        now = time.time()
        # 我 10 分鐘前講過；其他人 3 分鐘前 → 輪到我
        worker = _worker(ME, db=_RotateDB({ME: now - 600, OTHER: now - 180, THIRD: now - 240}))
        worker.tg_user_id = ME
        assert await worker._proactive_rotation_ok(GROUP, _row("assistant", OTHER, 200)) is True

        # 反過來：別人 10 分鐘前、我 2 分鐘前 → 輪不到我
        worker2 = _worker(ME, db=_RotateDB({ME: now - 120, OTHER: now - 600}))
        worker2.tg_user_id = ME
        assert await worker2._proactive_rotation_ok(GROUP, _row("assistant", OTHER, 300)) is False

    asyncio.run(main())


def test_never_spoken_before_wins_the_turn():
    async def main():
        now = time.time()
        worker = _worker(ME, db=_RotateDB({OTHER: now - 60, THIRD: now - 90}))
        worker.tg_user_id = ME
        assert await worker._proactive_rotation_ok(GROUP, _row("assistant", OTHER, 120)) is True

    asyncio.run(main())


def test_missing_db_support_fails_open():
    """DB 沒有這個查詢時不能整條主動發言掛掉（舊版 fake 相容）。"""

    async def main():
        class _Bare:
            pass

        worker = _worker(ME, db=_Bare())
        worker.tg_user_id = ME
        assert await worker._proactive_rotation_ok(GROUP, _row("assistant", OTHER, 300)) is True

    asyncio.run(main())


def test_self_throttle_blocks_second_proactive_within_gap():
    """自我節流：我 120 秒前才在該群發過言（不管主動或回覆），
    就算 rotation 判斷「輪到我」（我比別號更久沒講）也先閉嘴。

    實測根因（10-08 群 111）：真人熱聊時每個 cycle 獨立擲 30% 接話，
    同一帳號連中 13 分鐘連發 4 次主動——rotation 只防三隻接力。
    """

    async def main():
        now = time.time()
        # 我 120 秒前講過；別號 600 秒前 → rotation 本來會判「輪到我」
        worker = _worker(ME, db=_RotateDB({ME: now - 120, OTHER: now - 600}))
        worker.tg_user_id = ME
        assert await worker._proactive_rotation_ok(GROUP, _row("assistant", OTHER, 300)) is False

        # 超過自我節流窗（360s）後就放行
        worker2 = _worker(ME, db=_RotateDB({ME: now - 600, OTHER: now - 600}))
        worker2.tg_user_id = ME
        assert await worker2._proactive_rotation_ok(GROUP, _row("assistant", OTHER, 300)) is True

    asyncio.run(main())
