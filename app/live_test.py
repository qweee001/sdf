from __future__ import annotations

import asyncio
import hashlib
import hmac

import json
import math
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast
from urllib.parse import urljoin, urlsplit

import httpx

from .media import APPROVED_VISION_MODEL, MediaAsset
from .persona import get_system_prompt

if TYPE_CHECKING:
    from .worker import MediaEvidence


_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_ASSET_BYTES = 50 * 1024 * 1024
_HUMAN_PAUSE_SECONDS = 180.0
_MONITOR_SECONDS = 60.0
_FIXED_LIVE_TEST_GROUP_ID = -5428680940
_FIXED_LIVE_TEST_ACCOUNT_IDS = frozenset(
    {
        "2ce525dfb0d4",
        "faa9a202f96e",
        "038632e4395b",
        "e63e27a4340d",
    }
)
_FIXED_PERSONA_AGES = {21, 25, 29, 34}
_ACCOUNT_PROFILE_MAP = {
    "2ce525dfb0d4": 21,
    "faa9a202f96e": 25,
    "038632e4395b": 29,
    "e63e27a4340d": 34,
}
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VISIBLE_ASCII = re.compile(r"^[!-~]{1,128}$")
_WAN_STATUSES = frozenset({"queued", "running", "complete", "failed", "cancelled"})
_DECODE_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "format",
        "codec",
        "width",
        "height",
        "fps",
        "duration_seconds",
        "decoded_frames",
        "unique_frame_signatures",
        "motion_pairs",
        "moving_pairs",
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
        "semantic_quality",
    }
)
_SEMANTIC_QUALITY_FIELDS = frozenset(
    {
        "schema_version",
        "passed",
        "frame_count",
        "min_face_landmark_points",
        "min_body_recognized_points",
        "min_hand_recognized_points",
        "max_image_feature_distance",
        "max_face_feature_distance",
        "end_face_feature_distance",
        "thresholds",
        "reviewer_sha256",
    }
)
_SEMANTIC_THRESHOLDS = {
    "required_face_count_per_frame": 1,
    "required_body_count_per_frame": 1,
    "required_hand_count_per_frame": 2,
    "min_face_landmark_points": 60,
    "min_body_recognized_points": 14,
    "min_hand_recognized_points": 30,
    "max_image_feature_distance_from_first": 0.45,
    "max_face_feature_distance_from_first": 0.60,
    "max_face_feature_distance_at_end": 0.25,
    "min_image_feature_motion": 0.15,
    "min_mean_adjacent_diff": 1.0,
}


class LiveTestError(ValueError):
    """A bounded live-test request failed closed before it could dispatch."""


class _RunExpired(Exception):
    pass


class _RunFailed(Exception):
    pass


@dataclass(frozen=True, slots=True)
class VideoGenerationRequest:
    """Exact trigger/snapshot envelope accepted by the Wan evidence contract."""

    run_id: str
    event_id: str
    account_id: str
    group_id: int
    trigger_received_at: str
    snapshot_at: str
    snapshot_sha256: str
    profile_id: int
    context_prompt: str


@dataclass(frozen=True, slots=True)
class BoundVideoAsset:
    """Verified MP4 plus the immutable evidence required at the send gate."""

    asset: MediaAsset
    media_evidence: MediaEvidence


