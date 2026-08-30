import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.live_test import (
    BoundVideoAsset,
    BoundedLiveTest,
    RealtimeVideoClient,
    _RunFailed,
    _SEMANTIC_THRESHOLDS,
)
from app.media import MediaAsset
from app.worker import AccountWorker, MediaEvidence, VideoContextEvidence


GROUP_ID = -5428680940


class ContextDB:
    def __init__(self, messages, events):
        self.messages = list(messages)
        self.events = events

    async def get_recent_messages(self, account_id, group_id, limit):
        self.events.append(("snapshot", account_id, group_id, limit))
        return deepcopy(self.messages)


def _worker(db):
    config = SimpleNamespace(memory_max_messages=30)
    persona = {
        "name": "小安",
        "gender": "女",
        "age": 29,
        "city": "桃園",
        "district": "中壢",
        "industry": "設計",
        "university": "輔大",
        "personality": "成熟自然",
        "hobbies": ["電影", "早餐"],
        "looking_for": "聊天",
        "meetups_done": 0,
        "schedule": "正常",
        "chat_style": "生活感",
    }
    return AccountWorker(
        account_id="038632e4395b",
        session_key="session",
        tg_api_id=1,
        tg_api_hash="hash",
        ai_client=None,
        db=db,
        config=config,
        managed_ids=set(),
        on_status_change=lambda *args: None,
        persona=persona,
        selected_groups=[GROUP_ID],
    )


def _message(content):
    return {
        "sender_id": 123,
        "sender_name": "真人",
        "role": "user",
        "content": content,
        "timestamp": 1_000.0,
    }


def test_current_context_voice_snapshot_precedes_generation_and_changes_reply():
    async def main():
        events = []
        db = ContextDB([_message("早餐吃蛋餅")], events)
        worker = _worker(db)

        async def generate(system_prompt, context_prompt):
            events.append(("generate", system_prompt, context_prompt))
            if "電影" in context_prompt:
                return "今晚想看電影"
            if "蛋餅" in context_prompt:
                return "我也想吃蛋餅"
            return ""

        worker._call_ai = AsyncMock(side_effect=generate)
        first = await worker.generate_realtime_voice_reply(
            GROUP_ID,
            run_id="context-run",
            event_id="voice-first",
            trigger_received_at=1_000.0,
        )
        db.messages.append(_message("今晚要看電影"))
        second = await worker.generate_realtime_voice_reply(
            GROUP_ID,
            run_id="context-run",
            event_id="voice-second",
            trigger_received_at=1_000.0,
        )

        assert first is not None and second is not None
        first_token, first_text = first
        second_token, second_text = second
        assert first_token != second_token
        assert first_text == "我也想吃蛋餅"
        assert second_text == "今晚想看電影"
        assert [entry[0] for entry in events] == [
            "snapshot",
            "generate",
            "snapshot",
            "generate",
        ]
        assert all("小安" in call.args[0] for call in worker._call_ai.await_args_list)

    asyncio.run(main())


def test_current_context_video_brief_precedes_generation_and_tracks_latest_message():
    async def main():
        events = []
        db = ContextDB([_message("窗外正在下雨")], events)
        worker = _worker(db)

        async def generate(system_prompt, context_prompt):
            events.append(("generate", system_prompt, context_prompt))
            if "咖啡" in context_prompt:
                return "小安拿著咖啡，回應群裡的新咖啡話題"
            if "下雨" in context_prompt:
                return "小安在窗邊聽雨，回應群裡的雨天話題"
            return ""

        worker._call_ai = AsyncMock(side_effect=generate)
        first = await worker.generate_realtime_video_brief(
            GROUP_ID,
            run_id="context-run",
            event_id="video-first",
            trigger_received_at=1_000.0,
        )
        db.messages.append(_message("剛買了一杯咖啡"))
        second = await worker.generate_realtime_video_brief(
            GROUP_ID,
            run_id="context-run",
            event_id="video-second",
            trigger_received_at=1_001.0,
        )

        assert first is not None and second is not None
        assert first.snapshot_sha256 != second.snapshot_sha256
        assert "雨" in first.context_prompt
        assert "咖啡" in second.context_prompt
        assert first.event_id == "video-first"
        assert second.event_id == "video-second"
        assert [entry[0] for entry in events] == [
            "snapshot",
            "generate",
            "snapshot",
            "generate",
        ]

    asyncio.run(main())


