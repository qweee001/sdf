"""Offline defect reproductions for snapshot 347eb6d; no external services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import Database
from app.worker import AccountWorker


def test_member_memory_preserves_more_than_the_latest_message(tmp_path):
    async def run():
        db = Database(str(tmp_path / "memory.db"))
        await db.connect()
        try:
            await db.upsert_group_member_note(-1001, 123, "fixture", "Likes coffee")
            await db.upsert_group_member_note(-1001, 123, "fixture", "Hello again")
            notes = await db.get_group_member_notes(-1001, 123, "fixture")
            assert "Likes coffee" in notes, notes
        finally:
            await db.close()

    asyncio.run(run())


def test_account_deletion_removes_member_and_shared_notes(tmp_path):
    async def run():
        db = Database(str(tmp_path / "deletion.db"))
        await db.connect()
        try:
            await db.create_account("fixture", "Fixture", "local-test-session")
            await db.upsert_group_member_note(-1001, 123, "fixture", "member note")
            await db.upsert_group_shared_note(-1001, "fixture", "shared note")
            await db.delete_account("fixture")
            assert await db.get_account("fixture") is None
            member = await db.get_group_member_notes(-1001, 123, "fixture")
            shared = await db.get_group_shared_notes(-1001, "fixture")
            assert (member, shared) == ([], []), (member, shared)
        finally:
            await db.close()

    asyncio.run(run())


@pytest.mark.xfail(strict=True, reason="Unrelated incoming messages cancel all replies in the group")
def test_unrelated_message_keeps_pending_direct_reply():
    async def run():
        worker = object.__new__(AccountWorker)
        worker.is_running = True
        worker.tg_client = object()
        worker.selected_groups = {-1001}
        worker._known_groups = set()
        worker._last_activity = {}
        worker.managed_ids = set()
        worker.last_human_activity = {}
        worker.topic_turn_counts = {}
        worker.stats = {"errors": 0}
        worker.account_id = "fixture"
        worker.name = "fixture"
        worker.db = SimpleNamespace(
            upsert_group_member_note=AsyncMock(),
            upsert_group_shared_note=AsyncMock(),
            add_message=AsyncMock(),
        )
        worker._record_group_event = AsyncMock()
        worker._should_reply = AsyncMock(return_value=False)
        pending = asyncio.create_task(asyncio.Event().wait())
        pending._sdf_group_id = -1001
        worker._reply_tasks = {pending}
        event = SimpleNamespace(
            is_private=False, is_group=True, chat_id=-1001, sender_id=456,
            raw_text="An unrelated message", media=None,
            get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Other")),
        )
        try:
            await worker.on_message(event)
            worker._should_reply.assert_awaited_once()
            assert worker.stats["errors"] == 0
            assert pending.cancelling() == 0
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.xfail(strict=True, reason="Big5 repertoire is not a traditional/simplified classifier")
def test_traditional_name_character_is_not_simplified():
    assert not AccountWorker._has_simplified_chars("\u5586")