class RealtimeVideoClient:
    """Create one evidence-bound Wan MP4 for one current-context trigger."""

    def __init__(
        self,
        config: Any,
        *,
        http_client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        request_id_factory: Callable[[], str] | None = None,
    ):
        self.base_url = str(getattr(config, "video_realtime_url", "") or "").rstrip(
            "/"
        )
        self.token = str(getattr(config, "video_realtime_token", "") or "")
        self.request_timeout = min(
            max(float(getattr(config, "video_realtime_request_timeout", 30.0)), 1.0),
            60.0,
        )
        # This is one total wall-clock budget, not a fresh poll-only budget.
        self.total_timeout = min(
            max(float(getattr(config, "video_realtime_poll_timeout", 300.0)), 1.0),
            600.0,
        )
        self.poll_interval = min(
            max(float(getattr(config, "video_realtime_poll_interval", 2.0)), 0.1),
            10.0,
        )
        self.download_timeout = min(
            max(float(getattr(config, "video_realtime_download_timeout", 60.0)), 1.0),
            120.0,
        )
        self._http_client = http_client
        self._sleep = sleep
        self._monotonic = monotonic
        self._request_id_factory = request_id_factory or (lambda: uuid.uuid4().hex)

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            return None
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed

    @staticmethod
    def _decode_metadata_sha256(value: Any) -> str | None:
        if not isinstance(value, dict) or set(value) != _DECODE_METADATA_FIELDS:
            return None
        semantic = value.get("semantic_quality")
        if not isinstance(semantic, dict) or set(semantic) != _SEMANTIC_QUALITY_FIELDS:
            return None
        strict_integer_fields = (
            "schema_version",
            "width",
            "height",
            "decoded_frames",
            "unique_frame_signatures",
            "motion_pairs",
            "moving_pairs",
        )
        strict_semantic_integer_fields = (
            "schema_version",
            "frame_count",
            "min_face_landmark_points",
            "min_body_recognized_points",
            "min_hand_recognized_points",
        )
        if any(type(value.get(key)) is not int for key in strict_integer_fields):
            return None
        if any(
            type(semantic.get(key)) is not int
            for key in strict_semantic_integer_fields
        ):
            return None
        thresholds = semantic.get("thresholds")
        if not isinstance(thresholds, dict) or set(thresholds) != set(
            _SEMANTIC_THRESHOLDS
        ):
            return None
        for key, expected in _SEMANTIC_THRESHOLDS.items():
            actual = thresholds.get(key)
            if type(expected) is int:
                if type(actual) is not int or actual != expected:
                    return None
            elif (
                type(actual) is not float
                or not math.isfinite(actual)
                or actual != expected
            ):
                return None
        if (
            value.get("schema_version") != 1
            or value.get("format") != "mp4"
            or value.get("codec") != "h264"
            or value.get("width") != 608
            or value.get("height") != 896
            or value.get("decoded_frames") != 33
            or semantic.get("schema_version") != 1
            or semantic.get("passed") is not True
            or semantic.get("frame_count") != 33
            or semantic.get("thresholds") != _SEMANTIC_THRESHOLDS
            or not isinstance(semantic.get("reviewer_sha256"), str)
            or _SHA256.fullmatch(semantic["reviewer_sha256"]) is None
        ):
            return None
        numeric_values = [
            value.get(key)
            for key in (
                "fps",
                "duration_seconds",
                "unique_frame_signatures",
                "motion_pairs",
                "moving_pairs",
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
        ] + [
            semantic.get(key)
            for key in (
                "min_face_landmark_points",
                "min_body_recognized_points",
                "min_hand_recognized_points",
                "max_image_feature_distance",
                "max_face_feature_distance",
                "end_face_feature_distance",
            )
        ]
        if any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
            for number in numeric_values
        ):
            return None
        if (
            float(value["fps"]) != 8.0
            or abs(float(value["duration_seconds"]) - 4.125) > 0.125
            or int(value["unique_frame_signatures"]) < 2
            or int(value["motion_pairs"]) != 32
            or int(value["moving_pairs"]) < 0
            or int(value["moving_pairs"]) > 32
            or abs(
                float(value["motion_pair_ratio"])
                - (float(value["moving_pairs"]) / float(value["motion_pairs"]))
            ) > 1e-9
            or float(value["motion_pair_ratio"]) < 0.5
            or float(value["mean_adjacent_frame_diff"]) < 1.0
            or float(value["first_last_frame_diff"]) < 0.5
            or abs(float(value["saturation_endpoint_drift_percent"])) > 20.0
            or abs(float(value["brightness_endpoint_drift_percent"])) > 15.0
            or float(value["edge_energy_endpoint_drift_percent"]) < -40.0
            or float(value["max_adjacent_luma_diff"]) > 15.0
            or float(value["max_adjacent_color_diff"]) > 25.0
            or int(semantic["min_face_landmark_points"]) < 60
            or int(semantic["min_body_recognized_points"]) < 14
            or int(semantic["min_hand_recognized_points"]) < 30
            or float(semantic["max_image_feature_distance"]) > 0.45
            or float(semantic["max_face_feature_distance"]) > 0.60
            or float(semantic["end_face_feature_distance"]) > 0.25
        ):
            return None
        try:
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(canonical).hexdigest()

    def _valid_request(self, request: VideoGenerationRequest) -> bool:
        trigger = self._timestamp(request.trigger_received_at)
        snapshot = self._timestamp(request.snapshot_at)
        return (
            bool(self.base_url)
            and bool(self.token)
            and all(
                isinstance(value, str) and _VISIBLE_ASCII.fullmatch(value) is not None
                for value in (request.run_id, request.event_id, request.account_id)
            )
            and type(request.group_id) is int
            and request.group_id == _FIXED_LIVE_TEST_GROUP_ID
            and type(request.profile_id) is int
            and _ACCOUNT_PROFILE_MAP.get(request.account_id) == request.profile_id
            and isinstance(request.snapshot_sha256, str)
            and _SHA256.fullmatch(request.snapshot_sha256) is not None
            and isinstance(request.context_prompt, str)
            and bool(request.context_prompt.strip())
            and len(request.context_prompt) <= 2000
            and "\x00" not in request.context_prompt
            and trigger is not None
            and snapshot is not None
            and snapshot >= trigger
            and self._same_origin(self.base_url)
        )

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int] | None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme, parsed.hostname.lower(), port

    def _same_origin(self, url: str) -> bool:
        expected = self._origin(self.base_url)
        candidate = self._origin(url)
        return expected is not None and candidate == expected

    @staticmethod
    def _binding_digest(job_id: str, payload: dict[str, Any]) -> str:
        context_prompt_sha256 = hashlib.sha256(
            payload["context_prompt"].encode("utf-8")
        ).hexdigest()
        binding = {
            "job_id": job_id,
            "request_id": payload["request_id"],
            "run_id": payload["run_id"],
            "event_id": payload["event_id"],
            "account_id": payload["account_id"],
            "group_id": payload["group_id"],
            "trigger_received_at": payload["trigger_received_at"],
            "snapshot_at": payload["snapshot_at"],
            "snapshot_sha256": payload["snapshot_sha256"],
            "profile_id": payload["profile_id"],
            "context_prompt": payload["context_prompt"],
            "context_prompt_sha256": context_prompt_sha256,
        }
        encoded = json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _payload_value_matches(actual: Any, expected: Any) -> bool:
        if type(actual) is not type(expected):
            return False
        if isinstance(expected, str):
            return hmac.compare_digest(
                actual.encode("utf-8"), expected.encode("utf-8")
            )
        return actual == expected

    def _validate_response(
        self,
        response: Any,
        payload: dict[str, Any],
        job_id: str,
        *,
        submit: bool,
    ) -> tuple[dict[str, Any], str] | None:
        if not isinstance(response, dict):
            return None
        expected = {
            "job_id": job_id,
            **payload,
            "context_prompt_sha256": hashlib.sha256(
                payload["context_prompt"].encode("utf-8")
            ).hexdigest(),
            "binding_sha256": self._binding_digest(job_id, payload),
        }
        if any(
            field not in response
            or not self._payload_value_matches(response[field], value)
            for field, value in expected.items()
        ):
            return None
        status = response.get("status")
        if not isinstance(status, str) or status not in _WAN_STATUSES:
            return None
        if submit and status != "queued":
            return None
        output_sha256 = response.get("output_sha256")
        decode_metadata = response.get("decode_metadata")
        if status == "complete":
            if (
                not isinstance(output_sha256, str)
                or _SHA256.fullmatch(output_sha256) is None
                or self._decode_metadata_sha256(decode_metadata) is None
            ):
                return None
        elif output_sha256 is not None or decode_metadata is not None:
            return None
        return response, status

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("Wan total deadline elapsed")
        return remaining

    async def _cancel_job(self, client: Any, job_id: str) -> None:
        try:
            await client.delete(
                f"{self.base_url}/v1/video/jobs/{job_id}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
                timeout=self.request_timeout,
            )
        except BaseException:
            # Cleanup is authenticated and bounded, but remains best effort.
            pass

    async def generate(
        self,
        *,
        run_id: str,
        event_id: str,
        account_id: str,
        group_id: int,
        trigger_received_at: str,
        snapshot_at: str,
        snapshot_sha256: str,
        profile_id: int,
        context_prompt: str,
    ) -> BoundVideoAsset | None:
        request = VideoGenerationRequest(
            run_id=run_id,
            event_id=event_id,
            account_id=account_id,
            group_id=group_id,
            trigger_received_at=trigger_received_at,
            snapshot_at=snapshot_at,
            snapshot_sha256=snapshot_sha256,
            profile_id=profile_id,
            context_prompt=context_prompt,
        )
        if not self._valid_request(request):
            return None
        request_id = self._request_id_factory()
        if not isinstance(request_id, str) or _JOB_ID.fullmatch(request_id) is None:
            return None
        payload = {
            "request_id": request_id,
            "run_id": request.run_id,
            "event_id": request.event_id,
            "account_id": request.account_id,
            "group_id": request.group_id,
            "trigger_received_at": request.trigger_received_at,
            "snapshot_at": request.snapshot_at,
            "snapshot_sha256": request.snapshot_sha256,
            "profile_id": request.profile_id,
            "context_prompt": request.context_prompt,
        }
        client = self._http_client or httpx.AsyncClient()
        owns_client = self._http_client is None
        auth_headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        deadline = self._monotonic() + self.total_timeout
        job_id = ""
        succeeded = False
        try:
            submitted = await client.post(
                f"{self.base_url}/v1/video/jobs",
                json=payload,
                headers=auth_headers,
                timeout=min(self.request_timeout, self._remaining(deadline)),
            )
            self._remaining(deadline)
            if submitted.status_code != 202:
                return None
            submitted_payload = submitted.json()
            if isinstance(submitted_payload, dict):
                candidate_job_id = submitted_payload.get("job_id")
                if isinstance(candidate_job_id, str) and _JOB_ID.fullmatch(
                    candidate_job_id
                ):
                    job_id = candidate_job_id
            if not job_id or self._validate_response(
                submitted_payload, payload, job_id, submit=True
            ) is None:
                return None

            status_url = f"{self.base_url}/v1/video/jobs/{job_id}"
            state: dict[str, Any] | None = None
            while True:
                polled = await client.get(
                    status_url,
                    headers=auth_headers,
                    timeout=min(self.request_timeout, self._remaining(deadline)),
                )
                self._remaining(deadline)
                if polled.status_code != 200:
                    return None
                validated = self._validate_response(
                    polled.json(), payload, job_id, submit=False
                )
                if validated is None:
                    return None
                state, status = validated
                if status == "complete":
                    break
                if status in {"failed", "cancelled"}:
                    return None
                await self._sleep(min(self.poll_interval, self._remaining(deadline)))
                self._remaining(deadline)

            result_reference = state.get(
                "result_url", f"/v1/video/jobs/{job_id}/result"
            )
            if not isinstance(result_reference, str) or not result_reference:
                return None
            download_url = urljoin(f"{self.base_url}/", result_reference)
            if not self._same_origin(download_url):
                return None
            downloaded = await client.get(
                download_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "video/mp4",
                },
                timeout=min(self.download_timeout, self._remaining(deadline)),
            )
            self._remaining(deadline)
            data = bytes(downloaded.content or b"")
            content_type = str(downloaded.headers.get("content-type") or "").lower()
            output_sha256 = state["output_sha256"]
            expected_headers = {
                "X-SDF-Contract-Version": "1",
                "X-SDF-Request-ID": request_id,
                "X-SDF-Job-ID": job_id,
                "X-SDF-Run-ID": request.run_id,
                "X-SDF-Event-ID": request.event_id,
                "X-SDF-Account-ID": request.account_id,
                "X-SDF-Group-ID": str(request.group_id),
                "X-SDF-Trigger-Received-At": request.trigger_received_at,
                "X-SDF-Snapshot-At": request.snapshot_at,
                "X-SDF-Profile-ID": str(request.profile_id),
                "X-SDF-Snapshot-SHA256": request.snapshot_sha256,
                "X-SDF-Context-Prompt-SHA256": hashlib.sha256(
                    request.context_prompt.encode("utf-8")
                ).hexdigest(),
                "X-SDF-Output-SHA256": output_sha256,
                "X-SDF-Decode-Schema": "1",
            }
            headers_match = all(
                isinstance(downloaded.headers.get(name), str)
                and hmac.compare_digest(downloaded.headers[name], expected)
                for name, expected in expected_headers.items()
            )
            body_sha256 = hashlib.sha256(data).hexdigest()
            if (
                downloaded.status_code != 200
                or not content_type.startswith("video/mp4")
                or not headers_match
                or len(data) < 12
                or data[4:8] != b"ftyp"
                or len(data) > _MAX_ASSET_BYTES
                or not hmac.compare_digest(body_sha256, output_sha256)
            ):
                return None
            asset = MediaAsset("video", data, f"wan-{job_id}.mp4", "video/mp4")
            from .worker import MediaEvidence

            trigger_timestamp = self._timestamp(request.trigger_received_at)
            snapshot_timestamp = self._timestamp(request.snapshot_at)
            assert trigger_timestamp is not None and snapshot_timestamp is not None
            evidence = MediaEvidence(
                request_id=request_id,
                snapshot_sha256=request.snapshot_sha256,
                output_sha256=output_sha256,
                trigger_received_at=trigger_timestamp.timestamp(),
                snapshot_at=snapshot_timestamp.timestamp(),
                profile_id=str(request.profile_id),
                content_sha256=hashlib.sha256(
                    request.context_prompt.encode("utf-8")
                ).hexdigest(),
                decode_metadata_sha256=self._decode_metadata_sha256(
                    state["decode_metadata"]
                )
                or "",
            )
            succeeded = True
            return BoundVideoAsset(asset=asset, media_evidence=evidence)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        finally:
            if job_id and not succeeded:
                await self._cancel_job(client, job_id)
            if owns_client:
                await client.aclose()


