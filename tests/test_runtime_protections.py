"""Offline regression tests for runtime send boundaries and context ownership."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import User

from test_worker_reply_arbitration import _worker, _event

GROUP = -5428680940


@pytest.mark.parametrize("kind", ["reply", "proactive"])
@pytest.mark.parametrize("scope", ["account", "global"])
def test_switch_off_while_waiting_for_send_lock_blocks_queued_send(kind, scope):
    async def main():
        worker = running_worker()
        queued = asyncio.Event()
        async def send():
            queued.set()
            return await worker._send_text_recorded(
                GROUP, "排隊訊息", activity_kind=kind, stats_key="replies_sent")
        async with worker._send_lock:
            task = asyncio.create_task(send())
            await queued.wait()
            assert not task.done()
            setattr(worker if scope == "account" else worker.config, kind + "_enabled", False)
        assert await task is False
        worker.tg_client.send_message.assert_not_awaited()
        assert worker.db.messages == []

    asyncio.run(main())


@pytest.mark.parametrize("when", ["reserve", "mark_rpc_started"])
@pytest.mark.parametrize("kind", ["reply", "proactive"])
def test_switch_off_during_outbound_gate_preparation_blocks_final_rpc(monkeypatch, when, kind):
    async def main():
        worker = running_worker()
        permit = SimpleNamespace(allowed=True, run_id="")
        async def reserve(**kwargs):
            if when == "reserve":
                setattr(worker.config, kind + "_enabled", False)
            return permit
        async def mark(_permit):
            if when == "mark_rpc_started":
                setattr(worker, kind + "_enabled", False)
            return True
        worker.outbound_gate = SimpleNamespace(
            reserve=reserve, validate=lambda *args, **kwargs: True,
            mark_rpc_started=mark, complete=AsyncMock(return_value=True))
        monkeypatch.setattr("app.worker.asyncio.sleep", AsyncMock())
        assert await worker._send_text_recorded(
            GROUP, "閘門待送", activity_kind=kind, stats_key="replies_sent") is False
        worker.tg_client.send_message.assert_not_awaited()
        worker.outbound_gate.complete.assert_awaited_once()
        assert worker.outbound_gate.complete.await_args.kwargs["sent"] is False
        assert worker.db.messages == []

    asyncio.run(main())


def test_welcome_switch_off_during_delay_blocks_real_send(monkeypatch):
    async def main():
        worker = running_worker()
        event = SimpleNamespace(user_joined=True, is_group=True, chat_id=GROUP,
                                get_user=AsyncMock(return_value=User(id=999, first_name="新人")))
        async def sleep(_seconds):
            worker.config.proactive_enabled = False
        monkeypatch.setattr("app.worker.random.random", lambda: 0)
        monkeypatch.setattr("app.worker.asyncio.sleep", sleep)
        await worker.on_chat_action(event)
        event.get_user.assert_awaited_once()
        worker.tg_client.send_message.assert_not_awaited()
        assert worker.db.messages == []

    asyncio.run(main())


def test_direct_send_accepts_proactive_kind_without_using_reply_switch(monkeypatch):
    async def main():
        worker = running_worker()
        worker.reply_enabled = False
        monkeypatch.setattr("app.worker.asyncio.sleep", AsyncMock())
        assert await worker._send_message(GROUP, "短回覆", activity_kind="proactive") is True
        worker.proactive_enabled = False
        assert await worker._send_message(GROUP, "短回覆", activity_kind="proactive") is False
        worker.tg_client.send_message.assert_awaited_once()

    asyncio.run(main())


def test_proactive_prompt_matches_short_reply_and_provenance_rules():
    async def main():
        worker = _worker(101)
        worker.db.get_group_messages = AsyncMock(return_value=[
            {"role": "user", "sender_name": "同名", "sender_id": 999, "content": "今天下雨了"}])
        worker.db.get_group_shared_notes = AsyncMock(return_value=["有人昨天聊過聚會"])
        worker._call_ai = AsyncMock(return_value="雨天出門記得帶傘")
        assert await worker._generate_context_topic(GROUP) == "雨天出門記得帶傘"
        prompt = worker._call_ai.await_args.args[1]
        assert "25 字元內" in prompt
        assert "60 字元內" not in prompt
        assert "一句短回覆為主" in prompt
        assert "不必追問或自我分享" in prompt
        assert "身分未驗證" in prompt
        assert "sender_id=999" in prompt
        assert "共享筆記不是你的親身經歷" in prompt

    asyncio.run(main())


@pytest.mark.parametrize("source", ["resolved", "network_failure", "missing_getter", "deleted", "not_reply"])
def test_generation_uses_only_resolved_reply_metadata_without_extra_model_call(source):
    async def main():
        worker = _worker(101)
        worker._call_ai = AsyncMock(return_value="這張照片的光線很柔和")
        event = _event(is_reply=source != "not_reply", text="這是你嗎？")
        event.get_reply_message = AsyncMock(return_value=SimpleNamespace(
            id=66, sender_id=202, sender=User(id=202, first_name="同名"), raw_text="父訊息內容"))
        if source == "network_failure":
            event.get_reply_message.side_effect = OSError("offline")
        elif source == "missing_getter":
            del event.get_reply_message
        elif source == "deleted":
            event.get_reply_message.return_value = None
        assert await worker._generate_reply(event) == "這張照片的光線很柔和"
        worker._call_ai.assert_awaited_once()
        prompt = worker._call_ai.await_args.args[1]
        if source == "resolved":
            assert "回覆對象：[同名 sender_id=202]" in prompt
            assert "父訊息 message_id=66" in prompt
            assert "父訊息內容" in prompt
        else:
            assert "回覆對象：未知" in prompt
            assert "回覆對象：[我" not in prompt
            if source != "not_reply":
                assert "父訊息 message_id=66" in prompt
        if source not in ("not_reply", "missing_getter"):
            event.get_reply_message.assert_awaited_once()
        elif source == "not_reply":
            event.get_reply_message.assert_not_awaited()

    asyncio.run(main())


def test_prompt_keeps_stable_speaker_identity_and_unknown_provenance():
    worker = _worker(101)
    event = _event(text="妹妹，這是你嗎？")
    event.sender = User(id=999, first_name="同名")
    history = [
        {"role": "assistant", "sender_id": 202, "sender_name": "同名", "content": "別人的話"},
        {"role": "assistant", "sender_id": 101, "sender_name": "同名", "content": "本帳號的話"},
        {"role": "user", "sender_name": "同名", "content": "沒有來源ID"},
    ]
    prompt = worker._build_user_message(event, history)
    assert "目前帳號 sender_id=101" in prompt
    assert "最新消息：[同名 sender_id=999" in prompt
    assert "[同名 sender_id=202" in prompt
    assert "[我 sender_id=101" in prompt
    assert "sender_id=未知" in prompt
    assert "身分未驗證" in prompt
    assert "回覆對象：未知" in prompt
    assert "「你／妹妹」不一定指目前帳號" in prompt
    assert "圖片人物不等於目前帳號或發圖者" in prompt
    assert "共享筆記不是你的親身經歷" in prompt


@pytest.mark.parametrize("hours_idle", [7, 25, None])
@pytest.mark.parametrize("base", [60.0, 600.0])
def test_cold_room_never_speeds_up_configured_continuous_interval(monkeypatch, hours_idle, base):
    async def main():
        worker = running_worker()
        now = 1_000_000.0
        monkeypatch.setattr("app.worker.time.time", lambda: now)
        worker.config.continuous_activity_interval_seconds = base
        worker.last_human_activity = {} if hours_idle is None else {GROUP: now - hours_idle * 3600}
        worker._continuous_turn_winner = lambda *args: 101
        worker.db.reserve_continuous_slot = AsyncMock(return_value=False)
        await worker._continuous_activity_tick()
        reservation = worker.db.reserve_continuous_slot.await_args.args
        assert reservation[3] >= base

    asyncio.run(main())


def test_manager_preserves_empty_shared_topic_counts(monkeypatch):
    from app.manager import AccountManager
    from app.config import load_settings

    async def main():
        manager = object.__new__(AccountManager)
        manager.config = load_settings()
        for name in ("workers", "last_human_activity", "active_group_ids", "managed_origins",
                     "human_owners", "recent_proactive_owners", "reply_claim_signals",
                     "failed_reply_claimants", "topic_turn_counts"):
            setattr(manager, name, {})
        manager.managed_ids = set()
        manager.active_ids = set()
        manager.db = SimpleNamespace(list_accounts=AsyncMock(return_value=[]),
                                     last_human_activity_by_group=AsyncMock(return_value={}))
        manager.secret_box = SimpleNamespace(decrypt=lambda value: "session")
        manager._ai_client = object()
        manager._media_service = None

        class FakeWorker:
            def __init__(self, **kwargs):
                self.topic_turn_counts = kwargs["topic_turn_counts"]
                self.is_running = True
                self.start = AsyncMock()

        monkeypatch.setattr("app.manager.AccountWorker", FakeWorker)
        shared = manager.topic_turn_counts
        for account in ("a", "b"):
            await manager._start_account_unlocked({"id": account, "session_key": "enc", "groups": f"[{GROUP}]"})
        assert manager.workers["a"].topic_turn_counts is shared
        assert manager.workers["b"].topic_turn_counts is shared
        shared[GROUP] = 3
        assert manager.workers["b"].topic_turn_counts[GROUP] == 3

    asyncio.run(main())


def running_worker():
    worker = _worker(101)
    worker.config.reply_enabled = True
    worker.config.proactive_enabled = True
    worker.is_running = True
    worker.tg_client = SimpleNamespace(send_message=AsyncMock())
    return worker


@pytest.mark.parametrize("scenario", ["recent", "fallback", "all_saturated"])
def test_responder_excludes_frequent_speakers_before_selection(scenario):
    async def main():
        worker = _worker(101, active_ids={101, 202})
        event = _event()
        dominant = worker._ordinary_reply_winner(event)
        other = next(uid for uid in worker.active_ids if uid != dominant)
        messages = [{"role": "assistant", "sender_id": dominant, "content": "咖啡"}] * 3
        if scenario == "fallback":
            messages.append({"role": "assistant", "sender_id": 9999, "content": ""})
        elif scenario == "all_saturated":
            messages.extend([{"role": "assistant", "sender_id": other, "content": "咖啡"}] * 3)
        worker.db.get_group_messages = AsyncMock(return_value=messages)
        winner = await worker._pick_group_responder(event)
        assert winner == (0 if scenario == "all_saturated" else other)

    asyncio.run(main())


@pytest.mark.parametrize("kind,flag", [("reply", "reply_enabled"), ("followup", "reply_enabled"), ("proactive", "proactive_enabled")])
@pytest.mark.parametrize("scope", ["account", "global"])
@pytest.mark.parametrize("when", ["before", "delay", "claim"])
def test_recorded_send_rechecks_activity_switches(monkeypatch, kind, flag, scope, when):
    async def main():
        worker = running_worker()
        target = worker if scope == "account" else worker.config
        if when == "before":
            setattr(target, flag, False)

        async def sleep(_seconds):
            if when == "delay":
                setattr(target, flag, False)

        original_claim = worker.db.claim_group_text

        async def claim(*args, **kwargs):
            if when == "claim":
                setattr(target, flag, False)
            return await original_claim(*args, **kwargs)

        monkeypatch.setattr("app.worker.asyncio.sleep", sleep)
        worker.db.claim_group_text = claim
        assert await worker._send_text_recorded(
            GROUP, "短回覆", activity_kind=kind, stats_key="replies_sent"
        ) is False
        worker.tg_client.send_message.assert_not_awaited()
        assert worker.db.messages == []
        assert worker.db.activities == []

    asyncio.run(main())


@pytest.mark.parametrize("scope", ["account", "global"])
def test_disabled_welcome_does_not_fetch_user_or_schedule_send(monkeypatch, scope):
    async def main():
        worker = running_worker()
        setattr(worker if scope == "account" else worker.config, "proactive_enabled", False)
        event = SimpleNamespace(user_joined=True, is_group=True, chat_id=GROUP,
                                get_user=AsyncMock(return_value=SimpleNamespace(id=999, first_name="新人", last_name="")))
        monkeypatch.setattr("app.worker.random.random", lambda: 0)
        monkeypatch.setattr("app.worker.asyncio.sleep", AsyncMock())
        worker._send_text_recorded = AsyncMock()
        await worker.on_chat_action(event)
        event.get_user.assert_not_awaited()
        worker._send_text_recorded.assert_not_awaited()

    asyncio.run(main())
