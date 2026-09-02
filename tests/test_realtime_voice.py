import asyncio
import hashlib
import json
import re
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.media import MediaAsset
from app.worker import (
    AccountWorker,
    MediaEvidence,
    VideoContextEvidence,
    VoiceGenerationEvidence,
)


GROUP_ID = -5428680940
# Voice profile IDs (IndexTTS2 OmniVoice clone buckets) — used for evidence/headers.
ACCOUNT_PROFILE_MAP = {
    "2ce525dfb0d4": "21",
    "faa9a202f96e": "25",
    "038632e4395b": "29",
    "e63e27a4340d": "34",
}
# Persona ages (real ages for integrity checking) — separate from voice buckets.
_ACCOUNT_PERSONA_AGES = {
    "2ce525dfb0d4": 28,
    "faa9a202f96e": 27,
    "038632e4395b": 29,
    "e63e27a4340d": 31,
}
REQUEST_ID = "b" * 32
AUDIO = b"OggS" + b"\x00" * 5000
TEXT = "剛剛提到的早餐我也想吃蛋餅"
SNAPSHOT_SHA256 = "a" * 64
VIDEO_CONTEXT = "fresh bound video context"
VIDEO_CONTENT_SHA256 = hashlib.sha256(VIDEO_CONTEXT.encode("utf-8")).hexdigest()
DECODE_METADATA_SHA256 = "d" * 64


class _VoiceDB:
    def __init__(self):
        self.messages = []
        self.activities = []
        self.claims = []
        self.persona: dict | None = None
        self.reconciliation = []
        self.recent_messages = [
            {
                "sender_name": "真人",
                "role": "user",
                "content": "早餐吃蛋餅",
                "timestamp": 999.0,
            }
        ]

    async def claim_daily_voice(self, *args, **kwargs):
        self.claims.append((args, kwargs))
        return True

    async def add_message(self, *args):
        self.messages.append(args)

    async def touch_activity(self, *args):
        self.activities.append(args)

    async def get_recent_messages(self, *_args):
        return list(self.recent_messages)

    async def get_account(self, account_id):
        return {
            "id": account_id,
            "persona": json.dumps(self.persona or {}, ensure_ascii=False),
        }

    async def mark_live_test_needs_reconciliation(self, run_id, detail):
        self.reconciliation.append((run_id, detail))
        return True


