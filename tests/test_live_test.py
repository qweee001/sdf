import asyncio
import hashlib
import json
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.database import Database
from app.live_test import (
    BoundVideoAsset,
    BoundedLiveTest,
    LiveTestError,
    _RunExpired,
)
from app.media import MediaAsset
from app.worker import (
    BoundVoiceAsset,
    MediaEvidence,
    VideoContextEvidence,
    VoiceGenerationEvidence,
)


ACCOUNT_IDS = [
    "2ce525dfb0d4",
    "faa9a202f96e",
    "038632e4395b",
    "e63e27a4340d",
]
GROUP_ID = -5428680940
ACCOUNT_AGES = dict(zip(ACCOUNT_IDS, (21, 25, 29, 34), strict=True))


def _persona(account_id, *, gender="女", age=None):
    return {
        "name": account_id,
        "gender": gender,
        "age": ACCOUNT_AGES[account_id] if age is None else age,
        "city": "台北",
        "district": "中山",
        "industry": "設計",
        "university": "台大",
        "personality": "自然",
        "hobbies": ["電影"],
        "looking_for": "聊天",
        "meetups_done": 0,
        "schedule": "正常",
        "chat_style": "生活碎念",
    }


class FakeClock:
    def __init__(self, now=1_000.0, on_sleep=None):
        self.now = float(now)
        self.sleeps = []
        self.on_sleep = on_sleep

    def time(self):
        return self.now

    async def sleep(self, seconds):
        seconds = float(seconds)
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self)
        await asyncio.sleep(0)


class FakeMediaService:
    def __init__(self):
        self.vision_calls = []

    async def understand_image(
        self, account_id, image_data, mime_type, system_prompt, user_message
    ):
        self.vision_calls.append(
            (account_id, image_data, mime_type, system_prompt, user_message)
        )
        return "我有看懂這張圖"


class FakeVideoClient:
    def __init__(self, on_generate=None):
        self.calls = []
        self.fail = False
        self.on_generate = on_generate

    async def generate(
        self,
        *,
        run_id,
        event_id,
        account_id,
        group_id,
        trigger_received_at,
        snapshot_at,
        snapshot_sha256,
        profile_id,
        context_prompt,
    ):
        call = {
            "run_id": run_id,
            "event_id": event_id,
            "account_id": account_id,
            "group_id": group_id,
            "trigger_received_at": trigger_received_at,
            "snapshot_at": snapshot_at,
            "snapshot_sha256": snapshot_sha256,
            "profile_id": profile_id,
            "context_prompt": context_prompt,
        }
        self.calls.append(call)
        if self.on_generate:
            self.on_generate()
        if self.fail:
            return None
        asset = MediaAsset(
            "video",
            b"\x00\x00\x00\x18ftypmp42" + b"fresh" * 100,
            f"wan-{len(self.calls)}.mp4",
            "video/mp4",
        )
        return BoundVideoAsset(
            asset=asset,
            media_evidence=MediaEvidence(
                request_id=hashlib.sha256(event_id.encode()).hexdigest()[:32],
                snapshot_sha256=snapshot_sha256,
                output_sha256=hashlib.sha256(asset.data).hexdigest(),
                trigger_received_at=datetime.fromisoformat(
                    trigger_received_at
                ).timestamp(),
                snapshot_at=datetime.fromisoformat(snapshot_at).timestamp(),
                profile_id=str(profile_id),
                content_sha256=hashlib.sha256(
                    context_prompt.encode("utf-8")
                ).hexdigest(),
                decode_metadata_sha256="d" * 64,
            ),
        )


