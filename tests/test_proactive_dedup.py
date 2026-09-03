"""反重复 P0-2/P1-2：去重持久化与跨账号撞句拦截。

- P0-2: proactive 去重集合跨账号共享并持久化；重启后从 DB 回填。
- P1-2: 发送前查全账号最近发送历史，撞句重抽。
"""

import asyncio
import time

import pytest

from test_worker_reply_arbitration import MANAGED, _ClaimDB, _worker


def test_worker_backfills_recent_proactive_topics_from_db():
    """重启后 worker 必须从 DB 回填最近已发话题（含其他账号发的），否则重启清零必然复读。"""

    async def main():
        seen = [
            "附近有人嗎？先聊得來再決定要不要見",
            "週末想去逛書店，有人也喜歡嗎？",
        ]

        class BackfillDB(_ClaimDB):
            def __init__(self):
                super().__init__()
                self.recorded = []

            async def recent_bot_texts_by_group(self, group_id, *, hours=48, limit=200):
                return list(seen)

        db = BackfillDB()
        worker = _worker(101, db=db)
        # 模拟 manager 启动时的回填调用
        await worker.reload_proactive_memory()
        normalized = worker._recent_proactive_topics
        assert len(normalized) == 2, normalized
        assert any("附近有人嗎" in t for t in normalized)
        assert any("週末想去逛書店" in t for t in normalized)

    asyncio.run(main())


def test_next_proactive_topic_avoids_cross_account_history():
    """同一群内其他账号发过的句子，本账号不得再抽中。"""

    async def main():
        class HistoryDB(_ClaimDB):
            def __init__(self):
                super().__init__()
                self.history = ["今天超有精神，有人想出去晃晃嗎？"]

            async def recent_bot_texts_by_group(self, group_id, *, hours=48, limit=200):
                return list(self.history)

        db = HistoryDB()
        worker = _worker(202, db=db)
        await worker.reload_proactive_memory()
        topic = worker._next_proactive_topic()
        assert topic
        assert "今天超有精神" not in topic
        # 抽题查过跨账号历史（回填内容已进去重集合）
        assert any("今天超有精神" in t for t in worker._recent_proactive_topics)

    asyncio.run(main())


def test_proactive_topic_falls_back_when_pool_exhausted():
    """池子全部用过时返回空串（宁可不发也不复读）。"""

    async def main():
        class FullDB(_ClaimDB):
            async def recent_bot_texts_by_group(self, group_id, *, hours=48, limit=200):
                # 返回全部池子内容 + 更多，逼上去重上限
                from app.persona import (
                    BOY_PROACTIVE,
                    DAILY_TOPICS,
                    GIRL_PROACTIVE,
                    PERSONA_PROACTIVE,
                    SHOW_OFF_FEMALE,
                    SHOW_OFF_MALE,
                )
                pools = [
                    *PERSONA_PROACTIVE["shy"],
                    *PERSONA_PROACTIVE["lively"],
                    *PERSONA_PROACTIVE["flirty"],
                    *PERSONA_PROACTIVE["direct"],
                    *GIRL_PROACTIVE,
                    *BOY_PROACTIVE,
                    *DAILY_TOPICS,
                    *SHOW_OFF_FEMALE,
                    *SHOW_OFF_MALE,
                ]
                return pools

        db = FullDB()
        worker = _worker(303, db=db)
        await worker.reload_proactive_memory()
        # 池子所有句子都在最近历史里 → 拒绝生成复读
        assert worker._next_proactive_topic() == ""

    asyncio.run(main())


def test_claim_group_text_blocks_cross_account_duplicate():
    """同群同文案 1 小时内只允许第一个账号发出（DB 层跨账号拦截）。"""
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
            assert ok2 is False, "跨账号同句必须被拦"
            assert ok3 is True

    asyncio.run(main())