@dataclass(frozen=True)
class OutboundPermit:
    allowed: bool
    tracked: bool = False
    run_id: str = ""
    event_id: str = ""
    generation: int = 0
    account_id: str = ""
    group_id: int = 0
    kind: str = ""
    scripted: bool = False
    request_id: str = ""
    snapshot_sha256: str = ""
    output_sha256: str = ""
    trigger_received_at: float = 0.0
    snapshot_at: float = 0.0
    profile_id: str = ""
    content_sha256: str = ""
    decode_metadata_sha256: str = ""


class LiveTestOutboundGate:
    """Shared final-RPC gate for every worker owned by one manager."""

    def __init__(
        self,
        db: Any,
        *,
        clock: Callable[[], float] = time.time,
        last_human_activity: dict[int, float] | None = None,
        human_pause_seconds: float = _HUMAN_PAUSE_SECONDS,
    ):
        self.db = db
        self._clock = clock
        self._last_human_activity = (
            last_human_activity if last_human_activity is not None else {}
        )
        self._human_pause_seconds = max(0.0, float(human_pause_seconds))
        self._active: tuple[str, frozenset[str], int, float, int] | None = None
        self._generation = 0
        self._finish_lock = asyncio.Lock()

    def activate(
        self,
        *,
        run_id: str,
        account_ids: list[str],
        group_id: int,
        expires_at: float | None = None,
    ) -> None:
        self._generation += 1
        self._active = (
            str(run_id),
            frozenset(str(account_id) for account_id in account_ids),
            int(group_id),
            float("inf") if expires_at is None else float(expires_at),
            self._generation,
        )

    def prepare(
        self,
        *,
        run_id: str,
        group_id: int,
        expires_at: float,
    ) -> None:
        """Deny every manager-owned outbound while a run is being persisted."""
        self.activate(
            run_id=run_id,
            account_ids=[],
            group_id=group_id,
            expires_at=expires_at,
        )

    def deactivate(self, run_id: str) -> None:
        active = self._active
        if active and active[0] == str(run_id):
            self._generation += 1
            self._active = None

    def lockdown(self, run_id: str) -> bool:
        """Keep the run gate active while denying every outbound RPC."""
        active = self._active
        if active is None or active[0] != str(run_id):
            return False
        self._generation += 1
        self._active = (
            active[0],
            frozenset(),
            active[2],
            active[3],
            self._generation,
        )
        return True

    def validate(
        self,
        permit: OutboundPermit,
        *,
        account_id: str,
        group_id: int,
    ) -> bool:
        """Synchronously linearize a permit immediately before its RPC call."""
        if not permit.allowed or int(group_id) >= 0:
            return False
        active = self._active
        if not permit.tracked:
            return active is None
        if active is None:
            return False
        run_id, account_ids, target_group, expires_at, generation = active
        if (
            permit.run_id != run_id
            or permit.generation != generation
            or permit.account_id != str(account_id)
            or permit.group_id != int(group_id)
            or str(account_id) not in account_ids
            or int(group_id) != target_group
            or float(self._clock()) >= expires_at
        ):
            return False
        if permit.scripted:
            last_human = float(
                self._last_human_activity.get(int(group_id), 0.0) or 0.0
            )
            if last_human and float(self._clock()) < (
                last_human + self._human_pause_seconds
            ):
                return False
        return True

    async def mark_rpc_started(self, permit: OutboundPermit) -> bool:
        if not permit.tracked:
            return True
        if not self.validate(
            permit,
            account_id=permit.account_id,
            group_id=permit.group_id,
        ):
            return False
        started = await self.db.mark_live_test_event_rpc_started(
            permit.run_id,
            permit.event_id,
            account_id=permit.account_id,
            group_id=permit.group_id,
            kind=permit.kind,
            request_id=permit.request_id,
            snapshot_sha256=permit.snapshot_sha256,
            output_sha256=permit.output_sha256,
            trigger_received_at=permit.trigger_received_at,
            snapshot_at=permit.snapshot_at,
            profile_id=permit.profile_id,
            content_sha256=permit.content_sha256,
            decode_metadata_sha256=permit.decode_metadata_sha256,
        )
        if not started:
            return False
        if self.validate(
            permit,
            account_id=permit.account_id,
            group_id=permit.group_id,
        ):
            return True
        await self.db.finish_live_test_event(
            permit.run_id,
            permit.event_id,
            "released",
            "permit revoked after rpc_started but before RPC call",
            kind=permit.kind,
            request_id=permit.request_id,
            snapshot_sha256=permit.snapshot_sha256,
            output_sha256=permit.output_sha256,
            trigger_received_at=permit.trigger_received_at,
            snapshot_at=permit.snapshot_at,
            profile_id=permit.profile_id,
            content_sha256=permit.content_sha256,
            decode_metadata_sha256=permit.decode_metadata_sha256,
            rpc_started=True,
        )
        return False

    async def release_bound(
        self,
        *,
        run_id: str,
        event_id: str,
        account_id: str,
        group_id: int,
        kind: str,
        request_id: str,
        snapshot_sha256: str,
        output_sha256: str,
        trigger_received_at: float,
        snapshot_at: float,
        profile_id: str,
        content_sha256: str,
        decode_metadata_sha256: str,
        detail: str,
    ) -> bool:
        async with self._finish_lock:
            return await self.db.release_live_test_event_bound(
                run_id,
                event_id,
                account_id=account_id,
                group_id=group_id,
                kind=kind,
                request_id=request_id,
                snapshot_sha256=snapshot_sha256,
                output_sha256=output_sha256,
                trigger_received_at=trigger_received_at,
                snapshot_at=snapshot_at,
                profile_id=profile_id,
                content_sha256=content_sha256,
                decode_metadata_sha256=decode_metadata_sha256,
                detail=detail,
            )

    async def reserve(
        self,
        *,
        account_id: str,
        group_id: int,
        kind: str,
        event_id: str | None = None,
        request_id: str = "",
        snapshot_sha256: str = "",
        output_sha256: str = "",
        trigger_received_at: float = 0.0,
        snapshot_at: float = 0.0,
        profile_id: str = "",
        content_sha256: str = "",
        decode_metadata_sha256: str = "",
    ) -> OutboundPermit:
        if isinstance(group_id, bool) or int(group_id) >= 0:
            return OutboundPermit(allowed=False)
        active = self._active
        if active is None:
            return OutboundPermit(allowed=True)
        run_id, account_ids, target_group, _, generation = active
        if str(account_id) not in account_ids or int(group_id) != target_group:
            return OutboundPermit(allowed=False)
        scripted_event = bool(str(event_id or "").strip())
        if scripted_event:
            last_human = float(
                self._last_human_activity.get(int(group_id), 0.0) or 0.0
            )
            if last_human and float(self._clock()) < (
                last_human + self._human_pause_seconds
            ):
                return OutboundPermit(allowed=False)
        identifier = str(event_id or "").strip()
        if not identifier:
            identifier = f"organic:{kind}:{uuid.uuid4().hex}"
        reserved = await self.db.reserve_live_test_event(
            run_id,
            identifier,
            str(account_id),
            str(kind),
            group_id=int(group_id),
            scripted=scripted_event,
            request_id=request_id,
            snapshot_sha256=snapshot_sha256,
            output_sha256=output_sha256,
            trigger_received_at=trigger_received_at,
            snapshot_at=snapshot_at,
            profile_id=profile_id,
            content_sha256=content_sha256,
            decode_metadata_sha256=decode_metadata_sha256,
            clock=self._clock,
        )
        if not reserved:
            return OutboundPermit(allowed=False)
        permit = OutboundPermit(
            allowed=True,
            tracked=True,
            run_id=run_id,
            event_id=identifier,
            generation=generation,
            account_id=str(account_id),
            group_id=int(group_id),
            kind=str(kind),
            scripted=scripted_event,
            request_id=str(request_id),
            snapshot_sha256=str(snapshot_sha256),
            output_sha256=str(output_sha256),
            trigger_received_at=float(trigger_received_at),
            snapshot_at=float(snapshot_at),
            profile_id=str(profile_id),
            content_sha256=str(content_sha256),
            decode_metadata_sha256=str(decode_metadata_sha256),
        )
        if self.validate(permit, account_id=account_id, group_id=group_id):
            return permit
        await self.complete(
            permit,
            sent=False,
            detail="reservation revoked before final RPC validation",
        )
        return OutboundPermit(allowed=False)

    async def complete(
        self,
        permit: OutboundPermit,
        *,
        sent: bool,
        rpc_started: bool = False,
        detail: str = "",
    ) -> bool:
        if not permit.tracked:
            return True
        async with self._finish_lock:
            terminal_state = (
                "sent" if sent else "hard_attempt" if rpc_started else "released"
            )
            return await self.db.finish_live_test_event(
                permit.run_id,
                permit.event_id,
                terminal_state,
                detail,
                kind=permit.kind,
                request_id=permit.request_id,
                snapshot_sha256=permit.snapshot_sha256,
                output_sha256=permit.output_sha256,
                trigger_received_at=permit.trigger_received_at,
                snapshot_at=permit.snapshot_at,
                profile_id=permit.profile_id,
                content_sha256=permit.content_sha256,
                decode_metadata_sha256=permit.decode_metadata_sha256,
            )