class FakeWorker:
    def __init__(self, account_id, clock):
        self.account_id = account_id
        self.is_running = True
        self.tg_client = object()
        self.selected_groups = {GROUP_ID}
        self.persona = _persona(account_id)
        self.media_service = FakeMediaService()
        self.clock = clock
        self.dispatches = []
        self.outbound_gate: Any = None
        self.current_context = "早餐蛋餅"
        self.context_events = []

    async def _record(self, kind, chat_id, payload, kwargs):
        assert self.outbound_gate is not None
        evidence = kwargs.get("media_evidence")
        evidence_kwargs = {}
        if evidence is not None:
            evidence_kwargs = {
                "request_id": evidence.request_id,
                "snapshot_sha256": evidence.snapshot_sha256,
                "output_sha256": evidence.output_sha256,
                "trigger_received_at": evidence.trigger_received_at,
                "snapshot_at": evidence.snapshot_at,
                "profile_id": evidence.profile_id,
                "content_sha256": evidence.content_sha256,
                "decode_metadata_sha256": evidence.decode_metadata_sha256,
            }
        permit = await self.outbound_gate.reserve(
            account_id=self.account_id,
            group_id=chat_id,
            kind=kwargs.get("live_test_kind") or kind,
            event_id=kwargs.get("live_test_event_id"),
            **evidence_kwargs,
        )
        if not permit.allowed:
            return False
        if not await self.outbound_gate.mark_rpc_started(permit):
            await self.outbound_gate.complete(
                permit,
                sent=False,
                detail="fake final RPC admission failed",
            )
            return False
        self.dispatches.append((self.clock.time(), kind, chat_id, payload, kwargs))
        return await self.outbound_gate.complete(permit, sent=True)

    async def _send_text_recorded(self, chat_id, text, **kwargs):
        return await self._record("text", chat_id, text, kwargs)

    async def _send_media_recorded(self, chat_id, asset, marker, **kwargs):
        return await self._record(asset.kind, chat_id, asset.filename, kwargs)

    async def generate_realtime_voice_reply(
        self, chat_id, *, run_id, event_id, trigger_received_at
    ):
        self.context_events.append(("voice_snapshot", chat_id, self.current_context))
        if not self.current_context:
            return None
        snapshot_at = self.clock.time()
        return VoiceGenerationEvidence(
            run_id=run_id,
            event_id=event_id,
            account_id=self.account_id,
            group_id=chat_id,
            trigger_received_at=trigger_received_at,
            snapshot_at=snapshot_at,
            snapshot_sha256=hashlib.sha256(
                self.current_context.encode()
            ).hexdigest(),
            profile_id=str(self.persona["age"]),
            text=f"語音回應：{self.current_context}",
        )

    async def _send_realtime_voice(self, evidence, **kwargs):
        asset = MediaAsset(
            "voice",
            b"OggS" + b"voice" * 500,
            f"voice-{evidence.event_id}.ogg",
            "audio/ogg",
        )
        media_evidence = MediaEvidence(
            request_id=hashlib.sha256(evidence.event_id.encode()).hexdigest()[:32],
            snapshot_sha256=evidence.snapshot_sha256,
            output_sha256=hashlib.sha256(asset.data).hexdigest(),
            trigger_received_at=evidence.trigger_received_at,
            snapshot_at=evidence.snapshot_at,
            profile_id=evidence.profile_id,
            content_sha256=hashlib.sha256(
                evidence.text.encode("utf-8")
            ).hexdigest(),
            decode_metadata_sha256="",
        )
        bound = BoundVoiceAsset(
            run_id=evidence.run_id,
            event_id=evidence.event_id,
            account_id=evidence.account_id,
            group_id=evidence.group_id,
            profile_id=evidence.profile_id,
            text=evidence.text,
            text_sha256=hashlib.sha256(evidence.text.encode()).hexdigest(),
            asset=asset,
            media_evidence=media_evidence,
        )
        callback = kwargs.pop("before_send", None)
        if callback is not None:
            callback_result = callback(bound)
            if asyncio.iscoroutine(callback_result):
                callback_result = await callback_result
            if callback_result is False:
                return None
        sent = await self._send_media_recorded(
            evidence.group_id,
            asset,
            "[語音]",
            media_evidence=media_evidence,
            **kwargs,
        )
        return bound if sent else None

    async def generate_realtime_video_brief(
        self, chat_id, *, run_id, event_id, trigger_received_at
    ):
        self.context_events.append(("video_snapshot", chat_id, self.current_context))
        if not self.current_context:
            return None
        return VideoContextEvidence(
            run_id=run_id,
            event_id=event_id,
            account_id=self.account_id,
            group_id=chat_id,
            trigger_received_at=trigger_received_at,
            snapshot_at=self.clock.time(),
            snapshot_sha256=hashlib.sha256(
                self.current_context.encode()
            ).hexdigest(),
            profile_id=int(self.persona["age"]),
            context_prompt=f"影片回應：{self.current_context}",
        )

    async def send_live_test_asset(
        self, chat_id, asset, *, event_id, kind, media_evidence, marker=None
    ):
        return await self._send_media_recorded(
            chat_id,
            asset,
            marker or "[媒體]",
            live_test_event_id=event_id,
            live_test_kind=kind,
            media_evidence=media_evidence,
        )

    async def stop(self):
        self.is_running = False
        self.tg_client = None


class FakeManager:
    def __init__(self, db, clock):
        self.db = db
        self._clock = clock
        self.config = SimpleNamespace(
            media_enabled=True,
            voice_media_enabled=True,
            voice_realtime_url="https://voice.test",
            voice_realtime_token="token",
            video_realtime_url="https://wan.test",
            video_realtime_token="wan-token",
            video_realtime_request_timeout=3.0,
            video_realtime_poll_timeout=30.0,
            video_realtime_poll_interval=0.1,
            video_realtime_download_timeout=5.0,
        )
        self.workers = {
            account_id: FakeWorker(account_id, clock) for account_id in ACCOUNT_IDS
        }
        self.last_human_activity = {}
        self.stopped = []
        self.video_client = FakeVideoClient()
        self.live_test_start_calls = []
        self.live_test: Any = SimpleNamespace(outbound_gate=None)
        self.live_test_start_fail_after: int | None = None
        self.live_test_start_gate_scopes: list[frozenset[str]] = []

    async def start_live_test_accounts(
        self, account_ids, group_id, *, before_release=None
    ):
        self.live_test_start_calls.append((tuple(account_ids), group_id))
        for account_id in account_ids:
            if account_id in self.workers:
                continue
            active = self.live_test.outbound_gate._active
            self.live_test_start_gate_scopes.append(
                active[1] if active else frozenset({"missing-gate"})
            )
            if (
                self.live_test_start_fail_after is not None
                and len(self.workers) >= self.live_test_start_fail_after
            ):
                return "injected live-test account startup failure"
            await self.db.update_account(account_id, enabled=1)
            worker = FakeWorker(account_id, self._clock)
            worker.outbound_gate = self.live_test.outbound_gate
            self.workers[account_id] = worker
        if before_release is not None:
            await before_release()
        return ""

    async def stop(self, account_id):
        self.stopped.append(account_id)
        await self.db.update_account(account_id, enabled=0)
        worker = self.workers.pop(account_id, None)
        if worker:
            await worker.stop()
        return ""


