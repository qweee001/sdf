"""①②③ 共用上下文快照 + 發送前新鮮度檢查（借鑑 jev-chat-jarvis 的兩點）。

1. 三個階段不再各讀各的 DB：① 決策、② 生成、③ 審核看到的是同一份上下文。
2. 發送前再確認「要回的那句」沒有被後來的真人訊息追過（避免回過時話題）。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_worker_reply_arbitration import _worker


class _CtxDB:
    def __init__(self, messages=None, replies=None, shared=None, member=None):
        self.messages = list(messages or [])
        self.replies = list(replies or [])
        self.shared = list(shared or [])
        self.member = list(member or [])
        self.calls = {"recent": 0, "replies": 0, "shared": 0, "member": 0, "group": 0}

    async def get_recent_messages(self, *_a, **_k):
        self.calls["recent"] += 1
        return list(self.messages)

    async def get_recent_group_replies(self, *_a, **_k):
        self.calls["replies"] += 1
        return list(self.replies)

    async def get_group_shared_notes(self, *_a, **_k):
        self.calls["shared"] += 1
        return list(self.shared)

    async def get_group_member_notes(self, *_a, **_k):
        self.calls["member"] += 1
        return list(self.member)

    async def get_group_messages(self, *_a, **_k):
        self.calls["group"] += 1
        return list(self.messages)


def _reply_event(text="今天好累喔", message_id=77):
    event = AsyncMock(
        sender_id=999,
        chat_id=-5428680940,
        id=message_id,
        mentioned=False,
        is_reply=False,
        reply_to=None,
        raw_text=text,
        media=None,
    )
    event.sender = None
    return event


def test_snapshot_reads_each_source_once():
    async def main():
        db = _CtxDB(
            messages=[{"sender_id": 5, "sender_name": "阿宏", "role": "user", "content": "加班到十點"}],
            shared=["有人聊過宵夜"],
        )
        worker = _worker(101, db=db)
        event = _reply_event()
        ctx = await worker._reply_context_snapshot(event)
        assert ctx["history"]
        assert ctx["shared_notes"] == ["有人聊過宵夜"]
        assert db.calls["recent"] == 1 and db.calls["shared"] == 1

    asyncio.run(main())


def test_decision_and_generation_share_one_snapshot():
    """② 生成不得再讀一次歷史：三階段共用同一份上下文，避免判斷看 A、生成看 B。"""

    async def main():
        fresh_rows = [
            {"sender_id": 5, "sender_name": "阿宏", "role": "user", "content": "第一版上下文"}
        ]
        db = _CtxDB(messages=fresh_rows, replies=["近期文案"])
        worker = _worker(101, db=db)
        event = _reply_event()
        event._sdf_ctx = await worker._reply_context_snapshot(event)
        reads_after_snapshot = db.calls["recent"]

        worker._call_ai = AsyncMock(return_value="那早點休息")
        text = await worker._generate_reply(event)
        assert text == "那早點休息"
        # 快照之後 ② 沒有再讀歷史／近期文案
        assert db.calls["recent"] == reads_after_snapshot
        assert db.calls["replies"] <= 1
        prompt = worker._call_ai.await_args.args[1]
        assert "第一版上下文" in prompt
        assert "近期文案" in prompt

    asyncio.run(main())


def test_generation_still_reads_db_without_snapshot():
    """沒有快照時（例如舊呼叫路徑）行為不變，仍然自己讀 DB。"""

    async def main():
        db = _CtxDB(messages=[{"sender_id": 5, "sender_name": "阿宏", "role": "user", "content": "沒快照"}])
        worker = _worker(101, db=db)
        worker._call_ai = AsyncMock(return_value="好喔")
        assert await worker._generate_reply(_reply_event()) == "好喔"
        assert db.calls["recent"] >= 1

    asyncio.run(main())


def test_freshness_blocks_when_human_spoke_later():
    async def main():
        worker = _worker(101)
        event = _reply_event("今天好累喔", message_id=10)
        event._sdf_seen_at = 1000.0
        worker.db.get_group_messages = AsyncMock(
            return_value=[
                {
                    "sender_id": 5,
                    "sender_name": "阿宏",
                    "role": "user",
                    "content": "那先不聊了",
                    "timestamp": 1200.0,  # 在我們看到目標訊息之後才講的
                }
            ]
        )
        assert await worker._context_still_fresh(event) is False

    asyncio.run(main())


def test_freshness_allows_when_only_water_army_or_old_human_messages():
    async def main():
        worker = _worker(101)
        event = _reply_event("今天好累喔", message_id=10)
        event._sdf_seen_at = 1000.0
        worker.db.get_group_messages = AsyncMock(
            return_value=[
                {"sender_id": 7, "sender_name": "小小", "role": "assistant", "content": "早點睡", "timestamp": 1300.0},
                {"sender_id": 5, "sender_name": "阿宏", "role": "user", "content": "更早之前的訊息", "timestamp": 500.0},
            ]
        )
        assert await worker._context_still_fresh(event) is True

    asyncio.run(main())


def test_freshness_fails_open_without_timestamp_or_on_error():
    async def main():
        worker = _worker(101)
        event = _reply_event("今天好累喔", message_id=10)
        # 沒有 _sdf_seen_at（例如測試或舊路徑）→ 不擋
        assert await worker._context_still_fresh(event) is True

        event._sdf_seen_at = time.time()
        worker.db.get_group_messages = AsyncMock(side_effect=OSError("db down"))
        assert await worker._context_still_fresh(event) is True

    asyncio.run(main())