class BoundedLiveTest:
    """Manager-owned scheduler using only already-running AccountWorkers.

    Every event obtains a durable Database reservation before any worker send
    primitive can reach Telegram. The class never creates a TelegramClient and
    never handles encrypted sessions.
    """

    def __init__(
        self,
        manager: Any,
        *,
        enabled: bool,
        wan22_ready: bool = False,
        asset_root: str | Path | None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.manager = manager
        self.db = manager.db
        self.enabled = bool(enabled)
        self.wan22_ready = bool(wan22_ready)
        raw_root = str(asset_root or "").strip()
        root = Path(raw_root).expanduser() if raw_root else None
        self.asset_root = root.resolve() if root and root.is_absolute() else None
        self._clock = clock
        self._sleep = sleep
        self.video_client = getattr(manager, "video_client", None) or RealtimeVideoClient(
            manager.config
        )
        self.outbound_gate = LiveTestOutboundGate(
            db=self.db,
            clock=clock,
            last_human_activity=getattr(manager, "last_human_activity", None),
        )
        for worker in self.manager.workers.values():
            worker.outbound_gate = self.outbound_gate
        self._task: asyncio.Task | None = None
        self._run_id: str | None = None
        self._account_ids: tuple[str, ...] = ()
        self._start_lock = asyncio.Lock()
        self._video_generation_lock = asyncio.Lock()

    async def reconcile(self) -> None:
        """Stop accounts for every persisted run that was not cleanly closed."""
        if self._task and not self._task.done():
            return
        finder = getattr(self.db, "get_live_test_reconciliation_run", None)
        while True:
            latest = (
                await finder()
                if callable(finder)
                else await self.db.get_live_test_run()
            )
            if not latest or latest.get("status") not in {
                "running",
                "needs_reconciliation",
                "lockdown",
                "expired",
                "failed",
            }:
                return
            reconciled = await self._reconcile_run(latest)
            if not reconciled or not callable(finder):
                return

    async def _reconcile_run(self, latest: dict) -> bool:
        account_ids = tuple(str(value) for value in latest.get("account_ids", []))
        run_id = str(latest["id"])
        expired = str(latest.get("stop_reason") or "") == "expired"
        self.outbound_gate.activate(
            run_id=run_id,
            account_ids=list(account_ids),
            group_id=int(latest["group_id"]),
            expires_at=float(latest["expires_at"]),
        )
        self.outbound_gate.lockdown(run_id)
        marker = getattr(self.db, "mark_live_test_needs_reconciliation", None)
        if callable(marker):
            await marker(run_id, "reconciliation_required: process_restart")
        stop_errors = await self._stop_accounts(account_ids)
        if stop_errors:
            if callable(marker):
                await marker(run_id, "stop_failed: " + "; ".join(stop_errors))
            return False
        try:
            await self.db.reconcile_live_test_events(run_id, "process_restart")
        except Exception as exc:
            marker_call = (
                cast(Callable[..., Awaitable[Any]], marker)
                if callable(marker)
                else None
            )
            if marker_call is not None:
                await marker_call(
                    run_id,
                    f"event_reconciliation_failed: {type(exc).__name__}",
                )
            return False
        persisted = await self.db.finish_live_test_run(
            run_id,
            status="expired" if expired else "failed",
            reason="reconciled_expired" if expired else "process_restart",
        )
        if persisted:
            self.outbound_gate.deactivate(run_id)
        return bool(persisted)

    async def start_block_error(self) -> str:
        checker = getattr(self.db, "has_live_test_reconciliation", None)
        if callable(checker) and await checker():
            return "live-test reconciliation is required before account startup"
        return ""

    def _feature_error(self, *, video_enabled: bool = True) -> str:
        if not self.enabled:
            return "live test feature is disabled"
        if self.asset_root is None or not self.asset_root.is_dir():
            return "configured live test asset root is unavailable"
        config = self.manager.config
        if not bool(getattr(config, "media_enabled", False)):
            return "media feature is disabled"
        if str(getattr(config, "vision_model", "")).strip() != APPROVED_VISION_MODEL:
            return "vision model is not approved for live test"
        if not bool(getattr(config, "voice_media_enabled", False)):
            return "voice feature is disabled"
        if not str(getattr(config, "voice_realtime_url", "")).strip():
            return "realtime voice service is unavailable"
        if not str(getattr(config, "voice_realtime_token", "")).strip():
            return "realtime voice authentication is unavailable"
        if video_enabled:
            if not self.wan22_ready:
                return "Wan 2.2 assets are not marked ready"
            if not str(getattr(config, "video_realtime_url", "")).strip():
                return "realtime video service is unavailable"
            if not str(getattr(config, "video_realtime_token", "")).strip():
                return "realtime video authentication is unavailable"
        return ""

    @staticmethod
    def _strict_int(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise LiveTestError(f"{label} must be an integer")
        return int(value)

    async def _validate_accounts(
        self, account_ids: list[str], group_id: int
    ) -> None:
        requested = set(account_ids)
        if requested != _FIXED_LIVE_TEST_ACCOUNT_IDS:
            raise LiveTestError("request must select the four fixed managed accounts")
        selected_sets: list[set[int]] = []
        ages: list[int] = []
        for account_id in account_ids:
            account = await self.db.get_account(account_id)
            if account is None:
                raise LiveTestError("request must select the four fixed managed accounts")
            if not int(account.get("setup_complete") or 0):
                raise LiveTestError(f"account {account_id} is not configured")
            try:
                persona = json.loads(str(account.get("persona") or "{}"))
                age = int(persona.get("age") or 0)
                raw_groups = json.loads(str(account.get("groups") or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                persona, age, raw_groups = {}, 0, []
            if not isinstance(persona, dict) or persona.get("gender") != "女":
                raise LiveTestError(f"account {account_id} persona must be female")
            if age != int(_ACCOUNT_PROFILE_MAP[account_id]):
                raise LiveTestError("fixed account profile mapping mismatch")
            if not isinstance(raw_groups, list):
                raw_groups = []
            selected_sets.append(
                {
                    int(value)
                    for value in raw_groups
                    if type(value) is int and int(value) < 0
                }
            )
            ages.append(age)
        common = set.intersection(*selected_sets) if selected_sets else set()
        if group_id not in common:
            raise LiveTestError("no common selected group for all four accounts")
        if set(ages) != _FIXED_PERSONA_AGES or len(ages) != len(set(ages)):
            raise LiveTestError("persona ages must be exactly 21, 25, 29, and 34")

    async def _validate_running_accounts(
        self, account_ids: list[str], group_id: int
    ) -> None:
        requested = set(account_ids)
        workers = self.manager.workers
        if requested != _FIXED_LIVE_TEST_ACCOUNT_IDS or set(workers) != requested:
            raise LiveTestError("request must select the four fixed managed accounts")
        ages: list[int] = []
        selected_sets: list[set[int]] = []
        for account_id in account_ids:
            account = await self.db.get_account(account_id)
            if account is None or not int(account.get("enabled") or 0):
                raise LiveTestError(f"account {account_id} is not configured")
            try:
                persona = json.loads(str(account.get("persona") or "{}"))
                age = int(persona.get("age") or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                persona, age = {}, 0
            worker = workers.get(account_id)
            if (
                worker is None
                or not bool(getattr(worker, "is_running", False))
                or getattr(worker, "tg_client", None) is None
            ):
                raise LiveTestError(f"account {account_id} worker is not running")
            worker_persona = getattr(worker, "persona", {})
            if not isinstance(worker_persona, dict) or worker_persona != persona:
                raise LiveTestError(
                    f"account {account_id} DB and worker persona must match exactly"
                )
            try:
                worker_age = int(worker_persona.get("age") or 0)
            except (TypeError, ValueError):
                worker_age = 0
            if worker_persona.get("gender") != "女":
                raise LiveTestError(f"account {account_id} worker persona must be female")
            if worker_age != age:
                raise LiveTestError(
                    f"account {account_id} DB and worker persona must match exactly"
                )
            ages.append(age)
            selected_sets.append(
                {int(value) for value in getattr(worker, "selected_groups", set())}
            )
        common = set.intersection(*selected_sets) if selected_sets else set()
        if group_id not in common:
            raise LiveTestError("no common selected group for all four accounts")
        if set(ages) != _FIXED_PERSONA_AGES or len(ages) != len(set(ages)):
            raise LiveTestError("persona ages must be exactly 21, 25, 29, and 34")

    def _asset_path(self, relative: Any, kind: str) -> Path:
        if self.asset_root is None or not isinstance(relative, str):
            raise LiveTestError("invalid asset path")
        raw = relative.strip()
        candidate_input = Path(raw)
        if (
            not raw
            or candidate_input.is_absolute()
            or ".." in candidate_input.parts
        ):
            raise LiveTestError("invalid asset path")
        try:
            candidate = (self.asset_root / candidate_input).resolve(strict=True)
        except OSError as exc:
            raise LiveTestError(f"missing local asset: {raw}") from exc
        if not candidate.is_relative_to(self.asset_root) or not candidate.is_file():
            raise LiveTestError("invalid asset path outside configured root")
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise LiveTestError(f"missing local asset: {raw}") from exc
        if size <= 0 or size > _MAX_ASSET_BYTES:
            raise LiveTestError(f"invalid local asset size: {raw}")
        suffix = candidate.suffix.lower()
        if kind in {"image", "vision_reply"} and suffix not in _IMAGE_SUFFIXES:
            raise LiveTestError(f"invalid image asset: {raw}")
        return candidate

    async def _validate(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise LiveTestError("request body must be an object")
        video_enabled = request.get("video_enabled", True)
        if type(video_enabled) is not bool:
            raise LiveTestError("video_enabled must be a boolean")
        feature_error = self._feature_error(video_enabled=video_enabled)
        if feature_error:
            raise LiveTestError(feature_error)
        raw_accounts = request.get("account_ids")
        if not isinstance(raw_accounts, list):
            raise LiveTestError("account_ids must be a list")
        account_ids = [
            value.strip() for value in raw_accounts if isinstance(value, str)
        ]
        if (
            len(account_ids) != 4
            or len(account_ids) != len(raw_accounts)
            or len(set(account_ids)) != 4
            or any(not value for value in account_ids)
        ):
            raise LiveTestError("exactly four unique account IDs are required")
        group_id = self._strict_int(request.get("group_id"), "group_id")
        if group_id >= 0:
            raise LiveTestError("group_id must be the negative fixed live-test group")
        if group_id != _FIXED_LIVE_TEST_GROUP_ID:
            raise LiveTestError("request must use the fixed live-test group")
        if set(account_ids) != _FIXED_LIVE_TEST_ACCOUNT_IDS:
            raise LiveTestError("request must select the four fixed managed accounts")
        duration = self._strict_int(
            request.get("duration_seconds"), "duration_seconds"
        )
        if duration != 3_600:
            raise LiveTestError("duration_seconds must be exactly 3600")
        event_cap = self._strict_int(request.get("event_cap"), "event_cap")
        if event_cap != 40:
            raise LiveTestError("event_cap must be exactly 40")
        await self._validate_accounts(account_ids, group_id)
        if self.manager.workers:
            await self._validate_running_accounts(account_ids, group_id)

        raw_schedule = request.get("schedule")
        if not isinstance(raw_schedule, list) or not raw_schedule:
            raise LiveTestError("schedule must be a non-empty list")
        if len(raw_schedule) != 30:
            raise LiveTestError("schedule must contain exactly 30 events")
        normalized: list[dict] = []
        event_ids: set[str] = set()
        counts: dict[str, int] = {}
        kind_accounts: dict[str, set[str]] = {"voice": set(), "image": set()}
        media_paths: dict[str, list[str]] = {"image": []}
        for raw_event in raw_schedule:
            if not isinstance(raw_event, dict):
                raise LiveTestError("every schedule event must be an object")
            event_id = str(raw_event.get("event_id") or "").strip()
            if not _EVENT_ID.fullmatch(event_id) or event_id in event_ids:
                raise LiveTestError("schedule event IDs must be unique and valid")
            event_ids.add(event_id)
            account_id = str(raw_event.get("account_id") or "").strip()
            if account_id not in account_ids:
                raise LiveTestError("schedule references a wrong account")
            kind = str(raw_event.get("kind") or "").strip()
            if kind not in {"text", "voice", "image", "video", "vision_reply"}:
                raise LiveTestError(f"unsupported live test event kind: {kind}")
            offset = raw_event.get("offset_seconds")
            if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                raise LiveTestError("offset_seconds must be numeric")
            offset = float(offset)
            if not math.isfinite(offset) or offset < 0 or offset >= duration:
                raise LiveTestError("schedule event falls outside run duration")
            event = {
                "event_id": event_id,
                "account_id": account_id,
                "kind": kind,
                "offset_seconds": offset,
            }
            counts[kind] = counts.get(kind, 0) + 1
            if kind in {"voice", "video"}:
                if set(raw_event) != {
                    "event_id",
                    "offset_seconds",
                    "account_id",
                    "kind",
                }:
                    raise LiveTestError(
                        "voice/video events accept trigger fields only"
                    )
            elif kind == "text":
                text = str(raw_event.get("text") or "").strip()
                if not text or len(text) > 60:
                    raise LiveTestError(f"invalid {kind} event text")
                event["text"] = text
            else:
                relative = str(raw_event.get("path") or "").strip()
                self._asset_path(relative, kind)
                event["path"] = relative
                if kind in media_paths:
                    media_paths[kind].append(relative)
            if kind in kind_accounts:
                kind_accounts[kind].add(account_id)
            normalized.append(event)

        if counts.get("text", 0) < 1:
            raise LiveTestError("schedule must include text events")
        if counts.get("voice", 0) != 4 or kind_accounts["voice"] != set(
            account_ids
        ):
            raise LiveTestError("schedule requires exactly four realtime voices")
        if counts.get("image", 0) != 4 or kind_accounts["image"] != set(
            account_ids
        ):
            raise LiveTestError("schedule requires exactly four adult images")
        if len(set(media_paths["image"])) != 4:
            raise LiveTestError("the four image assets must be distinct")
        if video_enabled:
            if counts.get("video", 0) != 2:
                raise LiveTestError("schedule requires exactly two realtime videos")
        elif counts.get("video", 0) != 0:
            raise LiveTestError("video events are disabled for this live test")
        if counts.get("vision_reply", 0) != 2:
            raise LiveTestError(
                "schedule requires exactly two designated image-understanding replies"
            )
        image_assets = set(media_paths["image"])
        vision_assets = {
            event["path"] for event in normalized if event["kind"] == "vision_reply"
        }
        if not vision_assets.issubset(image_assets):
            raise LiveTestError(
                "image-understanding replies must use scheduled image assets"
            )
        normalized.sort(key=lambda event: event["offset_seconds"])
        return {
            "account_ids": account_ids,
            "group_id": group_id,
            "duration_seconds": duration,
            "event_cap": event_cap,
            "video_enabled": video_enabled,
            "schedule": normalized,
        }

    async def start(self, request: dict) -> dict:
        async with self._start_lock:
            if self._task and not self._task.done():
                raise LiveTestError("another live test is already running")
            blocked = await self.start_block_error()
            if blocked:
                raise LiveTestError(blocked)
            normalized = await self._validate(request)
            run_id = uuid.uuid4().hex
            started_at = float(self._clock())
            self.outbound_gate.prepare(
                run_id=run_id,
                group_id=normalized["group_id"],
                expires_at=started_at + normalized["duration_seconds"],
            )
            created = False
            try:
                created = await self.db.create_live_test_run(
                    run_id=run_id,
                    account_ids=normalized["account_ids"],
                    group_id=normalized["group_id"],
                    duration_seconds=normalized["duration_seconds"],
                    event_cap=normalized["event_cap"],
                    schedule=normalized["schedule"],
                    started_at=started_at,
                )
                if not created:
                    raise LiveTestError("another persistent live test is already running")
                self._run_id = run_id
                self._account_ids = tuple(normalized["account_ids"])
                async def validate_and_activate() -> None:
                    await self._validate_running_accounts(
                        normalized["account_ids"], normalized["group_id"]
                    )
                    self.outbound_gate.activate(
                        run_id=run_id,
                        account_ids=normalized["account_ids"],
                        group_id=normalized["group_id"],
                        expires_at=started_at + normalized["duration_seconds"],
                    )

                start_error = await self.manager.start_live_test_accounts(
                    normalized["account_ids"],
                    normalized["group_id"],
                    before_release=validate_and_activate,
                )
                if start_error:
                    raise LiveTestError(str(start_error))
                self._task = asyncio.create_task(
                    self._run(run_id, normalized, started_at),
                    name=f"bounded-live-test:{run_id}",
                )
            except BaseException as exc:
                self.outbound_gate.lockdown(run_id)
                if created:
                    stop_errors = await self._stop_accounts(
                        tuple(normalized["account_ids"])
                    )
                    reason = f"startup_failed: {type(exc).__name__}: {exc}"
                    if stop_errors:
                        reason += "; " + "; ".join(stop_errors)
                    if stop_errors:
                        try:
                            await self.db.mark_live_test_needs_reconciliation(
                                run_id, reason
                            )
                        except Exception:
                            pass
                    else:
                        persisted = False
                        try:
                            persisted = await self.db.finish_live_test_run(
                                run_id, "failed", reason
                            )
                        except Exception:
                            persisted = False
                        if persisted:
                            self.outbound_gate.deactivate(run_id)
                        else:
                            try:
                                await self.db.mark_live_test_needs_reconciliation(
                                    run_id, reason
                                )
                            except Exception:
                                pass
                else:
                    self.outbound_gate.deactivate(run_id)
                self._run_id = None
                self._account_ids = ()
                raise
            return {
                "id": run_id,
                "status": "running",
                "running": 4,
                "reserved": 0,
                "sent": 0,
                "failed": 0,
                "event_cap": normalized["event_cap"],
                "duration_seconds": normalized["duration_seconds"],
                "video_enabled": normalized["video_enabled"],
            }

    def _runtime_error(
        self,
        account_ids: list[str],
        group_id: int,
        *,
        video_enabled: bool = True,
    ) -> str:
        feature_error = self._feature_error(video_enabled=video_enabled)
        if feature_error:
            return feature_error
        for account_id in account_ids:
            worker = self.manager.workers.get(account_id)
            if (
                worker is None
                or not bool(getattr(worker, "is_running", False))
                or getattr(worker, "tg_client", None) is None
            ):
                return f"account {account_id} worker is not running"
            if group_id not in {
                int(value) for value in getattr(worker, "selected_groups", set())
            }:
                return "no common selected group for all four accounts"
        return ""

    async def _wait_for_dispatch(
        self,
        *,
        scheduled_at: float,
        expires_at: float,
        account_ids: list[str],
        group_id: int,
        video_enabled: bool = True,
    ) -> None:
        while True:
            now = float(self._clock())
            if now >= expires_at:
                raise _RunExpired("duration elapsed")
            runtime_error = self._runtime_error(
                account_ids, group_id, video_enabled=video_enabled
            )
            if runtime_error:
                raise _RunFailed(runtime_error)
            human_at = float(self.manager.last_human_activity.get(group_id, 0.0) or 0.0)
            not_before = max(scheduled_at, human_at + _HUMAN_PAUSE_SECONDS)
            delay = not_before - now
            if delay <= 0:
                return
            await self._sleep(min(_MONITOR_SECONDS, delay, expires_at - now))

    def _load_asset(self, event: dict) -> MediaAsset:
        if event.get("kind") == "video":
            raise _RunFailed("video dispatch must use realtime generation")
        path = self._asset_path(event["path"], event["kind"])
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise _RunFailed(f"missing local asset: {event['path']}") from exc
        if not data or len(data) > _MAX_ASSET_BYTES:
            raise _RunFailed(f"invalid local asset: {event['path']}")
        kind = "image" if event["kind"] == "vision_reply" else event["kind"]
        mime = mimetypes.guess_type(path.name)[0]
        if not mime:
            mime = "image/jpeg" if kind == "image" else "video/mp4"
        return MediaAsset(kind, data, path.name, mime)

    @staticmethod
    def _iso_timestamp(value: Any) -> str | None:
        if isinstance(value, str):
            return value if RealtimeVideoClient._timestamp(value) is not None else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        try:
            return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    def _video_snapshot_fields(
        self,
        generated: Any,
        *,
        run_id: str,
        event_id: str,
        account_id: str,
        group_id: int,
        trigger_received_at: float,
        profile_id: int,
    ) -> tuple[str, str, str] | None:
        if not all(
            hasattr(generated, field)
            for field in (
                "run_id",
                "event_id",
                "account_id",
                "group_id",
                "trigger_received_at",
                "snapshot_at",
                "snapshot_sha256",
                "profile_id",
                "context_prompt",
            )
        ):
            return None
        if (
            generated.run_id != run_id
            or generated.event_id != event_id
            or generated.account_id != account_id
            or generated.group_id != group_id
            or generated.trigger_received_at != trigger_received_at
            or generated.profile_id != profile_id
        ):
            return None
        snapshot_at = generated.snapshot_at
        snapshot_sha256 = generated.snapshot_sha256
        context_prompt = generated.context_prompt
        normalized_snapshot_at = self._iso_timestamp(snapshot_at)
        if (
            normalized_snapshot_at is None
            or not isinstance(snapshot_sha256, str)
            or _SHA256.fullmatch(snapshot_sha256) is None
            or not isinstance(context_prompt, str)
            or not context_prompt.strip()
        ):
            return None
        return normalized_snapshot_at, snapshot_sha256, context_prompt

    @staticmethod
    async def _generate_video_context(
        worker: Any,
        group_id: int,
        *,
        run_id: str,
        event_id: str,
        trigger_received_at: float,
    ) -> Any:
        return await worker.generate_realtime_video_brief(
            group_id,
            run_id=run_id,
            event_id=event_id,
            trigger_received_at=trigger_received_at,
        )

    @staticmethod
    def _bound_video_matches_request(
        bound: Any, request: VideoGenerationRequest
    ) -> bool:
        if not isinstance(bound, BoundVideoAsset):
            return False
        asset = bound.asset
        evidence = bound.media_evidence
        trigger = RealtimeVideoClient._timestamp(request.trigger_received_at)
        snapshot = RealtimeVideoClient._timestamp(request.snapshot_at)
        evidence_trigger = BoundedLiveTest._iso_timestamp(
            getattr(evidence, "trigger_received_at", None)
        )
        evidence_snapshot = BoundedLiveTest._iso_timestamp(
            getattr(evidence, "snapshot_at", None)
        )
        parsed_evidence_trigger = RealtimeVideoClient._timestamp(evidence_trigger)
        parsed_evidence_snapshot = RealtimeVideoClient._timestamp(evidence_snapshot)
        output_sha256 = getattr(evidence, "output_sha256", None)
        return (
            isinstance(asset, MediaAsset)
            and asset.kind == "video"
            and asset.mime_type == "video/mp4"
            and len(asset.data) >= 12
            and asset.data[4:8] == b"ftyp"
            and len(asset.data) <= _MAX_ASSET_BYTES
            and isinstance(getattr(evidence, "request_id", None), str)
            and _JOB_ID.fullmatch(evidence.request_id) is not None
            and getattr(evidence, "snapshot_sha256", None)
            == request.snapshot_sha256
            and isinstance(output_sha256, str)
            and _SHA256.fullmatch(output_sha256) is not None
            and hmac.compare_digest(
                hashlib.sha256(asset.data).hexdigest(), output_sha256
            )
            and trigger is not None
            and snapshot is not None
            and parsed_evidence_trigger == trigger
            and parsed_evidence_snapshot == snapshot
        )

    async def _dispatch_realtime_video(
        self,
        run_id: str,
        event: dict,
        group_id: int,
        *,
        account_ids: list[str],
        scheduled_at: float,
        expires_at: float,
    ) -> None:
        await self._wait_for_dispatch(
            scheduled_at=scheduled_at,
            expires_at=expires_at,
            account_ids=account_ids,
            group_id=group_id,
        )
        worker = self.manager.workers.get(event["account_id"])
        if worker is None:
            raise _RunFailed(f"account {event['account_id']} worker is not running")
        async with self._video_generation_lock:
            # Waiting for the manager-owned slot may take a full Wan render. Recheck
            # runtime/human boundaries before freezing this event's trigger.
            await self._wait_for_dispatch(
                scheduled_at=float(self._clock()),
                expires_at=expires_at,
                account_ids=account_ids,
                group_id=group_id,
            )
            trigger_timestamp = float(self._clock())
            trigger_received_at = self._iso_timestamp(trigger_timestamp)
            if trigger_received_at is None:
                raise _RunFailed(f"{event['event_id']} has invalid trigger time")
            generated = await self._generate_video_context(
                worker,
                group_id,
                run_id=run_id,
                event_id=event["event_id"],
                trigger_received_at=trigger_timestamp,
            )
            profile_id = int(worker.persona.get("age") or 0)
            snapshot = self._video_snapshot_fields(
                generated,
                run_id=run_id,
                event_id=event["event_id"],
                account_id=event["account_id"],
                group_id=group_id,
                trigger_received_at=trigger_timestamp,
                profile_id=profile_id,
            )
            if snapshot is None:
                raise _RunFailed(f"{event['event_id']} has no bound current context")
            snapshot_at, snapshot_sha256, context_prompt = snapshot
            request = VideoGenerationRequest(
                run_id=run_id,
                event_id=event["event_id"],
                account_id=event["account_id"],
                group_id=group_id,
                trigger_received_at=trigger_received_at,
                snapshot_at=snapshot_at,
                snapshot_sha256=snapshot_sha256,
                profile_id=profile_id,
                context_prompt=context_prompt,
            )
            bound = await self.video_client.generate(
                run_id=request.run_id,
                event_id=request.event_id,
                account_id=request.account_id,
                group_id=request.group_id,
                trigger_received_at=request.trigger_received_at,
                snapshot_at=request.snapshot_at,
                snapshot_sha256=request.snapshot_sha256,
                profile_id=request.profile_id,
                context_prompt=request.context_prompt,
            )
            if not self._bound_video_matches_request(bound, request):
                raise _RunFailed(
                    f"{event['event_id']} realtime video generation failed closed"
                )
            assert isinstance(bound, BoundVideoAsset)
            asset = bound.asset
            media_evidence = bound.media_evidence

        # A human may have spoken while Wan was rendering. Re-run every runtime,
        # group, duration and human-pause check immediately before the send gate.
        await self._wait_for_dispatch(
            scheduled_at=float(self._clock()),
            expires_at=expires_at,
            account_ids=account_ids,
            group_id=group_id,
        )
        sender: Any = getattr(worker, "send_live_test_asset", None)
        if not callable(sender):
            raise _RunFailed("worker lacks evidence-aware live-test media sender")
        sent = await sender(
            group_id,
            asset,
            event_id=event["event_id"],
            kind="video",
            media_evidence=media_evidence,
            marker="[影片]",
        )
        if not sent:
            raise _RunFailed(f"{event['event_id']} dispatch failed")

    async def _dispatch(
        self,
        run_id: str,
        event: dict,
        group_id: int,
        *,
        account_ids: list[str] | None = None,
        expires_at: float | None = None,
        video_enabled: bool = True,
    ) -> None:
        worker = self.manager.workers.get(event["account_id"])
        if worker is None:
            raise _RunFailed(f"account {event['account_id']} worker is not running")
        asset = None
        if event["kind"] == "video":
            raise _RunFailed("video dispatch must use realtime generation")
        if event["kind"] in {"image", "vision_reply"}:
            asset = self._load_asset(event)
        try:
            kind = event["kind"]
            if kind == "text":
                sent = await worker._send_text_recorded(
                    group_id,
                    event["text"],
                    activity_kind="live_test_text",
                    stats_key="proactive_sent",
                    managed_origin=True,
                    live_test_event_id=event["event_id"],
                    live_test_kind=kind,
                )
            elif kind == "voice":
                trigger_received_at = float(self._clock())
                generated = await worker.generate_realtime_voice_reply(
                    group_id,
                    run_id=run_id,
                    event_id=event["event_id"],
                    trigger_received_at=trigger_received_at,
                )
                if generated is None:
                    sent = False
                else:
                    async def before_voice_send(bound: Any) -> bool:
                        media_evidence = getattr(bound, "media_evidence", None)
                        asset = getattr(bound, "asset", None)
                        if (
                            getattr(bound, "run_id", None) != run_id
                            or getattr(bound, "event_id", None)
                            != event["event_id"]
                            or getattr(bound, "account_id", None)
                            != event["account_id"]
                            or getattr(bound, "group_id", None) != group_id
                            or getattr(bound, "profile_id", None)
                            != str(worker.persona.get("age") or "")
                            or media_evidence is None
                            or getattr(media_evidence, "snapshot_sha256", None)
                            != getattr(generated, "snapshot_sha256", None)
                            or not isinstance(asset, MediaAsset)
                            or asset.kind != "voice"
                            or not hmac.compare_digest(
                                hashlib.sha256(asset.data).hexdigest(),
                                str(getattr(media_evidence, "output_sha256", "")),
                            )
                        ):
                            return False
                        if account_ids is None or expires_at is None:
                            return True
                        await self._wait_for_dispatch(
                            scheduled_at=float(self._clock()),
                            expires_at=expires_at,
                            account_ids=account_ids,
                            group_id=group_id,
                            video_enabled=video_enabled,
                        )
                        return True

                    bound = await worker._send_realtime_voice(
                        generated,
                        live_test_event_id=event["event_id"],
                        live_test_kind=kind,
                        before_send=before_voice_send,
                    )
                    sent = bound is not None
            elif kind == "image":
                sent = await worker._send_media_recorded(
                    group_id,
                    asset,
                    "[圖片]",
                    activity_kind=f"live_test_{kind}",
                    stats_key="proactive_sent",
                    live_test_event_id=event["event_id"],
                    live_test_kind=kind,
                )
            else:
                service = getattr(worker, "media_service", None)
                if service is None or asset is None:
                    sent = False
                else:
                    reply = await service.understand_image(
                        event["account_id"],
                        asset.data,
                        asset.mime_type,
                        get_system_prompt(worker.persona),
                        "看懂這張已指定的測試圖片後，以自然繁體中文回覆，最多60字。",
                    )
                    reply = str(reply or "").strip()
                    sent = bool(reply and len(reply) <= 60) and await worker._send_text_recorded(
                        group_id,
                        reply,
                        activity_kind="live_test_vision_reply",
                        stats_key="proactive_sent",
                        managed_origin=True,
                        require_media_enabled=True,
                        live_test_event_id=event["event_id"],
                        live_test_kind=kind,
                    )
            if not sent:
                raise _RunFailed(f"{event['event_id']} dispatch failed")
        except asyncio.CancelledError:
            raise
        except _RunExpired:
            raise
        except _RunFailed:
            raise
        except Exception as exc:
            raise _RunFailed(f"{event['event_id']} dispatch failed") from exc

    async def _stop_accounts(
        self, account_ids: tuple[str, ...] | list[str]
    ) -> list[str]:
        errors: list[str] = []
        account_ids = tuple(str(value) for value in account_ids)

        # Make every run-owned worker fail closed in memory before the first
        # fallible persistence or disconnect operation.
        for account_id in account_ids:
            worker = self.manager.workers.get(account_id)
            if worker is not None:
                worker.is_running = False

        for account_id in account_ids:
            try:
                await self.db.update_account(account_id, enabled=0)
            except Exception as exc:
                errors.append(f"{account_id}: persist {type(exc).__name__}")
            try:
                result = await self.manager.stop(account_id)
                if result:
                    errors.append(f"{account_id}: {result}")
            except Exception as exc:
                errors.append(f"{account_id}: {type(exc).__name__}")
                worker = self.manager.workers.pop(account_id, None)
                if worker is not None:
                    try:
                        await worker.stop()
                    except Exception as fallback_exc:
                        errors.append(
                            f"{account_id}: fallback {type(fallback_exc).__name__}"
                        )

        for account_id in account_ids:
            worker = self.manager.workers.get(account_id)
            if worker is not None and (
                bool(getattr(worker, "is_running", False))
                or getattr(worker, "tg_client", None) is not None
            ):
                errors.append(f"{account_id}: worker still active")
            try:
                account = await self.db.get_account(account_id)
            except Exception as exc:
                errors.append(f"{account_id}: verify {type(exc).__name__}")
            else:
                if account is not None and int(account.get("enabled") or 0):
                    errors.append(f"{account_id}: still enabled")
        return errors

    async def _run(self, run_id: str, request: dict, started_at: float) -> None:
        status = "completed"
        reason = "schedule_complete"
        expires_at = started_at + request["duration_seconds"]
        video_tasks: list[asyncio.Task] = []
        try:
            for event in request["schedule"]:
                scheduled_at = started_at + event["offset_seconds"]
                if event["kind"] == "video":
                    video_tasks.append(
                        asyncio.create_task(
                            self._dispatch_realtime_video(
                                run_id,
                                event,
                                request["group_id"],
                                account_ids=request["account_ids"],
                                scheduled_at=scheduled_at,
                                expires_at=expires_at,
                            )
                        )
                    )
                    continue
                await self._wait_for_dispatch(
                    scheduled_at=scheduled_at,
                    expires_at=expires_at,
                    account_ids=request["account_ids"],
                    group_id=request["group_id"],
                    video_enabled=request["video_enabled"],
                )
                await self._dispatch(
                    run_id,
                    event,
                    request["group_id"],
                    account_ids=request["account_ids"],
                    expires_at=expires_at,
                    video_enabled=request["video_enabled"],
                )
            if video_tasks:
                results = await asyncio.gather(*video_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, _RunExpired):
                        raise result
                    if isinstance(result, _RunFailed):
                        raise result
                    if isinstance(result, BaseException):
                        raise _RunFailed(
                            f"video task failed: {type(result).__name__}"
                        ) from result
            while float(self._clock()) < expires_at:
                runtime_error = self._runtime_error(
                    request["account_ids"],
                    request["group_id"],
                    video_enabled=request["video_enabled"],
                )
                if runtime_error:
                    raise _RunFailed(runtime_error)
                remaining = expires_at - float(self._clock())
                await self._sleep(min(_MONITOR_SECONDS, remaining))
            reason = "duration_complete"
        except _RunExpired as exc:
            status, reason = "expired", str(exc)
        except _RunFailed as exc:
            status, reason = "failed", str(exc)
        except asyncio.CancelledError:
            status, reason = "stopped", "cancelled"
        except Exception as exc:
            status, reason = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            for task in video_tasks:
                if not task.done():
                    task.cancel()
            if video_tasks:
                await asyncio.gather(*video_tasks, return_exceptions=True)
            self.outbound_gate.lockdown(run_id)
            marker = getattr(self.db, "mark_live_test_needs_reconciliation", None)
            persist_errors: list[str] = []
            try:
                marked = (
                    await marker(run_id, "reconciliation_required: shutdown in progress")
                    if callable(marker)
                    else False
                )
                if not marked:
                    persist_errors.append("lockdown state was not persisted")
            except Exception as exc:
                persist_errors.append(f"lockdown persist {type(exc).__name__}")
            try:
                stop_errors = await self._stop_accounts(
                    tuple(request["account_ids"])
                )
            except Exception as exc:
                stop_errors = [f"shutdown {type(exc).__name__}"]
            if stop_errors:
                reason = "stop_failed: " + "; ".join(stop_errors)
            if not stop_errors:
                try:
                    await self.db.reconcile_live_test_events(run_id, reason)
                except Exception as exc:
                    persist_errors.append(
                        f"event reconciliation {type(exc).__name__}"
                    )
            if stop_errors or persist_errors:
                combined = "; ".join([reason, *persist_errors])
                if callable(marker):
                    try:
                        await marker(run_id, combined)
                    except Exception:
                        pass
                return
            persisted = await self.db.finish_live_test_run(run_id, status, reason)
            if persisted:
                self.outbound_gate.deactivate(run_id)
            elif callable(marker):
                try:
                    await marker(run_id, "terminal_persist_failed: " + str(reason))
                except Exception:
                    pass

    async def wait(self) -> dict:
        task = self._task
        if task is not None:
            await task
        return await self.status(self._run_id)

    async def status(self, run_id: str | None = None) -> dict:
        persisted = await self.db.get_live_test_run(run_id)
        if persisted is None:
            return {"status": "idle", "running": 0}
        account_ids = [str(value) for value in persisted.get("account_ids", [])]
        schedule = persisted.get("schedule") or []
        running = sum(
            1
            for account_id in account_ids
            if account_id in self.manager.workers
            and bool(getattr(self.manager.workers[account_id], "is_running", False))
        )
        result = {
            key: value
            for key, value in persisted.items()
            if key not in {"schedule", "account_ids"}
        }
        result["account_ids"] = account_ids
        result["schedule_count"] = len(schedule)
        result["video_enabled"] = any(
            isinstance(event, dict) and event.get("kind") == "video"
            for event in schedule
        )
        result["running"] = running
        result["remaining"] = max(
            0, int(result.get("event_cap", 0)) - int(result.get("reserved", 0))
        )
        return result

    async def stop(self, reason: str = "operator_stop") -> dict:
        task = self._task
        if task is None and self._run_id is None:
            return {"status": "idle", "running": 0}
        if task is not None and not task.done():
            task.cancel()
            await task
        else:
            persisted = await self.db.get_live_test_run(self._run_id)
            if persisted and persisted.get("status") == "running":
                run_id = str(persisted["id"])
                account_ids = tuple(
                    str(value) for value in persisted.get("account_ids", [])
                )
                self.outbound_gate.lockdown(run_id)
                stop_errors = await self._stop_accounts(account_ids)
                terminal_status = "failed" if stop_errors else "stopped"
                terminal_reason = reason
                if stop_errors:
                    terminal_reason = "stop_failed: " + "; ".join(stop_errors)
                saved = await self.db.finish_live_test_run(
                    run_id, terminal_status, terminal_reason
                )
                if saved and not stop_errors:
                    self.outbound_gate.deactivate(run_id)
        return await self.status(self._run_id)
