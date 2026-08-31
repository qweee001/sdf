"""托管互聊冷却兜底测试：complete 链断裂时发送成功仍必须提交群级冷却。"""

import asyncio

from test_worker_reply_arbitration import MANAGED, _worker


class _BrokenCompleteDB:
    """reserve 正常、complete 永远失败的 DB —— 模拟链断裂。"""

    def __init__(self):
        self.cooldowns = []
        self.followup_claims = set()
        self.followup_pending = {}

    async def complete_managed_followup(self, *a, **k):
        return False

    async def release_managed_followup(self, *a, **k):
        pass

    async def ensure_managed_followup_cooldown(
        self, group_id, account_id, cooldown_seconds=600, **k
    ):
        self.cooldowns.append((group_id, account_id, cooldown_seconds))
        return None


def test_finish_managed_reservation_falls_back_to_cooldown():
    async def main():
        db = _BrokenCompleteDB()
        worker = _worker(sorted(MANAGED)[0], db=db)
        event = SimpleNamespace(
            chat_id=-5428680940, id=777, raw_text="x"
        )
        await worker._finish_managed_reservation(event, sent=True)
        assert db.cooldowns, "complete 失败时必须写入兜底冷却"
        assert db.cooldowns[0][0] == -5428680940
        assert db.cooldowns[0][2] == 600
        assert worker.stats["managed_sent"] == 1
        assert worker.stats.get("cooldown_fallback") == 1

    asyncio.run(main())


from types import SimpleNamespace  # noqa: E402
