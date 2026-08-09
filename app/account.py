from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from ipaddress import ip_address
from urllib.parse import urlparse

from .adult_safety import (
    adult_text_enabled_for_mode,
    adult_text_mode_from_legacy,
    clean_adult_text_mode,
    resolve_adult_text_mode,
)
from .media_types import AccountMediaSettings


ROLE_GENDERS = {"male", "female"}
ROLE_STAGES = {"old_member", "observer"}


@dataclass(frozen=True)
class AccountRecord:
    id: str
    label: str
    session_ciphertext: str
    session_fingerprint: str
    telegram_user_id: int
    telegram_name: str
    enabled: bool
    gender: str
    stage: str
    style: str
    task_name: str
    task_info: str
    ai_base_url: str
    ai_model: str
    ai_api_key_ciphertext: str
    group_reply_probability: float
    reply_on_mention: bool
    reply_on_reply: bool
    typing_delay_min_seconds: float
    typing_delay_max_seconds: float
    proactive_enabled: bool
    proactive_idle_minutes: int
    proactive_min_interval_minutes: int
    proactive_max_interval_minutes: int
    max_proactive_per_day: int
    all_groups: bool
    group_ids: frozenset[int]
    revision: int
    created_at: int
    updated_at: int
    blocked_terms: tuple[str, ...] = ()
    blocked_topics: tuple[str, ...] = ()
    media_settings: AccountMediaSettings = field(
        default_factory=AccountMediaSettings
    )
    adult_text_enabled: bool | None = None
    adult_text_mode: str = ""
    # 账号人格状态（JSON）：{"preferences": {...}, "memory": {...}, "mood": {...}}
    account_state: str = ""

    def __post_init__(self) -> None:
        if self.adult_text_mode:
            mode = resolve_adult_text_mode(
                self.adult_text_mode,
                **(
                    {"adult_text_enabled": self.adult_text_enabled}
                    if self.adult_text_enabled is not None
                    else {}
                ),
            )
        elif self.adult_text_enabled is not None:
            mode = adult_text_mode_from_legacy(self.adult_text_enabled)
        else:
            mode = "strict"
        object.__setattr__(self, "adult_text_mode", mode)
        object.__setattr__(
            self,
            "adult_text_enabled",
            adult_text_enabled_for_mode(mode),
        )

    @property
    def role_key(self) -> str:
        return f"{self.gender}_{self.stage}"

    @property
    def has_custom_api_key(self) -> bool:
        return bool(self.ai_api_key_ciphertext)

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "telegram_user_id": self.telegram_user_id,
            "telegram_name": self.telegram_name,
            "session_configured": bool(self.session_ciphertext),
            "enabled": self.enabled,
            "gender": self.gender,
            "stage": self.stage,
            "role": self.role_key,
            "style": self.style,
            "task_name": self.task_name,
            "task_info": self.task_info,
            "ai_base_url": self.ai_base_url,
            "ai_model": self.ai_model,
            "has_custom_api_key": self.has_custom_api_key,
            "group_reply_probability": self.group_reply_probability,
            "reply_on_mention": self.reply_on_mention,
            "reply_on_reply": self.reply_on_reply,
            "typing_delay_min_seconds": self.typing_delay_min_seconds,
            "typing_delay_max_seconds": self.typing_delay_max_seconds,
            "proactive_enabled": self.proactive_enabled,
            "proactive_idle_minutes": self.proactive_idle_minutes,
            "proactive_min_interval_minutes": self.proactive_min_interval_minutes,
            "proactive_max_interval_minutes": self.proactive_max_interval_minutes,
            "max_proactive_per_day": self.max_proactive_per_day,
            "all_groups": self.all_groups,
            "group_ids": sorted(self.group_ids),
            "blocked_terms": list(self.blocked_terms),
            "blocked_topics": list(self.blocked_topics),
            "media": self.media_settings.public_dict(),
            "adult_text_enabled": self.adult_text_enabled,
            "adult_text_mode": self.adult_text_mode,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def with_updates(self, **changes: object) -> AccountRecord:
        if "adult_text_mode" in changes and "adult_text_enabled" in changes:
            resolve_adult_text_mode(
                changes["adult_text_mode"],
                adult_text_enabled=changes["adult_text_enabled"],
            )
        elif "adult_text_mode" in changes:
            mode = clean_adult_text_mode(changes["adult_text_mode"])
            changes["adult_text_enabled"] = adult_text_enabled_for_mode(mode)
        elif "adult_text_enabled" in changes:
            changes["adult_text_mode"] = adult_text_mode_from_legacy(
                changes["adult_text_enabled"]
            )
        return replace(self, **changes)


def clean_text(value: object, name: str, *, maximum: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} is required")
    if len(text) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return text


def clean_account_state(value: object) -> str:
    """校验账号人格状态 JSON（偏好/记忆/情绪）。空值允许，非法 JSON 拒绝。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 4000:
        raise ValueError("account_state must be at most 4000 characters")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("account_state must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("account_state must be a JSON object")
    return text


def clean_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def clean_int(
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


def clean_float(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def clean_role(gender: object, stage: object) -> tuple[str, str]:
    cleaned_gender = clean_text(gender, "gender", maximum=10, required=True).lower()
    cleaned_stage = clean_text(stage, "stage", maximum=20, required=True).lower()
    if cleaned_gender not in ROLE_GENDERS:
        raise ValueError("gender must be male or female")
    if cleaned_stage not in ROLE_STAGES:
        raise ValueError("stage must be old_member or observer")
    return cleaned_gender, cleaned_stage


def clean_group_ids(value: object) -> frozenset[int]:
    if not isinstance(value, list) or len(value) > 500:
        raise ValueError("group_ids must be a list with at most 500 IDs")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError("group_ids must contain integers")
    return frozenset(value)


def clean_string_list(
    value: object,
    name: str,
    *,
    maximum_items: int,
    maximum_item_length: int,
    maximum_total_length: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(
            f"{name} must be a list with at most {maximum_items} items"
        )
    cleaned: list[str] = []
    seen: set[str] = set()
    total_length = 0
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{name} must contain only strings")
        normalized = unicodedata.normalize("NFKC", item).strip()
        if any(
            unicodedata.category(char).startswith("C")
            for char in normalized
        ):
            raise ValueError(f"{name} cannot contain control characters")
        normalized = re.sub(r"\s+", " ", normalized)
        if not normalized:
            continue
        if len(normalized) > maximum_item_length:
            raise ValueError(
                f"Each {name} item must be at most "
                f"{maximum_item_length} characters"
            )
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        total_length += len(normalized)
        if total_length > maximum_total_length:
            raise ValueError(
                f"{name} total length must be at most "
                f"{maximum_total_length} characters"
            )
        cleaned.append(normalized)
    return tuple(cleaned)


def validate_provider_url(value: object) -> str:
    url = clean_text(value, "ai_base_url", maximum=500, required=True).rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("AI Base URL must be an HTTPS URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("AI Base URL cannot contain a query string or fragment")
    host = parsed.hostname.lower().rstrip(".")
    if (
        host == "localhost"
        or host.endswith((".local", ".localhost", ".internal", ".lan", ".home"))
    ):
        raise ValueError("Local AI Base URLs are not allowed")
    try:
        address = ip_address(host)
    except ValueError:
        return url
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError("Private or reserved AI Base URLs are not allowed")
    return url