def test_current_context_media_generation_fails_closed_without_messages():
    async def main():
        events = []
        worker = _worker(ContextDB([], events))
        worker._call_ai = AsyncMock(return_value="不得生成")

        assert await worker.generate_realtime_voice_reply(
            GROUP_ID,
            run_id="context-run",
            event_id="voice-empty",
            trigger_received_at=1_000.0,
        ) is None
        assert await worker.generate_realtime_video_brief(
            GROUP_ID,
            run_id="context-run",
            event_id="video-empty",
            trigger_received_at=1_000.0,
        ) is None
        worker._call_ai.assert_not_awaited()
        assert [entry[0] for entry in events] == ["snapshot", "snapshot"]

    asyncio.run(main())


WAN_GROUP_ID = -5428680940
WAN_ACCOUNT_ID = "038632e4395b"
WAN_JOB_ID = "e" * 32
WAN_REQUEST_ID = "a" * 32
WAN_VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"fresh" * 100
WAN_REQUEST = {
    "request_id": WAN_REQUEST_ID,
    "run_id": "run-20260830",
    "event_id": "video-event-1",
    "account_id": WAN_ACCOUNT_ID,
    "group_id": WAN_GROUP_ID,
    "trigger_received_at": "2026-08-30T01:00:00+00:00",
    "snapshot_at": "2026-08-30T01:00:01+00:00",
    "snapshot_sha256": "b" * 64,
    "profile_id": 29,
    "context_prompt": "根據最新咖啡話題拍一段自然短片",
}