async def _seed_accounts(db, *, gender="女", age=None):
    for index, account_id in enumerate(ACCOUNT_IDS, start=1):
        persona = json.dumps(
            _persona(account_id, gender=gender, age=age),
            ensure_ascii=False,
        )
        await db.create_account(account_id, account_id, "encrypted", persona)
        await db.update_account(
            account_id,
            tg_user_id=index,
            groups=json.dumps([GROUP_ID]),
            setup_complete=1,
            enabled=1,
        )


def _schedule(asset_root):
    for index in range(4):
        (asset_root / f"adult-{index}.jpg").write_bytes(b"\xff\xd8image\xff\xd9")

    events = [
        {
            "event_id": f"text-{index}",
            "offset_seconds": 0,
            "account_id": ACCOUNT_IDS[index % 4],
            "kind": "text",
            "text": f"測試文字 {index}",
        }
        for index in range(18)
    ]
    events.extend(
        {
            "event_id": f"voice-{index}",
            "offset_seconds": 0,
            "account_id": account_id,
            "kind": "voice",
        }
        for index, account_id in enumerate(ACCOUNT_IDS)
    )
    events.extend(
        {
            "event_id": f"image-{index}",
            "offset_seconds": 0,
            "account_id": account_id,
            "kind": "image",
            "path": f"adult-{index}.jpg",
        }
        for index, account_id in enumerate(ACCOUNT_IDS)
    )
    events.extend(
        {
            "event_id": f"video-{index}",
            "offset_seconds": 0,
            "account_id": ACCOUNT_IDS[index],
            "kind": "video",
        }
        for index in range(2)
    )
    events.extend(
        {
            "event_id": f"vision-{index}",
            "offset_seconds": 0,
            "account_id": ACCOUNT_IDS[index + 2],
            "kind": "vision_reply",
            "path": f"adult-{index}.jpg",
        }
        for index in range(2)
    )
    assert len(events) == 30
    return events