def _rt_config(**over):
    base = dict(
        voice_media_enabled=True,
        media_enabled=True,
        voice_realtime_url="https://tunnel.example",
        voice_realtime_token="tok",
        voice_realtime_daily_max=3,
        voice_assets_dir="/nonexistent",
        voice_daily_pre_gen=False,
        memory_max_messages=30,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _rt_worker(
    *,
    account_id: str = "2ce525dfb0d4",
    persona_age: int | None = None,
    config: SimpleNamespace | None = None,
    due: bool = True,
):
    db = _VoiceDB()
    worker = AccountWorker(
        account_id=account_id,
        session_key="session",
        tg_api_id=1,
        tg_api_hash="hash",
        ai_client=None,
        db=db,
        config=config or _rt_config(),
        managed_ids=set(),
        on_status_change=lambda *args: None,
        selected_groups=[GROUP_ID],
        voice_library=None,
    )
    worker.is_running = True
    worker.tg_client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
    worker.tg_user_id = 77
    age = int(persona_age or _ACCOUNT_PERSONA_AGES.get(account_id, 28))
    worker.persona = dict(worker.persona, gender="女", age=age)
    db.persona = dict(worker.persona)
    worker._realtime_voice_due = lambda current: due  # noqa: SLF001
    return worker, db


def _evidence(**over):
    values = dict(
        run_id="run-voice-1",
        event_id="event-voice-1",
        account_id="2ce525dfb0d4",
        group_id=GROUP_ID,
        trigger_received_at=1_000.0,
        snapshot_at=1_001.25,
        snapshot_sha256=SNAPSHOT_SHA256,
        profile_id="21",
        text=TEXT,
    )
    values.update(over)
    return VoiceGenerationEvidence(**values)


def _seed_video_evidence(
    worker,
    media_evidence: MediaEvidence,
    *,
    event_id: str,
    run_id: str = "run-video-1",
):
    worker._pending_live_video_evidence[event_id] = VideoContextEvidence(
        run_id=run_id,
        event_id=event_id,
        account_id=worker.account_id,
        group_id=GROUP_ID,
        trigger_received_at=media_evidence.trigger_received_at,
        snapshot_at=media_evidence.snapshot_at,
        snapshot_sha256=media_evidence.snapshot_sha256,
        profile_id=int(ACCOUNT_PROFILE_MAP[worker.account_id]),
        context_prompt=VIDEO_CONTEXT,
    )


def _response_headers(evidence, *, output: bytes = AUDIO):
    return {
        "content-type": "audio/ogg",
        "X-SDF-Contract-Version": "1",
        "X-SDF-Request-ID": REQUEST_ID,
        "X-SDF-Run-ID": evidence.run_id,
        "X-SDF-Event-ID": evidence.event_id,
        "X-SDF-Account-ID": evidence.account_id,
        "X-SDF-Group-ID": str(evidence.group_id),
        "X-SDF-Trigger-Received-At": str(evidence.trigger_received_at),
        "X-SDF-Snapshot-At": str(evidence.snapshot_at),
        "X-SDF-Profile-ID": evidence.profile_id,
        "X-SDF-Snapshot-SHA256": evidence.snapshot_sha256,
        "X-SDF-Text-SHA256": hashlib.sha256(
            evidence.text.encode("utf-8")
        ).hexdigest(),
        "X-SDF-Output-SHA256": hashlib.sha256(output).hexdigest(),
    }


def _install_http(
    monkeypatch,
    evidence,
    *,
    response_content: bytes = AUDIO,
    headers_output: bytes = AUDIO,
    mutate_headers=None,
    status_code: int = 200,
):
    calls = []

    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.content = response_content
            self.headers = _response_headers(evidence, output=headers_output)
            if mutate_headers is not None:
                mutate_headers(self.headers)

    class _Client:
        def __init__(self, *args, **kwargs):
            self.init = (args, kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr("secrets.token_hex", lambda size: REQUEST_ID)
    return calls


def test_generate_voice_freezes_trigger_snapshot_and_generated_text(monkeypatch):
    async def main():
        worker, db = _rt_worker()
        worker._call_ai = AsyncMock(return_value=TEXT)
        monkeypatch.setattr("app.worker.time.time", lambda: 1_001.25)

        evidence = await worker.generate_realtime_voice_reply(
            GROUP_ID,
            run_id="run-voice-1",
            event_id="event-voice-1",
            trigger_received_at=1_000.0,
        )

        assert evidence is not None
        assert evidence.run_id == "run-voice-1"
        assert evidence.event_id == "event-voice-1"
        assert evidence.account_id == "2ce525dfb0d4"
        assert evidence.group_id == GROUP_ID
        assert evidence.trigger_received_at == 1_000.0
        assert evidence.snapshot_at == 1_001.25
        snapshot = "\x1e".join(
            (
                "2ce525dfb0d4",
                str(GROUP_ID),
                "\x1f".join(("user", "真人", "早餐吃蛋餅", "999.0")),
            )
        )
        assert evidence.snapshot_sha256 == hashlib.sha256(
            snapshot.encode("utf-8")
        ).hexdigest()
        assert evidence.profile_id == "21"
        assert evidence.text == TEXT
        with pytest.raises(FrozenInstanceError):
            evidence.snapshot_sha256 = "0" * 64
        worker._call_ai.assert_awaited_once()
        assert db.messages == []

    asyncio.run(main())


def test_synthesize_posts_exact_evidence_payload_and_returns_immutable_bound_asset(
    monkeypatch,
):
    async def main():
        worker, _ = _rt_worker()
        evidence = _evidence()
        calls = _install_http(monkeypatch, evidence)

        result = await worker._synthesize_realtime_voice(evidence)

        assert len(calls) == 1
        url, kwargs = calls[0]
        assert url == "https://tunnel.example/v1/synthesize"
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}
        assert kwargs["json"] == {
            "request_id": REQUEST_ID,
            "run_id": "run-voice-1",
            "event_id": "event-voice-1",
            "account_id": "2ce525dfb0d4",
            "group_id": GROUP_ID,
            "trigger_received_at": 1_000.0,
            "snapshot_at": 1_001.25,
            "snapshot_sha256": SNAPSHOT_SHA256,
            "profile_id": "21",
            "voice": "21",
            "text": TEXT,
        }
        assert result is not None
        assert result.request_id == REQUEST_ID
        assert result.snapshot_sha256 == SNAPSHOT_SHA256
        assert result.text_sha256 == hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
        assert result.output_sha256 == hashlib.sha256(AUDIO).hexdigest()
        assert result.asset == MediaAsset(
            "voice", AUDIO, f"realtime-21-{REQUEST_ID}.ogg", "audio/ogg"
        )
        with pytest.raises(FrozenInstanceError):
            result.output_sha256 = "0" * 64

    asyncio.run(main())


@pytest.mark.parametrize("account_id,profile_id", ACCOUNT_PROFILE_MAP.items())
def test_all_authorized_accounts_bind_to_exact_profile(
    monkeypatch, account_id, profile_id
):
    async def main():
        worker, _ = _rt_worker(account_id=account_id)
        evidence = _evidence(account_id=account_id, profile_id=profile_id)
        calls = _install_http(monkeypatch, evidence)

        result = await worker._synthesize_realtime_voice(evidence)

        assert result is not None
        assert result.account_id == account_id
        assert result.profile_id == profile_id
        assert calls[0][1]["json"]["voice"] == profile_id
        assert calls[0][1]["json"]["profile_id"] == profile_id

    asyncio.run(main())


EVIDENCE_HEADERS = (
    "X-SDF-Contract-Version",
    "X-SDF-Request-ID",
    "X-SDF-Run-ID",
    "X-SDF-Event-ID",
    "X-SDF-Account-ID",
    "X-SDF-Group-ID",
    "X-SDF-Trigger-Received-At",
    "X-SDF-Snapshot-At",
    "X-SDF-Profile-ID",
    "X-SDF-Snapshot-SHA256",
    "X-SDF-Text-SHA256",
    "X-SDF-Output-SHA256",
)


@pytest.mark.parametrize("header_name", EVIDENCE_HEADERS)
def test_synthesize_rejects_each_mismatched_evidence_header(monkeypatch, header_name):
    async def main():
        worker, _ = _rt_worker()
        evidence = _evidence()

        def mismatch(headers):
            headers[header_name] = "wrong"

        _install_http(monkeypatch, evidence, mutate_headers=mismatch)
        assert await worker._synthesize_realtime_voice(evidence) is None
        assert worker.stats["voice_realtime_errors"] == 1

    asyncio.run(main())


def test_synthesize_rejects_non_ogg_content_type(monkeypatch):
    async def main():
        worker, _ = _rt_worker()
        evidence = _evidence()

        def mismatch(headers):
            headers["content-type"] = "audio/wav"

        _install_http(monkeypatch, evidence, mutate_headers=mismatch)
        assert await worker._synthesize_realtime_voice(evidence) is None

    asyncio.run(main())


def test_synthesize_rejects_body_whose_hash_does_not_match_output_header(monkeypatch):
    async def main():
        worker, _ = _rt_worker()
        evidence = _evidence()
        changed_audio = b"OggS" + b"\x01" * 5000
        _install_http(
            monkeypatch,
            evidence,
            response_content=changed_audio,
            headers_output=AUDIO,
        )

        assert await worker._synthesize_realtime_voice(evidence) is None
        assert worker.stats["voice_realtime_errors"] == 1

    asyncio.run(main())


@pytest.mark.parametrize(
    "worker_account,evidence_overrides",
    [
        ("unknown-account", {"account_id": "unknown-account"}),
        ("2ce525dfb0d4", {"account_id": "faa9a202f96e"}),
        ("2ce525dfb0d4", {"profile_id": "25"}),
        ("2ce525dfb0d4", {"group_id": -1009}),
    ],
    ids=("unknown-account", "wrong-account", "wrong-profile", "wrong-group"),
)
def test_synthesize_rejects_identity_or_group_mismatch_without_http(
    monkeypatch, worker_account, evidence_overrides
):
    async def main():
        worker, _ = _rt_worker(account_id=worker_account)
        attempts = []

        class _ForbiddenClient:
            def __init__(self, *args, **kwargs):
                attempts.append((args, kwargs))
                raise RuntimeError("HTTP must not be reached")

        monkeypatch.setattr(httpx, "AsyncClient", _ForbiddenClient)
        result = await worker._synthesize_realtime_voice(
            _evidence(**evidence_overrides)
        )
        assert result is None
        assert attempts == []

    asyncio.run(main())


def test_old_text_only_synthesis_and_two_argument_send_are_rejected(monkeypatch):
    async def main():
        worker, _ = _rt_worker()
        calls = _install_http(monkeypatch, _evidence())

        assert await worker._synthesize_realtime_voice("舊兩字段呼叫") is None
        assert calls == []
        worker._synthesize_realtime_voice = AsyncMock(
            return_value=MediaAsset("voice", AUDIO, "legacy.ogg", "audio/ogg")
        )
        with pytest.raises(TypeError):
            await worker._send_realtime_voice(GROUP_ID, "舊兩字段呼叫")
        worker.tg_client.send_file.assert_not_awaited()

    asyncio.run(main())


def test_realtime_voice_send_passes_immutable_bound_evidence_before_final_rpc(
    monkeypatch,
):
    async def main():
        worker, db = _rt_worker()
        evidence = _evidence()
        _install_http(monkeypatch, evidence)
        order = []
        seen = []

        async def before_send(bound):
            order.append("authorize")
            seen.append(bound)
            assert bound.run_id == evidence.run_id
            assert bound.event_id == evidence.event_id
            assert bound.group_id == evidence.group_id
            assert bound.snapshot_sha256 == evidence.snapshot_sha256
            assert bound.output_sha256 == hashlib.sha256(AUDIO).hexdigest()
            return True

        async def send_file(*args, **kwargs):
            order.append("send_file")

        worker.tg_client.send_file = AsyncMock(side_effect=send_file)
        result = await worker._send_realtime_voice(
            evidence,
            live_test_event_id=evidence.event_id,
            live_test_kind="voice",
            before_send=before_send,
        )

        assert result is seen[0]
        assert order == ["authorize", "send_file"]
        assert worker.tg_client.send_file.await_count == 1
        args = worker.tg_client.send_file.await_args.args
        assert args[0] == GROUP_ID
        assert args[1].getvalue() == AUDIO
        assert worker.tg_client.send_file.await_args.kwargs["voice_note"] is True
        assert worker._realtime_voice_today == 1
        assert db.activities == [
            ("2ce525dfb0d4", GROUP_ID, "voice_realtime")
        ]

    asyncio.run(main())


def test_realtime_voice_callback_failure_after_tts_blocks_send(monkeypatch):
    async def main():
        worker, _db = _rt_worker()
        evidence = _evidence()
        _install_http(monkeypatch, evidence)
        before_send = AsyncMock(side_effect=RuntimeError("expired"))

        with pytest.raises(RuntimeError, match="expired"):
            await worker._send_realtime_voice(
                evidence,
                live_test_event_id=evidence.event_id,
                live_test_kind="voice",
                before_send=before_send,
            )

        before_send.assert_awaited_once()
        worker.tg_client.send_file.assert_not_awaited()
        assert worker._realtime_voice_today == 0

    asyncio.run(main())


def test_send_live_test_asset_passes_exact_media_evidence_into_gate_permit():
    class Gate:
        def __init__(self):
            self.reserved = None
            self.completed = []
            self.rpc_started = []
            self._active = (
                "run-video-1",
                frozenset({"2ce525dfb0d4"}),
                GROUP_ID,
                float("inf"),
                1,
            )

        async def reserve(self, **kwargs):
            self.reserved = kwargs
            return SimpleNamespace(
                allowed=True,
                run_id="run-video-1",
                **kwargs,
            )

        def validate(self, permit, **_kwargs):
            return permit.request_id == REQUEST_ID

        async def mark_rpc_started(self, permit):
            self.rpc_started.append(permit)
            return True

        async def complete(self, permit, *, sent, rpc_started=False, detail=""):
            self.completed.append((permit, sent, detail, rpc_started))
            assert permit.snapshot_sha256 == SNAPSHOT_SHA256
            assert permit.output_sha256 == hashlib.sha256(AUDIO).hexdigest()
            return True

    async def main():
        worker, _db = _rt_worker()
        gate = Gate()
        worker.outbound_gate = gate
        media_evidence = MediaEvidence(
            request_id=REQUEST_ID,
            snapshot_sha256=SNAPSHOT_SHA256,
            output_sha256=hashlib.sha256(AUDIO).hexdigest(),
            trigger_received_at=1_000.0,
            snapshot_at=1_001.25,
            profile_id="21",
            content_sha256=VIDEO_CONTENT_SHA256,
            decode_metadata_sha256=DECODE_METADATA_SHA256,
        )
        with pytest.raises(FrozenInstanceError):
            media_evidence.request_id = "0" * 32
        _seed_video_evidence(
            worker, media_evidence, event_id="video-event"
        )

        assert await worker.send_live_test_asset(
            GROUP_ID,
            MediaAsset("video", AUDIO, "wan.mp4", "video/mp4"),
            event_id="video-event",
            kind="video",
            media_evidence=media_evidence,
        )
        assert gate.reserved == {
            "account_id": "2ce525dfb0d4",
            "group_id": GROUP_ID,
            "kind": "video",
            "event_id": "video-event",
            "request_id": REQUEST_ID,
            "snapshot_sha256": SNAPSHOT_SHA256,
            "output_sha256": hashlib.sha256(AUDIO).hexdigest(),
            "trigger_received_at": 1_000.0,
            "snapshot_at": 1_001.25,
            "profile_id": "21",
            "content_sha256": VIDEO_CONTENT_SHA256,
            "decode_metadata_sha256": DECODE_METADATA_SHA256,
        }
        assert len(gate.rpc_started) == 1
        assert gate.completed[0][1:] == (True, "", False)
        worker.tg_client.send_file.assert_awaited_once()

    asyncio.run(main())


def test_ordinary_twelve_percent_reply_path_never_calls_realtime_tts(monkeypatch):
    async def main():
        worker, _db = _rt_worker()
        event = SimpleNamespace(
            sender_id=999,
            chat_id=GROUP_ID,
            raw_text="早餐吃什麼",
            media=None,
        )
        worker._generate_reply = AsyncMock(return_value="我今天吃蛋餅")
        worker._send_text_recorded = AsyncMock(return_value=True)
        worker._send_realtime_voice = AsyncMock(return_value=object())
        worker._audit_reply = AsyncMock()
        monkeypatch.setattr("app.worker.random.random", lambda: 0.0)

        await worker._reply_later(event, 0)

        worker._send_realtime_voice.assert_not_awaited()
        worker._send_text_recorded.assert_awaited_once()

    asyncio.run(main())


def test_requested_video_never_uses_legacy_media_service_but_image_is_preserved():
    async def main():
        worker, _db = _rt_worker()
        image = MediaAsset("image", b"jpg", "fresh.jpg", "image/jpeg")
        video = MediaAsset("video", b"mp4", "legacy.mp4", "video/mp4")
        service = SimpleNamespace(
            generate_image=AsyncMock(return_value=image),
            generate_video=AsyncMock(return_value=video),
        )
        worker.media_service = service
        event = SimpleNamespace(raw_text="拍一段早餐短片")

        assert await worker._generate_requested_media(event, "video") is None
        service.generate_video.assert_not_awaited()
        assert await worker._generate_requested_media(event, "image") == image
        service.generate_image.assert_awaited_once()

    asyncio.run(main())


def test_realtime_voice_requires_enabled_config_and_quota(monkeypatch):
    current = 10_000.0
    worker, _ = _rt_worker()
    worker._realtime_voice_due = AccountWorker._realtime_voice_due.__get__(worker)
    monkeypatch.setattr(worker, "_hkt_second_of_day", lambda _current: 18 * 3600)
    assert worker._realtime_voice_due(current) is True
    worker._realtime_voice_today = 3
    assert worker._realtime_voice_due(current) is False

    no_url, _ = _rt_worker(config=_rt_config(voice_realtime_url=""))
    no_url._realtime_voice_due = AccountWorker._realtime_voice_due.__get__(no_url)
    monkeypatch.setattr(no_url, "_hkt_second_of_day", lambda _current: 18 * 3600)
    assert no_url._realtime_voice_due(current) is False


def test_synthesize_realtime_voice_fails_closed_on_http_and_short_body(monkeypatch):
    async def main():
        worker, _ = _rt_worker()
        evidence = _evidence()
        _install_http(monkeypatch, evidence, status_code=503, response_content=b"")
        assert await worker._synthesize_realtime_voice(evidence) is None
        assert worker.stats["voice_realtime_errors"] == 1

        _install_http(monkeypatch, evidence, response_content=b"OggS123")
        assert await worker._synthesize_realtime_voice(evidence) is None
        assert worker.stats["voice_realtime_errors"] == 2

    asyncio.run(main())


def test_generated_request_id_shape_is_exact(monkeypatch):
    async def main():
        worker, _ = _rt_worker()
        evidence = _evidence()
        calls = _install_http(monkeypatch, evidence)
        await worker._synthesize_realtime_voice(evidence)
        request_id = calls[0][1]["json"].get("request_id", "")
        assert re.fullmatch(r"[0-9a-f]{32}", request_id)

    asyncio.run(main())


class _FinalMediaGate:
    def __init__(self, *, output_sha256, mutate_asset=None, permit_overrides=None):
        self._active = (
            "run-video-1",
            frozenset({"2ce525dfb0d4"}),
            GROUP_ID,
            float("inf"),
            1,
        )
        self.output_sha256 = output_sha256
        self.mutate_asset = mutate_asset
        self.permit_overrides = permit_overrides or {}
        self.reserved = []
        self.completed = []
        self.rpc_started = []
        self.released = []
        self.lockdowns = []

    async def reserve(self, **kwargs):
        self.reserved.append(kwargs)
        if self.mutate_asset is not None:
            self.mutate_asset()
        values = {
            "allowed": True,
            "tracked": True,
            "run_id": "run-video-1",
            "event_id": kwargs["event_id"],
            "account_id": kwargs["account_id"],
            "group_id": kwargs["group_id"],
            "kind": kwargs["kind"],
            "trigger_received_at": kwargs["trigger_received_at"],
            "snapshot_at": kwargs["snapshot_at"],
            "profile_id": kwargs["profile_id"],
            "content_sha256": kwargs["content_sha256"],
            "decode_metadata_sha256": kwargs["decode_metadata_sha256"],
            "request_id": kwargs["request_id"],
            "snapshot_sha256": kwargs["snapshot_sha256"],
            "output_sha256": self.output_sha256,
        }
        values.update(self.permit_overrides)
        return SimpleNamespace(**values)

    def validate(self, _permit, **_kwargs):
        return True

    async def mark_rpc_started(self, permit):
        self.rpc_started.append(permit)
        return True

    async def release_bound(self, **kwargs):
        self.released.append(kwargs)
        return True

    async def complete(self, permit, *, sent, rpc_started=False, detail=""):
        self.completed.append((permit, sent, detail, rpc_started))
        return True

    def lockdown(self, run_id):
        self.lockdowns.append(run_id)
        return True


def test_final_send_file_rehashes_current_raw_bytes_after_reservation():
    async def main():
        worker, _db = _rt_worker()
        original = b"\x00\xffOggS-bound-original"
        replacement = b"\x80\x81OggS-tampered-after-reserve"
        asset = MediaAsset("video", original, "wan.mp4", "video/mp4")
        gate = _FinalMediaGate(
            output_sha256=hashlib.sha256(original).hexdigest(),
            mutate_asset=lambda: object.__setattr__(asset, "data", replacement),
        )
        worker.outbound_gate = gate
        evidence = MediaEvidence(
            request_id=REQUEST_ID,
            snapshot_sha256=SNAPSHOT_SHA256,
            output_sha256=hashlib.sha256(original).hexdigest(),
            trigger_received_at=1_000.0,
            snapshot_at=1_001.25,
            profile_id="21",
            content_sha256=VIDEO_CONTENT_SHA256,
            decode_metadata_sha256=DECODE_METADATA_SHA256,
        )
        _seed_video_evidence(worker, evidence, event_id="event-video-1")

        assert await worker.send_live_test_asset(
            GROUP_ID,
            asset,
            event_id="event-video-1",
            kind="video",
            media_evidence=evidence,
        ) is False
        worker.tg_client.send_file.assert_not_awaited()
        assert len(gate.reserved) == 1
        assert len(gate.rpc_started) == 1
        assert gate.completed[0][1:] == (
            False,
            "bound raw-byte SHA256 mismatch after reservation; "
            "attempt conservatively consumed",
            True,
        )

    asyncio.run(main())


def test_final_send_file_rejects_zero_output_hash_before_reservation():
    async def main():
        worker, _db = _rt_worker()
        asset = MediaAsset("video", AUDIO, "wan.mp4", "video/mp4")
        gate = _FinalMediaGate(output_sha256="0" * 64)
        worker.outbound_gate = gate
        evidence = MediaEvidence(
            request_id=REQUEST_ID,
            snapshot_sha256=SNAPSHOT_SHA256,
            output_sha256="0" * 64,
            trigger_received_at=1_000.0,
            snapshot_at=1_001.25,
            profile_id="21",
            content_sha256=VIDEO_CONTENT_SHA256,
            decode_metadata_sha256=DECODE_METADATA_SHA256,
        )
        _seed_video_evidence(worker, evidence, event_id="event-video-1")

        assert await worker.send_live_test_asset(
            GROUP_ID,
            asset,
            event_id="event-video-1",
            kind="video",
            media_evidence=evidence,
        ) is False
        worker.tg_client.send_file.assert_not_awaited()
        assert gate.reserved == []
        assert gate.completed == []

    asyncio.run(main())


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("run_id", "wrong-run"),
        ("event_id", "wrong-event"),
        ("account_id", "faa9a202f96e"),
        ("group_id", GROUP_ID - 1),
        ("kind", "voice"),
        ("trigger_received_at", 999.0),
        ("snapshot_at", 1_002.0),
        ("profile_id", "25"),
        ("content_sha256", "c" * 64),
        ("decode_metadata_sha256", "c" * 64),
        ("request_id", "c" * 32),
        ("snapshot_sha256", "c" * 64),
        ("output_sha256", "c" * 64),
    ],
)
def test_final_send_file_cross_checks_every_gate_permit_field(field, wrong_value):
    async def main():
        worker, _db = _rt_worker()
        output_sha256 = hashlib.sha256(AUDIO).hexdigest()
        gate = _FinalMediaGate(
            output_sha256=output_sha256,
            permit_overrides={field: wrong_value},
        )
        worker.outbound_gate = gate
        evidence = MediaEvidence(
            request_id=REQUEST_ID,
            snapshot_sha256=SNAPSHOT_SHA256,
            output_sha256=output_sha256,
            trigger_received_at=1_000.0,
            snapshot_at=1_001.25,
            profile_id="21",
            content_sha256=VIDEO_CONTENT_SHA256,
            decode_metadata_sha256=DECODE_METADATA_SHA256,
        )
        _seed_video_evidence(worker, evidence, event_id="event-video-1")

        assert await worker.send_live_test_asset(
            GROUP_ID,
            MediaAsset("video", AUDIO, "wan.mp4", "video/mp4"),
            event_id="event-video-1",
            kind="video",
            media_evidence=evidence,
        ) is False
        worker.tg_client.send_file.assert_not_awaited()
        assert len(gate.released) == 1
        assert gate.released[0]["run_id"] == "run-video-1"
        assert gate.released[0]["event_id"] == "event-video-1"

    asyncio.run(main())


