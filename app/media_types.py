from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Mapping


MEDIA_KINDS = frozenset({"image", "voice", "video"})
MEDIA_JOB_STATUSES = frozenset(
    {"queued", "running", "completed", "failed", "cancelled"}
)
MAX_MEDIA_GROUPS = 500
MAX_MEDIA_DAILY_LIMIT = 1000
MAX_MEDIA_COOLDOWN_SECONDS = 7 * 24 * 60 * 60
MAX_MEDIA_JOB_PAYLOAD_BYTES = 64 * 1024


def clean_media_kind(value: object) -> str:
    kind = str(value or "").strip().lower()
    if kind not in MEDIA_KINDS:
        raise ValueError("media_type must be image, voice, or video")
    return kind


def _clean_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _clean_int(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _clean_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    cleaned = unicodedata.normalize("NFKC", value).strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    if any(unicodedata.category(char).startswith("C") for char in cleaned):
        raise ValueError(f"{name} cannot contain control characters")
    return cleaned


def _clean_group_ids(value: object, name: str) -> frozenset[int]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"{name} must be a list of group IDs")
    if len(value) > MAX_MEDIA_GROUPS:
        raise ValueError(f"{name} may contain at most {MAX_MEDIA_GROUPS} IDs")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{name} must contain only integer group IDs")
    return frozenset(value)


@dataclass(frozen=True)
class MediaFeatureSettings:
    enabled: bool = False
    model: str = ""
    voice: str = ""
    daily_limit: int = 0
    cooldown_seconds: int = 0
    allowed_group_ids: frozenset[int] = field(default_factory=frozenset)

    def public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "voice": self.voice,
            "daily_limit": self.daily_limit,
            "cooldown_seconds": self.cooldown_seconds,
            "allowed_group_ids": sorted(self.allowed_group_ids),
        }

    def allows_group(self, group_id: int) -> bool:
        return self.enabled and group_id in self.allowed_group_ids


@dataclass(frozen=True)
class AccountMediaSettings:
    image: MediaFeatureSettings = field(default_factory=MediaFeatureSettings)
    voice: MediaFeatureSettings = field(default_factory=MediaFeatureSettings)
    video: MediaFeatureSettings = field(default_factory=MediaFeatureSettings)

    def public_dict(self) -> dict[str, object]:
        return {
            "image": self.image.public_dict(),
            "voice": self.voice.public_dict(),
            "video": self.video.public_dict(),
        }

    def for_kind(self, media_type: str) -> MediaFeatureSettings:
        return getattr(self, clean_media_kind(media_type))


def clean_media_feature_settings(
    value: object,
    name: str,
    *,
    current: MediaFeatureSettings | None = None,
) -> MediaFeatureSettings:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    allowed = {
        "enabled",
        "model",
        "voice",
        "daily_limit",
        "cooldown_seconds",
        "allowed_group_ids",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} contains unsupported fields: {', '.join(sorted(unknown))}")
    base = current or MediaFeatureSettings()
    return MediaFeatureSettings(
        enabled=_clean_bool(value.get("enabled", base.enabled), f"{name}.enabled"),
        model=_clean_text(value.get("model", base.model), f"{name}.model", 200),
        voice=_clean_text(value.get("voice", base.voice), f"{name}.voice", 120),
        daily_limit=_clean_int(
            value.get("daily_limit", base.daily_limit),
            f"{name}.daily_limit",
            minimum=0,
            maximum=MAX_MEDIA_DAILY_LIMIT,
        ),
        cooldown_seconds=_clean_int(
            value.get("cooldown_seconds", base.cooldown_seconds),
            f"{name}.cooldown_seconds",
            minimum=0,
            maximum=MAX_MEDIA_COOLDOWN_SECONDS,
        ),
        allowed_group_ids=_clean_group_ids(
            value.get("allowed_group_ids", base.allowed_group_ids),
            f"{name}.allowed_group_ids",
        ),
    )


def clean_account_media_settings(
    value: object,
    *,
    current: AccountMediaSettings | None = None,
) -> AccountMediaSettings:
    if not isinstance(value, Mapping):
        raise ValueError("media must be an object")
    unknown = set(value) - MEDIA_KINDS
    if unknown:
        raise ValueError(f"media contains unsupported fields: {', '.join(sorted(unknown))}")
    base = current or AccountMediaSettings()
    return AccountMediaSettings(
        image=clean_media_feature_settings(
            value.get("image", {}),
            "media.image",
            current=base.image,
        ),
        voice=clean_media_feature_settings(
            value.get("voice", {}),
            "media.voice",
            current=base.voice,
        ),
        video=clean_media_feature_settings(
            value.get("video", {}),
            "media.video",
            current=base.video,
        ),
    )


def media_settings_from_json(raw_value: object) -> AccountMediaSettings:
    if not isinstance(raw_value, str):
        raise ValueError("stored media settings must be JSON text")
    try:
        decoded = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("stored media settings are invalid") from exc
    return clean_account_media_settings(decoded)


def safe_media_job_payload(value: object) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("media job payload must be an object")

    forbidden_keys = {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "session",
        "token",
    }

    def check(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized_key = str(key).strip().lower()
                if (
                    normalized_key in forbidden_keys
                    or normalized_key.endswith(("_api_key", "_password", "_secret"))
                    or normalized_key.startswith(("authorization_", "session_"))
                ):
                    raise ValueError("media job payload cannot contain credentials")
                check(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                check(nested)

    check(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("media job payload must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_MEDIA_JOB_PAYLOAD_BYTES:
        raise ValueError(
            f"media job payload must be at most {MAX_MEDIA_JOB_PAYLOAD_BYTES} bytes"
        )
    return encoded


@dataclass(frozen=True)
class MediaJob:
    id: str
    account_id: str
    group_id: int
    media_type: str
    status: str
    payload: dict[str, object]
    result_ref: str
    error: str
    attempts: int
    available_at: int
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class MediaQuotaDecision:
    allowed: bool
    reason: str
    used: int
    remaining: int
    retry_after_seconds: int


@dataclass(frozen=True)
class MediaJobReservation:
    quota: MediaQuotaDecision
    job: MediaJob | None


def utc_day_key(timestamp: int | None = None) -> str:
    current = int(time.time()) if timestamp is None else timestamp
    return time.strftime("%Y-%m-%d", time.gmtime(current))