def _binding_sha256(job_id, request):
    context_hash = hashlib.sha256(request["context_prompt"].encode()).hexdigest()
    encoded = json.dumps(
        {"job_id": job_id, **request, "context_prompt_sha256": context_hash},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _valid_decode_metadata():
    return {
        "schema_version": 1,
        "format": "mp4",
        "codec": "h264",
        "width": 608,
        "height": 896,
        "fps": 8.0,
        "duration_seconds": 4.125,
        "decoded_frames": 33,
        "unique_frame_signatures": 33,
        "motion_pairs": 32,
        "moving_pairs": 32,
        "motion_pair_ratio": 1.0,
        "mean_adjacent_frame_diff": 2.0,
        "first_last_frame_diff": 5.0,
        "start_saturation": 40.0,
        "end_saturation": 40.0,
        "start_brightness": 150.0,
        "end_brightness": 150.0,
        "start_edge_energy": 1.0,
        "end_edge_energy": 1.0,
        "saturation_endpoint_drift_percent": 0.0,
        "brightness_endpoint_drift_percent": 0.0,
        "edge_energy_endpoint_drift_percent": 0.0,
        "max_adjacent_luma_diff": 2.0,
        "max_adjacent_color_diff": 3.0,
        "semantic_quality": {
            "schema_version": 1,
            "passed": True,
            "frame_count": 33,
            "min_face_landmark_points": 70,
            "min_body_recognized_points": 14,
            "min_hand_recognized_points": 30,
            "max_image_feature_distance": 0.3,
            "max_face_feature_distance": 0.2,
            "end_face_feature_distance": 0.1,
            "thresholds": dict(_SEMANTIC_THRESHOLDS),
            "reviewer_sha256": "f" * 64,
        },
    }


def test_decode_metadata_rejects_fractional_float_slots_and_bool_thresholds():
    mutations = [
        lambda value: value.update(motion_pairs=32.5, moving_pairs=32.5),
        lambda value: value.update(unique_frame_signatures=2.5),
        lambda value: value["semantic_quality"].update(
            min_face_landmark_points=60.5
        ),
        lambda value: value.update(decoded_frames=33.0),
        lambda value: value.update(width=608.0),
        lambda value: value.update(schema_version=1.0),
        lambda value: value["semantic_quality"]["thresholds"].update(
            required_face_count_per_frame=True
        ),
        lambda value: value["semantic_quality"]["thresholds"].update(
            min_mean_adjacent_diff=1
        ),
    ]

    for mutate in mutations:
        malformed = _valid_decode_metadata()
        mutate(malformed)
        assert RealtimeVideoClient._decode_metadata_sha256(malformed) is None


def test_decode_metadata_exact_type_and_field_matrix_fails_closed():
    outer_int_fields = (
        "schema_version",
        "width",
        "height",
        "decoded_frames",
        "unique_frame_signatures",
        "motion_pairs",
        "moving_pairs",
    )
    outer_float_fields = (
        "fps",
        "duration_seconds",
        "motion_pair_ratio",
        "mean_adjacent_frame_diff",
        "first_last_frame_diff",
        "start_saturation",
        "end_saturation",
        "start_brightness",
        "end_brightness",
        "start_edge_energy",
        "end_edge_energy",
        "saturation_endpoint_drift_percent",
        "brightness_endpoint_drift_percent",
        "edge_energy_endpoint_drift_percent",
        "max_adjacent_luma_diff",
        "max_adjacent_color_diff",
    )
    semantic_int_fields = (
        "schema_version",
        "frame_count",
        "min_face_landmark_points",
        "min_body_recognized_points",
        "min_hand_recognized_points",
    )
    semantic_float_fields = (
        "max_image_feature_distance",
        "max_face_feature_distance",
        "end_face_feature_distance",
    )

    def rejected(value):
        assert RealtimeVideoClient._decode_metadata_sha256(value) is None

    for key in outer_int_fields:
        for replacement in (float(_valid_decode_metadata()[key]), True, "1"):
            malformed = _valid_decode_metadata()
            malformed[key] = replacement
            rejected(malformed)
    for key in semantic_int_fields:
        original = _valid_decode_metadata()["semantic_quality"][key]
        for replacement in (float(original), True, "1"):
            malformed = _valid_decode_metadata()
            malformed["semantic_quality"][key] = replacement
            rejected(malformed)
    for key in outer_float_fields:
        for replacement in (True, False, float("nan"), float("inf"), float("-inf"), "1.0"):
            malformed = _valid_decode_metadata()
            malformed[key] = replacement
            rejected(malformed)
    for key in semantic_float_fields:
        for replacement in (True, False, float("nan"), float("inf"), float("-inf"), "1.0"):
            malformed = _valid_decode_metadata()
            malformed["semantic_quality"][key] = replacement
            rejected(malformed)
    for key, expected in _SEMANTIC_THRESHOLDS.items():
        alternate_numeric_type = float(expected) if type(expected) is int else int(expected)
        for replacement in (alternate_numeric_type, True, False, "1"):
            malformed = _valid_decode_metadata()
            malformed["semantic_quality"]["thresholds"][key] = replacement
            rejected(malformed)
        if type(expected) is float:
            for replacement in (float("nan"), float("inf"), float("-inf")):
                malformed = _valid_decode_metadata()
                malformed["semantic_quality"]["thresholds"][key] = replacement
                rejected(malformed)

    for path in ("outer", "semantic", "thresholds"):
        malformed = _valid_decode_metadata()
        target = malformed
        if path == "semantic":
            target = malformed["semantic_quality"]
        elif path == "thresholds":
            target = malformed["semantic_quality"]["thresholds"]
        target["unexpected"] = 1
        rejected(malformed)


def _bound_payload(request, status, *, body=WAN_VIDEO, result_url=None):
    payload = {
        "job_id": WAN_JOB_ID,
        "status": status,
        **request,
        "context_prompt_sha256": hashlib.sha256(
            request["context_prompt"].encode()
        ).hexdigest(),
        "binding_sha256": _binding_sha256(WAN_JOB_ID, request),
        "output_sha256": hashlib.sha256(body).hexdigest()
        if status == "complete"
        else None,
        "decode_metadata": _valid_decode_metadata()
        if status == "complete"
        else None,
    }
    if result_url is not None:
        payload["result_url"] = result_url
    return payload


def _result_headers(request, body=WAN_VIDEO):
    return {
        "content-type": "video/mp4",
        "X-SDF-Contract-Version": "1",
        "X-SDF-Request-ID": request["request_id"],
        "X-SDF-Job-ID": WAN_JOB_ID,
        "X-SDF-Run-ID": request["run_id"],
        "X-SDF-Event-ID": request["event_id"],
        "X-SDF-Account-ID": request["account_id"],
        "X-SDF-Group-ID": str(request["group_id"]),
        "X-SDF-Trigger-Received-At": request["trigger_received_at"],
        "X-SDF-Snapshot-At": request["snapshot_at"],
        "X-SDF-Profile-ID": str(request["profile_id"]),
        "X-SDF-Snapshot-SHA256": request["snapshot_sha256"],
        "X-SDF-Context-Prompt-SHA256": hashlib.sha256(
            request["context_prompt"].encode()
        ).hexdigest(),
        "X-SDF-Output-SHA256": hashlib.sha256(body).hexdigest(),
        "X-SDF-Decode-Schema": "1",
    }


class _Response:
    def __init__(self, status_code, *, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.content = content
        self.headers = {} if headers is None else headers

    def json(self):
        return deepcopy(self._payload)


class _ContractHTTPClient:
    def __init__(
        self,
        request=None,
        *,
        submit=None,
        statuses=None,
        result_body=WAN_VIDEO,
        result_headers=None,
    ):
        self.request = dict(request or WAN_REQUEST)
        self.submit = submit or _bound_payload(self.request, "queued")
        self.statuses = list(
            statuses or [_bound_payload(self.request, "complete", body=result_body)]
        )
        self.result_body = result_body
        self.result_headers = result_headers or _result_headers(
            self.request, result_body
        )
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, deepcopy(kwargs)))
        return _Response(202, payload=self.submit)

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, deepcopy(kwargs)))
        if url.endswith(f"/v1/video/jobs/{WAN_JOB_ID}"):
            return _Response(200, payload=self.statuses.pop(0))
        return _Response(
            200, content=self.result_body, headers=deepcopy(self.result_headers)
        )

    async def delete(self, url, **kwargs):
        self.calls.append(("delete", url, deepcopy(kwargs)))
        return _Response(200, payload={"status": "cancelled"})