def test_voice_and_video_schedule_events_have_trigger_only_schema(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "trigger-schema.db"))
        await db.connect()
        await _seed_accounts(db)
        live_test = BoundedLiveTest(
            FakeManager(db, clock),
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        normalized = await live_test._validate(_request(root))
        triggers = [
            event
            for event in normalized["schedule"]
            if event["kind"] in {"voice", "video"}
        ]
        assert triggers
        assert all(
            set(event) == {"event_id", "offset_seconds", "account_id", "kind"}
            for event in triggers
        )

        forbidden_voice = _request(root)
        forbidden_voice["schedule"][18]["text"] = "預寫語音"
        with pytest.raises(LiveTestError, match="trigger fields only"):
            await live_test._validate(forbidden_voice)

        forbidden_video_path = _request(root)
        forbidden_video_path["schedule"][26]["path"] = "fixed.mp4"
        with pytest.raises(LiveTestError, match="trigger fields only"):
            await live_test._validate(forbidden_video_path)

        forbidden_video_text = _request(root)
        forbidden_video_text["schedule"][26]["text"] = "預寫影片提示"
        with pytest.raises(LiveTestError, match="trigger fields only"):
            await live_test._validate(forbidden_video_text)
        await db.close()

    asyncio.run(main())


def test_voice_dispatch_uses_fresh_context_reply_and_empty_context_sends_nothing(
    tmp_path,
):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "voice-dispatch.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        worker = manager.workers[ACCOUNT_IDS[0]]
        voice_evidence = VoiceGenerationEvidence(
            run_id="run",
            event_id="voice-current",
            account_id=ACCOUNT_IDS[0],
            group_id=GROUP_ID,
            trigger_received_at=clock.time(),
            snapshot_at=clock.time(),
            snapshot_sha256="a" * 64,
            profile_id="21",
            text="根據剛剛早餐話題的新回覆",
        )
        worker.generate_realtime_voice_reply = AsyncMock(
            return_value=voice_evidence
        )
        event = {
            "event_id": "voice-current",
            "offset_seconds": 0.0,
            "account_id": ACCOUNT_IDS[0],
            "kind": "voice",
        }

        await live_test._dispatch("run", event, GROUP_ID)

        worker.generate_realtime_voice_reply.assert_awaited_once_with(
            GROUP_ID,
            run_id="run",
            event_id="voice-current",
            trigger_received_at=1_000.0,
        )
        assert worker.dispatches[-1][1:4] == (
            "voice",
            GROUP_ID,
            "voice-voice-current.ogg",
        )

        before = len(worker.dispatches)
        worker.generate_realtime_voice_reply.reset_mock()
        worker.generate_realtime_voice_reply.return_value = None
        with pytest.raises(Exception, match="dispatch failed"):
            await live_test._dispatch(
                "run", dict(event, event_id="voice-no-context"), GROUP_ID
            )
        worker.generate_realtime_voice_reply.assert_awaited_once_with(
            GROUP_ID,
            run_id="run",
            event_id="voice-no-context",
            trigger_received_at=1_000.0,
        )
        assert len(worker.dispatches) == before
        await db.close()

    asyncio.run(main())


def test_realtime_video_uses_latest_context_rechecks_pause_and_has_no_fallback(
    tmp_path,
):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock(now=1_000)
        db = Database(str(tmp_path / "video-dispatch.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        manager.video_client = FakeVideoClient(
            on_generate=lambda: manager.last_human_activity.__setitem__(
                GROUP_ID, clock.time()
            )
        )
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        worker = manager.workers[ACCOUNT_IDS[0]]
        event = {
            "event_id": "video-current-1",
            "offset_seconds": 0.0,
            "account_id": ACCOUNT_IDS[0],
            "kind": "video",
        }

        await live_test._dispatch_realtime_video(
            "run",
            event,
            GROUP_ID,
            account_ids=list(ACCOUNT_IDS),
            scheduled_at=clock.time(),
            expires_at=clock.time() + 600,
        )

        assert len(manager.video_client.calls) == 1
        assert manager.video_client.calls[0]["profile_id"] == 21
        assert manager.video_client.calls[0]["context_prompt"] == "影片回應：早餐蛋餅"
        assert manager.video_client.calls[0]["run_id"] == "run"
        assert manager.video_client.calls[0]["event_id"] == "video-current-1"
        assert manager.video_client.calls[0]["account_id"] == ACCOUNT_IDS[0]
        assert manager.video_client.calls[0]["group_id"] == GROUP_ID
        assert worker.context_events[0][0] == "video_snapshot"
        assert worker.dispatches[-1][1] == "video"
        assert worker.dispatches[-1][0] >= 1_180

        manager.last_human_activity.clear()
        manager.video_client.on_generate = None
        worker.current_context = "剛買咖啡"
        await live_test._dispatch_realtime_video(
            "run",
            dict(event, event_id="video-current-2"),
            GROUP_ID,
            account_ids=list(ACCOUNT_IDS),
            scheduled_at=clock.time(),
            expires_at=clock.time() + 600,
        )
        assert manager.video_client.calls[-1]["profile_id"] == 21
        assert manager.video_client.calls[-1]["context_prompt"] == "影片回應：剛買咖啡"
        assert manager.video_client.calls[-1]["event_id"] == "video-current-2"

        before_sends = len(worker.dispatches)
        before_calls = len(manager.video_client.calls)
        worker.current_context = ""
        with pytest.raises(Exception, match="current context"):
            await live_test._dispatch_realtime_video(
                "run",
                dict(event, event_id="video-no-context"),
                GROUP_ID,
                account_ids=list(ACCOUNT_IDS),
                scheduled_at=clock.time(),
                expires_at=clock.time() + 600,
            )
        assert len(manager.video_client.calls) == before_calls
        assert len(worker.dispatches) == before_sends

        worker.current_context = "服務失敗時不能用舊片"
        manager.video_client.fail = True
        with pytest.raises(Exception, match="generation failed"):
            await live_test._dispatch_realtime_video(
                "run",
                dict(event, event_id="video-gateway-failed"),
                GROUP_ID,
                account_ids=list(ACCOUNT_IDS),
                scheduled_at=clock.time(),
                expires_at=clock.time() + 600,
            )
        assert len(worker.dispatches) == before_sends
        await db.close()

    asyncio.run(main())


def test_fixed_prebuilt_mp4_cannot_reach_video_dispatch(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        (root / "fixed.mp4").write_bytes(
            b"\x00\x00\x00\x18ftypmp42" + b"old" * 100
        )
        clock = FakeClock()
        db = Database(str(tmp_path / "fixed-video.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        worker = manager.workers[ACCOUNT_IDS[0]]

        with pytest.raises(Exception, match="realtime"):
            await live_test._dispatch(
                "run",
                {
                    "event_id": "fixed-video",
                    "offset_seconds": 0.0,
                    "account_id": ACCOUNT_IDS[0],
                    "kind": "video",
                    "path": "fixed.mp4",
                },
                GROUP_ID,
            )
        assert worker.dispatches == []
        await db.close()

    asyncio.run(main())


def test_video_render_runs_in_own_task_without_blocking_text_or_voice(tmp_path):
    class BlockingVideoClient(FakeVideoClient):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().generate(**kwargs)

    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "video-task.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        manager.video_client = BlockingVideoClient()
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        request = _request(root)
        request["schedule"] = sorted(
            request["schedule"],
            key=lambda event: 0 if event["kind"] == "video" else 1,
        )

        await live_test.start(request)
        await asyncio.wait_for(manager.video_client.started.wait(), timeout=1)
        kinds = []
        try:
            for _ in range(100):
                kinds = [
                    dispatch[1]
                    for worker in manager.workers.values()
                    for dispatch in worker.dispatches
                ]
                if "text" in kinds and "voice" in kinds:
                    break
                await asyncio.sleep(0.01)
            assert "text" in kinds
            assert "voice" in kinds
            task = live_test._task
            assert task is not None and not task.done()
        finally:
            manager.video_client.release.set()
        status = await live_test.wait()
        assert status["status"] == "completed", status
        assert status["sent"] == 30
        await db.close()

    asyncio.run(main())


def _request(asset_root, **overrides):
    request = {
        "account_ids": list(ACCOUNT_IDS),
        "group_id": GROUP_ID,
        "duration_seconds": 3_600,
        "event_cap": 40,
        "schedule": _schedule(asset_root),
    }
    request.update(overrides)
    return request


def _video_disabled_request(asset_root, **overrides):
    request = _request(asset_root, video_enabled=False)
    for event in request["schedule"]:
        if event["kind"] == "video":
            event["kind"] = "text"
            event["text"] = "今晚有點想吃鹽酥雞"
    request.update(overrides)
    assert len(request["schedule"]) == 30
    assert not [event for event in request["schedule"] if event["kind"] == "video"]
    return request


def test_video_disabled_scope_requires_zero_video_events_and_no_wan_readiness(
    tmp_path,
):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "video-disabled.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        manager.config.video_realtime_url = ""
        manager.config.video_realtime_token = ""
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=False,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        normalized = await live_test._validate(_video_disabled_request(root))
        assert normalized["video_enabled"] is False
        assert len(normalized["schedule"]) == 30
        assert not [event for event in normalized["schedule"] if event["kind"] == "video"]

        video_leak = _video_disabled_request(root)
        video_leak["schedule"][0] = {
            "event_id": "forbidden-video",
            "offset_seconds": 0,
            "account_id": ACCOUNT_IDS[0],
            "kind": "video",
        }
        with pytest.raises(LiveTestError, match="video events are disabled"):
            await live_test._validate(video_leak)

        wrong_type = _video_disabled_request(root, video_enabled="false")
        with pytest.raises(LiveTestError, match="video_enabled must be a boolean"):
            await live_test._validate(wrong_type)
        await db.close()

    asyncio.run(main())


def test_video_disabled_run_dispatches_exact_non_video_scope_and_stops(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "video-disabled-run.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        manager.config.video_realtime_url = ""
        manager.config.video_realtime_token = ""
        workers = list(manager.workers.values())
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=False,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        started = await live_test.start(_video_disabled_request(root))
        assert started["video_enabled"] is False
        final = await live_test.wait()

        assert final["status"] == "completed", final
        assert final["schedule_count"] == 30
        assert final["video_enabled"] is False
        assert final["reserved"] == 30
        assert final["sent"] == 30
        assert final["failed"] == 0
        assert final["running"] == 0
        dispatches = [entry for worker in workers for entry in worker.dispatches]
        assert Counter(entry[1] for entry in dispatches) == {
            "text": 22,
            "voice": 4,
            "image": 4,
        }
        assert "video" not in [entry[1] for entry in dispatches]
        assert sum(
            len(worker.media_service.vision_calls) for worker in workers
        ) == 2
        assert set(manager.stopped) == set(ACCOUNT_IDS)
        await db.close()

    asyncio.run(main())


def test_live_test_starts_four_configured_stopped_accounts_before_run(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        db = Database(str(tmp_path / "live-test-start-stopped.db"))
        await db.connect()
        await _seed_accounts(db)
        for account_id in ACCOUNT_IDS:
            await db.update_account(account_id, enabled=0)
        clock = FakeClock()
        manager = FakeManager(db, clock)
        manager.workers.clear()
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=False,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        manager.live_test = live_test

        started = await live_test.start(_video_disabled_request(root))
        assert started["status"] == "running"
        assert manager.live_test_start_calls == [(tuple(ACCOUNT_IDS), GROUP_ID)]
        assert manager.live_test_start_gate_scopes == [frozenset()] * 4
        assert set(manager.workers) == set(ACCOUNT_IDS)
        for account_id in ACCOUNT_IDS:
            account = await db.get_account(account_id)
            assert account is not None
            assert account["enabled"] == 1
        final = await live_test.wait()
        assert final["status"] == "completed", final
        assert final["running"] == 0
        await db.close()

    asyncio.run(main())


def test_live_test_partial_account_start_failure_rolls_back_under_lockdown(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        db = Database(str(tmp_path / "live-test-start-rollback.db"))
        await db.connect()
        await _seed_accounts(db)
        for account_id in ACCOUNT_IDS:
            await db.update_account(account_id, enabled=0)
        clock = FakeClock()
        manager = FakeManager(db, clock)
        manager.workers.clear()
        manager.live_test_start_fail_after = 2
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=False,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        manager.live_test = live_test

        with pytest.raises(
            LiveTestError, match="injected live-test account startup failure"
        ):
            await live_test.start(_video_disabled_request(root))

        latest = await db.get_live_test_run()
        assert latest is not None
        assert latest["status"] == "failed"
        assert set(manager.stopped) == set(ACCOUNT_IDS)
        assert manager.workers == {}
        assert manager.live_test_start_gate_scopes == [frozenset()] * 3
        assert live_test.outbound_gate._active is None
        for account_id in ACCOUNT_IDS:
            account = await db.get_account(account_id)
            assert account is not None
            assert account["enabled"] == 0
        await db.close()

    asyncio.run(main())


def test_live_test_startup_rollback_persist_failure_keeps_reconciliation_lockdown(
    tmp_path, monkeypatch
):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        db = Database(str(tmp_path / "live-test-start-persist-failure.db"))
        await db.connect()
        await _seed_accounts(db)
        for account_id in ACCOUNT_IDS:
            await db.update_account(account_id, enabled=0)
        clock = FakeClock()
        manager = FakeManager(db, clock)
        manager.workers.clear()
        manager.live_test_start_fail_after = 2
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=False,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        manager.live_test = live_test
        original_update = db.update_account

        async def fail_first_account_disable(account_id, **values):
            if account_id == ACCOUNT_IDS[0] and values.get("enabled") == 0:
                raise RuntimeError("injected disable persistence failure")
            return await original_update(account_id, **values)

        monkeypatch.setattr(db, "update_account", fail_first_account_disable)

        with pytest.raises(
            LiveTestError, match="injected live-test account startup failure"
        ):
            await live_test.start(_video_disabled_request(root))

        latest = await db.get_live_test_run()
        assert latest is not None
        assert latest["status"] == "needs_reconciliation"
        assert "persist RuntimeError" in latest["stop_reason"]
        assert manager.workers == {}
        assert live_test.outbound_gate._active is not None
        assert live_test.outbound_gate._active[1] == frozenset()
        assert await live_test.start_block_error()

        restarted_manager = FakeManager(db, clock)
        restarted_manager.workers.clear()
        restarted = BoundedLiveTest(
            restarted_manager,
            enabled=True,
            wan22_ready=False,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        assert await restarted.start_block_error()
        await db.close()

    asyncio.run(main())


def test_live_test_entry_requires_exact_cap_and_fixed_30_event_schedule(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "fixed-envelope-entry.db"))
        await db.connect()
        await _seed_accounts(db)
        live_test = BoundedLiveTest(
            FakeManager(db, clock),
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        with pytest.raises(LiveTestError, match="event_cap must be exactly 40"):
            await live_test.start(_request(root, event_cap=39))
        schedule = _schedule(root)
        with pytest.raises(LiveTestError, match="schedule must contain exactly 30"):
            await live_test.start(_request(root, schedule=schedule[:-1]))
        assert await db.get_live_test_run() is None
        await db.close()

    asyncio.run(main())


def test_live_test_dispatches_exact_scope_and_stops_every_account(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "orchestrator.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        workers = list(manager.workers.values())
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        started = await live_test.start(_request(root))
        assert started["status"] == "running"
        final = await live_test.wait()

        assert final["status"] == "completed", final
        assert final["schedule_count"] == 30
        assert final["video_enabled"] is True
        assert final["reserved"] == 30
        assert final["sent"] == 30
        assert final["failed"] == 0
        assert final["running"] == 0
        assert clock.time() >= 4_600
        assert set(manager.stopped) == set(ACCOUNT_IDS)
        dispatches = [entry for worker in workers for entry in worker.dispatches]
        assert Counter(entry[1] for entry in dispatches) == {
            "text": 20,
            "voice": 4,
            "image": 4,
            "video": 2,
        }
        assert sum(
            len(worker.media_service.vision_calls) for worker in workers
        ) == 2
        await db.close()

    asyncio.run(main())


def test_start_preparing_gate_closes_db_running_visibility_window(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "start-linearization.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        real_create = db.create_live_test_run
        running_visible = asyncio.Event()
        release_create = asyncio.Event()

        async def expose_running_window(**kwargs):
            created = await real_create(**kwargs)
            assert created is True
            running_visible.set()
            await release_create.wait()
            return created

        db.create_live_test_run = expose_running_window
        start_task = asyncio.create_task(live_test.start(_request(root)))
        await asyncio.wait_for(running_visible.wait(), timeout=1)
        persisted = await db.get_live_test_run()
        assert persisted is not None and persisted["status"] == "running"

        worker = manager.workers[ACCOUNT_IDS[0]]
        escaped = await worker._send_text_recorded(GROUP_ID, "audit-window")
        rpc_count = len(worker.dispatches)
        during = await db.get_live_test_run(str(persisted["id"]))

        release_create.set()
        started = await start_task
        await live_test.stop("linearization_probe_cleanup")
        await db.close()

        assert started["status"] == "running"
        assert escaped is False
        assert rpc_count == 0
        assert during is not None
        assert during["cap_used"] == 0
        assert during["reserved"] == 0

    asyncio.run(main())


def test_start_create_exception_safely_deactivates_preparing_gate(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "start-create-failure.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        async def fail_create(**kwargs):
            raise RuntimeError("injected create failure")

        db.create_live_test_run = fail_create
        with pytest.raises(RuntimeError, match="injected create failure"):
            await live_test.start(_request(root))

        worker = manager.workers[ACCOUNT_IDS[0]]
        assert await worker._send_text_recorded(GROUP_ID, "normal-after-failure")
        assert len(worker.dispatches) == 1
        await db.close()

    asyncio.run(main())


def test_live_test_pauses_scripted_dispatch_for_180_seconds_after_human_activity(
    tmp_path,
):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock(now=1_000)
        db = Database(str(tmp_path / "pause.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        workers = list(manager.workers.values())
        manager.last_human_activity[GROUP_ID] = 990
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        await live_test.start(_request(root))
        final = await live_test.wait()

        dispatch_times = [
            entry[0] for worker in workers for entry in worker.dispatches
        ]
        assert final["status"] == "completed", final
        assert min(dispatch_times) >= 1_170
        assert clock.sleeps
        assert max(clock.sleeps) <= 60
        await db.close()

    asyncio.run(main())


def test_live_test_expires_without_reserving_when_human_pause_crosses_deadline(
    tmp_path,
):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock(now=1_000)
        db = Database(str(tmp_path / "expiry.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        manager.last_human_activity[GROUP_ID] = 4_599
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        await live_test.start(_request(root))
        final = await live_test.wait()

        assert final["status"] == "expired"
        assert final["reserved"] == 0
        assert final["running"] == 0
        assert set(manager.stopped) == set(ACCOUNT_IDS)
        await db.close()

    asyncio.run(main())


def test_live_test_fails_closed_on_disabled_wrong_accounts_and_unsafe_assets(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "validation.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)

        disabled = BoundedLiveTest(
            manager,
            enabled=False,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        with pytest.raises(LiveTestError, match="disabled"):
            await disabled.start(_request(root))

        enabled = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        with pytest.raises(LiveTestError, match="exactly 3600"):
            await enabled.start(_request(root, duration_seconds=60))

        another_group = _request(root, group_id=GROUP_ID - 1)
        with pytest.raises(LiveTestError, match="fixed live-test group"):
            await enabled.start(another_group)
        private_target = _request(root, group_id=12345)
        with pytest.raises(LiveTestError, match="negative fixed live-test group"):
            await enabled.start(private_target)

        wrong_accounts = _request(root)
        wrong_accounts["account_ids"] = ACCOUNT_IDS[:3] + ["wrong"]
        with pytest.raises(LiveTestError, match="fixed managed accounts"):
            await enabled.start(wrong_accounts)

        unsafe = _request(root)
        unsafe["schedule"][22]["path"] = "../outside.jpg"
        with pytest.raises(LiveTestError, match="asset path"):
            await enabled.start(unsafe)

        missing = _request(root)
        missing["schedule"][22]["path"] = "missing.jpg"
        with pytest.raises(LiveTestError, match="missing local asset"):
            await enabled.start(missing)

        worker = manager.workers[ACCOUNT_IDS[0]]
        worker.is_running = False
        with pytest.raises(LiveTestError, match="not running"):
            await enabled.start(_request(root))
        worker.is_running = True

        first = ACCOUNT_IDS[0]
        original = dict(manager.workers[first].persona)
        mismatched = dict(original, city="高雄")
        manager.workers[first].persona = mismatched
        with pytest.raises(LiveTestError, match="DB and worker persona must match"):
            await enabled.start(_request(root))
        manager.workers[first].persona = original

        second = ACCOUNT_IDS[1]
        original_second = dict(manager.workers[second].persona)
        swapped_first = dict(original, age=ACCOUNT_AGES[second])
        swapped_second = dict(original_second, age=ACCOUNT_AGES[first])
        manager.workers[first].persona = swapped_first
        manager.workers[second].persona = swapped_second
        await db.update_account(
            first, persona=json.dumps(swapped_first, ensure_ascii=False)
        )
        await db.update_account(
            second, persona=json.dumps(swapped_second, ensure_ascii=False)
        )
        with pytest.raises(LiveTestError, match="profile mapping"):
            await enabled.start(_request(root))
        manager.workers[first].persona = original
        manager.workers[second].persona = original_second
        await db.update_account(
            first, persona=json.dumps(original, ensure_ascii=False)
        )
        await db.update_account(
            second, persona=json.dumps(original_second, ensure_ascii=False)
        )

        duplicate_age = _persona(first, age=25)
        manager.workers[first].persona = duplicate_age
        await db.update_account(
            first, persona=json.dumps(duplicate_age, ensure_ascii=False)
        )
        with pytest.raises(LiveTestError, match="profile mapping"):
            await enabled.start(_request(root))

        non_female = _persona(first, gender="男")
        manager.workers[first].persona = non_female
        await db.update_account(
            first, persona=json.dumps(non_female, ensure_ascii=False)
        )
        with pytest.raises(LiveTestError, match="female"):
            await enabled.start(_request(root))
        await db.close()

    asyncio.run(main())


def test_reconcile_stops_all_accounts_from_an_orphaned_persistent_run(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / "reconcile.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = FakeManager(db, clock)
        created = await db.create_live_test_run(
            run_id="orphaned-run",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=40,
            schedule=_schedule(root),
            started_at=clock.time(),
        )
        assert created is True
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        await live_test.reconcile()

        status = await live_test.status("orphaned-run")
        assert status["status"] == "failed"
        assert status["running"] == 0
        assert set(manager.stopped) == set(ACCOUNT_IDS)
        await db.close()

    asyncio.run(main())


def test_expiry_restart_probe_blocks_start_until_reconciled_terminal_expired(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        path = str(tmp_path / "expiry-restart.db")
        first_db = Database(path)
        await first_db.connect()
        await _seed_accounts(first_db)
        schedule = _schedule(root)
        assert await first_db.create_live_test_run(
            run_id="expiry-restart",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=40,
            schedule=schedule,
            started_at=1_000.0,
        )
        assert not await first_db.reserve_live_test_event(
            "expiry-restart",
            schedule[0]["event_id"],
            schedule[0]["account_id"],
            schedule[0]["kind"],
            group_id=GROUP_ID,
            scripted=True,
            now=4_600.001,
        )
        pending = await first_db.get_live_test_run("expiry-restart")
        assert pending["status"] == "needs_reconciliation"
        assert pending["stopped_at"] is None
        assert pending["stop_reason"] == "expired"
        assert await first_db.has_live_test_reconciliation() is True
        await first_db.close()

        restarted_db = Database(path)
        await restarted_db.connect()
        clock = FakeClock(now=4_601.0)
        manager = FakeManager(restarted_db, clock)
        restarted = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        assert await restarted.start_block_error()
        before = await restarted_db.get_live_test_run("expiry-restart")
        assert before["status"] == "needs_reconciliation"

        await restarted.reconcile()

        terminal = await restarted_db.get_live_test_run("expiry-restart")
        assert terminal["status"] == "expired"
        assert terminal["stop_reason"] == "reconciled_expired"
        assert terminal["stopped_at"] is not None
        assert await restarted_db.has_live_test_reconciliation() is False
        assert await restarted.start_block_error() == ""
        assert set(manager.stopped) == set(ACCOUNT_IDS)
        await restarted_db.close()

    asyncio.run(main())


def test_reconcile_drains_all_historical_stop_failed_runs(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        db = Database(str(tmp_path / "historical-lockdowns.db"))
        await db.connect()
        await _seed_accounts(db)
        clock = FakeClock()
        clock.now = 9_000.0
        manager = FakeManager(db, clock)
        for run_id in ("old-one", "old-two"):
            assert await db.create_live_test_run(
                run_id=run_id,
                account_ids=ACCOUNT_IDS,
                group_id=GROUP_ID,
                duration_seconds=3600,
                event_cap=40,
                schedule=_schedule(root),
                started_at=1_000.0,
            )
            assert await db.finish_live_test_run(
                run_id, "failed", "ordinary failure"
            )
        await db._c.execute(
            "UPDATE live_test_runs SET stop_reason='stop_failed: historical'"
        )
        await db._c.commit()

        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            sleep=clock.sleep,
            clock=clock.time,
        )
        await live_test.reconcile()

        assert await db.has_live_test_reconciliation() is False
        assert manager.stopped.count(ACCOUNT_IDS[0]) == 2
        first = await db.get_live_test_run("old-one")
        second = await db.get_live_test_run("old-two")
        assert first["stop_reason"] == "process_restart"
        assert second["stop_reason"] == "process_restart"
        await db.close()

    asyncio.run(main())


@pytest.mark.parametrize("fail_all", [False, True])
def test_stop_failure_keeps_gate_denied_and_never_reports_completed(
    tmp_path, fail_all
):
    class StopFailingManager(FakeManager):
        async def stop(self, account_id):
            self.stopped.append(account_id)
            if fail_all or account_id == ACCOUNT_IDS[0]:
                raise RuntimeError("simulated stop failure")
            worker = self.workers.pop(account_id, None)
            if worker:
                await worker.stop()
            return ""

    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock()
        db = Database(str(tmp_path / f"stop-failure-{fail_all}.db"))
        await db.connect()
        await _seed_accounts(db)
        manager = StopFailingManager(db, clock)
        workers = dict(manager.workers)
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )

        await live_test.start(_request(root))
        final = await live_test.wait()

        assert final["status"] == "needs_reconciliation", final
        assert set(manager.stopped) == set(ACCOUNT_IDS)
        denied_text = await workers[ACCOUNT_IDS[0]]._send_text_recorded(
            GROUP_ID, "不得漏出"
        )
        denied_media = await workers[ACCOUNT_IDS[0]]._send_media_recorded(
            GROUP_ID,
            MediaAsset("image", b"x", "x.jpg", "image/jpeg"),
            "[圖片]",
        )
        assert denied_text is False
        assert denied_media is False
        await db.close()

    asyncio.run(main())


def test_voice_tts_crossing_expiry_never_reaches_send_file(tmp_path):
    async def main():
        root = tmp_path / "assets"
        root.mkdir()
        clock = FakeClock(now=4_599.0)
        db = Database(str(tmp_path / "voice-expiry.db"))
        await db.connect()
        manager = FakeManager(db, clock)
        worker = manager.workers[ACCOUNT_IDS[0]]
        tts_completed = []

        original_send_realtime_voice = worker._send_realtime_voice

        async def delayed_voice(evidence, **kwargs):
            clock.now = 4_601.0
            tts_completed.append(clock.now)
            return await original_send_realtime_voice(evidence, **kwargs)

        worker._send_realtime_voice = delayed_voice
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=root,
            clock=clock.time,
            sleep=clock.sleep,
        )
        event = {
            "event_id": "voice-deadline",
            "offset_seconds": 3_599,
            "account_id": ACCOUNT_IDS[0],
            "kind": "voice",
        }

        with pytest.raises(_RunExpired, match="duration elapsed"):
            await live_test._dispatch(
                "voice-run",
                event,
                GROUP_ID,
                account_ids=ACCOUNT_IDS,
                expires_at=4_600.0,
            )

        assert tts_completed == [4_601.0]
        assert worker.dispatches == []
        await db.close()

    asyncio.run(main())
