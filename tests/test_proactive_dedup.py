"""去重持久化與跨帳號撞句攔截（沒有預設池之後，去重全壓在即時生成的話題上）。

- reload: proactive 去重集合跨帳號共享並持久化；重啟後從 DB 回填。
- 生成: 同一群其他帳號講過的句子，本帳號不得再發；撞句就回空字串（寧可不發）。
"""

import asyncio

from test_worker_reply_arbitration import _ClaimDB, _worker


class _TopicDB(_ClaimDB):
    """提供即時話題生成需要的讀取介面。"""

    def __init__(self, history=(), msgs=()):
        super().__init__()
        self.history = list(history)
        self.msgs = list(msgs)

    async def recent_bot_texts_by_group(self, group_id, *, hours=48, limit=200):
        return list(self.history)

    async def get_group_messages(self, group_id, limit=100):
        return list(self.msgs)

    async def get_group_shared_notes(self, group_id, account_id, limit=10):
        return []


def test_worker_backfills_recent_proactive_topics_from_db():
    """重啟後 worker 必須從 DB 回填最近已發話題（含其他帳號發的），否則重啟清零必然復讀。"""

    async def main():
        seen = [
            "附近有人嗎？先聊得來再決定要不要見",
            "週末想去逛書店，有人也喜歡嗎？",
        ]

        class BackfillDB(_ClaimDB):
            async def recent_bot_texts_by_group(self, group_id, *, hours=48, limit=200):
                return list(seen)

        worker = _worker(101, db=BackfillDB())
        await worker.reload_proactive_memory()
        normalized = worker._recent_proactive_topics
        assert len(normalized) == 2, normalized
        assert any("附近有人嗎" in t for t in normalized)
        assert any("週末想去逛書店" in t for t in normalized)

    asyncio.run(main())


def test_context_topic_skips_text_another_account_already_sent():
    """同一群內其他帳號講過的句子，即時生成時必須被攔下（回空字串，不塞罐頭句）。"""

    async def main():
        from unittest.mock import AsyncMock

        already = "今天超有精神，有人想出去晃晃嗎？"
        worker = _worker(202, db=_TopicDB(history=[already]))
        await worker.reload_proactive_memory()
        assert worker._normalized_reply(already) in worker._recent_proactive_topics

        # 模型吐出同一句 → 拒絕（跨帳號去重）
        worker._call_ai = AsyncMock(return_value=already)
        assert await worker._generate_context_topic(-5428680940) == ""

        # 模型換一句新的 → 放行
        worker._call_ai = AsyncMock(return_value="今天想吃牛肉麵，有人要一起嗎")
        fresh = await worker._generate_context_topic(-5428680940)
        assert fresh == "今天想吃牛肉麵，有人要一起嗎"
        assert worker._normalized_reply(fresh) in worker._recent_proactive_topics

    asyncio.run(main())


def test_context_topic_returns_empty_when_model_keeps_repeating():
    """模型只會復讀時回空字串：這一輪不開口，不塞預設句。"""

    async def main():
        from unittest.mock import AsyncMock

        worker = _worker(303, db=_TopicDB())
        worker._call_ai = AsyncMock(return_value="週末想唱歌")
        assert await worker._generate_context_topic(-5428680940) == "週末想唱歌"
        assert await worker._generate_context_topic(-5428680940) == ""

    asyncio.run(main())


def test_claim_group_text_blocks_cross_account_duplicate():
    """同群同文案 1 小時內只允許第一個帳號發出（DB 層跨帳號攔截）。"""
    import os
    import tempfile

    async def main():
        from app.database import Database

        with tempfile.TemporaryDirectory() as td:
            db = Database(os.path.join(td, "t.db"))
            await db.connect()
            ok1 = await db.claim_group_text(-100, "同一句話", "acct-a")
            ok2 = await db.claim_group_text(-100, "同一句話", "acct-b")
            ok3 = await db.claim_group_text(-100, "不同的一句話", "acct-b")
            await db.close()
            assert ok1 is True
            assert ok2 is False, "跨帳號同句必須被攔"
            assert ok3 is True

    asyncio.run(main())