def _video_config(**overrides):
    values = {
        "vision_model": "gemini-3.5-flash-lite",
        "video_realtime_url": "https://wan.example",
        "video_realtime_token": "wan-token",
        "video_realtime_request_timeout": 3.0,
        "video_realtime_poll_timeout": 20.0,
        "video_realtime_poll_interval": 0.25,
        "video_realtime_download_timeout": 4.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _generate(client):
    return await client.generate(**{key: value for key, value in WAN_REQUEST.items() if key != "request_id"})


def test_realtime_video_client_posts_exact_bound_payload_and_verifies_result():
    async def main():
        http = _ContractHTTPClient(
            statuses=[
                _bound_payload(WAN_REQUEST, "running"),
                _bound_payload(WAN_REQUEST, "complete"),
            ]
        )
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        client = RealtimeVideoClient(
            _video_config(),
            http_client=http,
            sleep=sleep,
            request_id_factory=lambda: WAN_REQUEST_ID,
        )
        asset = await _generate(client)

        assert asset is not None, repr(http.calls)
        assert asset.asset.kind == "video"
        assert asset.asset.filename == f"wan-{WAN_JOB_ID}.mp4"
        assert asset.asset.mime_type == "video/mp4"
        assert asset.asset.data == WAN_VIDEO
        assert asset.media_evidence == MediaEvidence(
            request_id=WAN_REQUEST_ID,
            snapshot_sha256=WAN_REQUEST["snapshot_sha256"],
            output_sha256=hashlib.sha256(WAN_VIDEO).hexdigest(),
            trigger_received_at=datetime.fromisoformat(
                WAN_REQUEST["trigger_received_at"]
            ).timestamp(),
            snapshot_at=datetime.fromisoformat(WAN_REQUEST["snapshot_at"]).timestamp(),
            profile_id=str(WAN_REQUEST["profile_id"]),
            content_sha256=hashlib.sha256(
                WAN_REQUEST["context_prompt"].encode("utf-8")
            ).hexdigest(),
            decode_metadata_sha256=(
                RealtimeVideoClient._decode_metadata_sha256(
                    _valid_decode_metadata()
                )
                or ""
            ),
        )
        assert http.calls[0][0:2] == (
            "post",
            "https://wan.example/v1/video/jobs",
        )
        assert http.calls[0][2]["json"] == WAN_REQUEST
        assert set(http.calls[0][2]["json"]) == {
            "request_id",
            "run_id",
            "event_id",
            "account_id",
            "group_id",
            "trigger_received_at",
            "snapshot_at",
            "snapshot_sha256",
            "profile_id",
            "context_prompt",
        }
        assert sleeps == [0.25]
        assert not [call for call in http.calls if call[0] == "delete"]

    asyncio.run(main())


def test_realtime_video_client_rejects_any_submit_or_status_binding_mismatch():
    wrong_values = {
        "job_id": "f" * 32,
        "request_id": "c" * 32,
        "run_id": "wrong-run",
        "event_id": "wrong-event",
        "account_id": "e63e27a4340d",
        "group_id": WAN_GROUP_ID - 1,
        "trigger_received_at": "2026-08-30T01:00:02+00:00",
        "snapshot_at": "2026-08-30T01:00:03+00:00",
        "snapshot_sha256": "c" * 64,
        "profile_id": 34,
        "context_prompt": "wrong prompt",
        "context_prompt_sha256": "d" * 64,
        "binding_sha256": "f" * 64,
    }

    async def assert_rejected(stage, field, wrong):
        submit = _bound_payload(WAN_REQUEST, "queued")
        status = _bound_payload(WAN_REQUEST, "complete")
        target = submit if stage == "submit" else status
        target[field] = wrong
        http = _ContractHTTPClient(
            submit=submit,
            statuses=[status],
        )
        client = RealtimeVideoClient(
            _video_config(),
            http_client=http,
            request_id_factory=lambda: WAN_REQUEST_ID,
        )
        assert await _generate(client) is None, (stage, field, http.calls)
        deletes = [call for call in http.calls if call[0] == "delete"]
        assert len(deletes) == 1, (stage, field, http.calls)
        assert deletes[0][1].startswith("https://wan.example/v1/video/jobs/")
        assert deletes[0][2]["headers"]["Authorization"] == "Bearer wan-token"

    async def main():
        for stage in ("submit", "status"):
            for field, wrong in wrong_values.items():
                await assert_rejected(stage, field, wrong)

        status = _bound_payload(WAN_REQUEST, "complete")
        status["status"] = "completed"
        http = _ContractHTTPClient(statuses=[status])
        client = RealtimeVideoClient(
            _video_config(),
            http_client=http,
            request_id_factory=lambda: WAN_REQUEST_ID,
        )
        assert await _generate(client) is None
        assert len([call for call in http.calls if call[0] == "delete"]) == 1

    asyncio.run(main())


def test_realtime_video_client_rejects_any_result_header_binding_mismatch():
    wrong_headers = {
        "X-SDF-Contract-Version": "2",
        "X-SDF-Request-ID": "c" * 32,
        "X-SDF-Job-ID": "f" * 32,
        "X-SDF-Run-ID": "wrong-run",
        "X-SDF-Event-ID": "wrong-event",
        "X-SDF-Account-ID": "e63e27a4340d",
        "X-SDF-Group-ID": str(WAN_GROUP_ID - 1),
        "X-SDF-Trigger-Received-At": "2026-08-30T01:00:02+00:00",
        "X-SDF-Snapshot-At": "2026-08-30T01:00:03+00:00",
        "X-SDF-Profile-ID": "34",
        "X-SDF-Snapshot-SHA256": "c" * 64,
        "X-SDF-Context-Prompt-SHA256": "d" * 64,
        "X-SDF-Output-SHA256": "f" * 64,
        "X-SDF-Decode-Schema": "2",
    }

    async def main():
        for header, wrong in wrong_headers.items():
            headers = _result_headers(WAN_REQUEST)
            headers[header] = wrong
            http = _ContractHTTPClient(result_headers=headers)
            client = RealtimeVideoClient(
                _video_config(),
                http_client=http,
                request_id_factory=lambda: WAN_REQUEST_ID,
            )
            assert await _generate(client) is None, (header, http.calls)
            assert len([call for call in http.calls if call[0] == "delete"]) == 1

    asyncio.run(main())


def test_realtime_video_client_rejects_hash_mime_ftyp_and_size_and_cancels():
    async def assert_rejected(body, *, headers=None, status_body=None):
        evidence_body = body if status_body is None else status_body
        http = _ContractHTTPClient(
            statuses=[_bound_payload(WAN_REQUEST, "complete", body=evidence_body)],
            result_body=body,
            result_headers=headers or _result_headers(WAN_REQUEST, evidence_body),
        )
        client = RealtimeVideoClient(
            _video_config(),
            http_client=http,
            request_id_factory=lambda: WAN_REQUEST_ID,
        )
        assert await _generate(client) is None
        assert len([call for call in http.calls if call[0] == "delete"]) == 1

    async def main():
        await assert_rejected(WAN_VIDEO + b"tampered", status_body=WAN_VIDEO)
        bad_mime = _result_headers(WAN_REQUEST)
        bad_mime["content-type"] = "application/octet-stream"
        await assert_rejected(WAN_VIDEO, headers=bad_mime)
        await assert_rejected(b"\x00\x00\x00\x18moovmp42" + b"x" * 100)
        oversized = b"\x00\x00\x00\x18ftypmp42" + b"x" * (50 * 1024 * 1024)
        await assert_rejected(oversized)

    asyncio.run(main())


def test_realtime_video_client_rejects_cross_origin_result_before_download():
    async def main():
        http = _ContractHTTPClient(
            statuses=[
                _bound_payload(
                    WAN_REQUEST,
                    "complete",
                    result_url=f"https://evil.example/v1/video/jobs/{WAN_JOB_ID}/result",
                )
            ]
        )
        client = RealtimeVideoClient(
            _video_config(),
            http_client=http,
            request_id_factory=lambda: WAN_REQUEST_ID,
        )

        assert await _generate(client) is None
        assert not [call for call in http.calls if call[1].startswith("https://evil")]
        deletes = [call for call in http.calls if call[0] == "delete"]
        assert [call[1] for call in deletes] == [
            f"https://wan.example/v1/video/jobs/{WAN_JOB_ID}"
        ]

    asyncio.run(main())


def test_realtime_video_client_total_deadline_covers_submit_poll_and_download():
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    class DeadlineHTTP(_ContractHTTPClient):
        async def post(self, url, **kwargs):
            response = await super().post(url, **kwargs)
            clock.now = 3.5
            return response

        async def get(self, url, **kwargs):
            response = await super().get(url, **kwargs)
            clock.now = 4.5 if url.endswith(WAN_JOB_ID) else 5.1
            return response

    clock = Clock()

    async def run():
        http = DeadlineHTTP()
        client = RealtimeVideoClient(
            _video_config(video_realtime_poll_timeout=5.0),
            http_client=http,
            monotonic=clock,
            request_id_factory=lambda: WAN_REQUEST_ID,
        )
        assert await _generate(client) is None
        non_delete_timeouts = [
            call[2]["timeout"] for call in http.calls if call[0] != "delete"
        ]
        assert non_delete_timeouts == [3.0, 1.5, 0.5]
        assert len([call for call in http.calls if call[0] == "delete"]) == 1

    asyncio.run(run())


def test_bounded_live_test_serializes_two_realtime_video_generations(tmp_path):
    account_ids = ("2ce525dfb0d4", "faa9a202f96e")

    class VideoClient:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.calls = []

        async def generate(self, **kwargs):
            self.calls.append(deepcopy(kwargs))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            evidence = MediaEvidence(
                request_id=("1" if len(self.calls) == 1 else "2") * 32,
                snapshot_sha256=kwargs["snapshot_sha256"],
                output_sha256=hashlib.sha256(WAN_VIDEO).hexdigest(),
                trigger_received_at=datetime.fromisoformat(
                    kwargs["trigger_received_at"]
                ).timestamp(),
                snapshot_at=datetime.fromisoformat(kwargs["snapshot_at"]).timestamp(),
                profile_id=str(kwargs["profile_id"]),
                content_sha256=hashlib.sha256(
                    kwargs["context_prompt"].encode("utf-8")
                ).hexdigest(),
                decode_metadata_sha256=(
                    RealtimeVideoClient._decode_metadata_sha256(
                        _valid_decode_metadata()
                    )
                    or ""
                ),
            )
            return BoundVideoAsset(
                asset=MediaAsset("video", WAN_VIDEO, "fresh.mp4", "video/mp4"),
                media_evidence=evidence,
            )

    class Worker:
        def __init__(self, account_id, age):
            self.account_id = account_id
            self.persona = {"age": age}
            self.is_running = True
            self.tg_client = object()
            self.selected_groups = {WAN_GROUP_ID}
            self.sent = []

        async def generate_realtime_video_brief(
            self,
            group_id,
            *,
            run_id,
            event_id,
            trigger_received_at,
        ):
            assert group_id == WAN_GROUP_ID
            assert run_id == "run-serial"
            assert event_id in {"video-0", "video-1"}
            assert trigger_received_at == 1_000.0
            return VideoContextEvidence(
                run_id=run_id,
                event_id=event_id,
                account_id=self.account_id,
                group_id=group_id,
                trigger_received_at=trigger_received_at,
                snapshot_at=1_001.0,
                snapshot_sha256=hashlib.sha256(
                    self.account_id.encode()
                ).hexdigest(),
                profile_id=int(self.persona["age"]),
                context_prompt=f"fresh context for {self.account_id}",
            )

        async def send_live_test_asset(
            self, group_id, asset, *, event_id, kind, media_evidence, marker=None
        ):
            self.sent.append((group_id, asset, marker, event_id, kind))
            assert isinstance(media_evidence, MediaEvidence)
            return True

    async def main():
        video_client = VideoClient()
        workers = {
            account_ids[0]: Worker(account_ids[0], 21),
            account_ids[1]: Worker(account_ids[1], 25),
        }
        config = _video_config()
        config.media_enabled = True
        config.voice_media_enabled = True
        config.voice_realtime_url = "https://voice.example"
        config.voice_realtime_token = "voice-token"
        manager = SimpleNamespace(
            db=object(),
            config=config,
            workers=workers,
            last_human_activity={},
            video_client=video_client,
        )
        live_test = BoundedLiveTest(
            manager,
            enabled=True,
            wan22_ready=True,
            asset_root=tmp_path,
            clock=lambda: 1_000.0,
            sleep=asyncio.sleep,
        )
        events = [
            {
                "event_id": f"video-{index}",
                "account_id": account_id,
                "kind": "video",
                "offset_seconds": 0.0,
            }
            for index, account_id in enumerate(account_ids)
        ]

        await asyncio.gather(
            *(
                live_test._dispatch_realtime_video(
                    "run-serial",
                    event,
                    WAN_GROUP_ID,
                    account_ids=list(account_ids),
                    scheduled_at=1_000.0,
                    expires_at=1_100.0,
                )
                for event in events
            )
        )

        assert video_client.max_active == 1
        assert len(video_client.calls) == 2
        assert {call["event_id"] for call in video_client.calls} == {
            "video-0",
            "video-1",
        }
        for call in video_client.calls:
            assert call["run_id"] == "run-serial"
            assert call["group_id"] == WAN_GROUP_ID
            assert call["trigger_received_at"] == "1970-01-01T00:16:40+00:00"
            assert call["snapshot_at"] == "1970-01-01T00:16:41+00:00"
            assert len(call["snapshot_sha256"]) == 64
        assert all(len(worker.sent) == 1 for worker in workers.values())

    asyncio.run(main())