@pytest.mark.parametrize("tamper_target", ["db", "worker"])
def test_fixed_persona_tamper_fails_before_reserve_and_enters_reconciliation(
    tamper_target,
):
    async def main():
        worker, db = _rt_worker()
        output_sha256 = hashlib.sha256(AUDIO).hexdigest()
        gate = _FinalMediaGate(output_sha256=output_sha256)
        worker.outbound_gate = gate
        if tamper_target == "db":
            db.persona = dict(db.persona or {}, age=25)
        else:
            worker.persona = dict(worker.persona, age=25)
        evidence = MediaEvidence(
            request_id=REQUEST_ID,
            snapshot_sha256=SNAPSHOT_SHA256,
            output_sha256=output_sha256,
            trigger_received_at=1_000.0,
            snapshot_at=1_001.25,
            profile_id="21",
            content_sha256=VIDEO_CONTENT_SHA256,
            decode_metadata_sha256=DECODE_METADATA_SHA256,
        )
        _seed_video_evidence(worker, evidence, event_id="event-video-1")

        assert await worker.send_live_test_asset(
            GROUP_ID,
            MediaAsset("video", AUDIO, "wan.mp4", "video/mp4"),
            event_id="event-video-1",
            kind="video",
            media_evidence=evidence,
        ) is False
        worker.tg_client.send_file.assert_not_awaited()
        assert gate.reserved == []
        assert gate.lockdowns == ["run-video-1"]
        assert db.reconciliation

    asyncio.run(main())
