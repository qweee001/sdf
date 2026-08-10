from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse

from dotenv import load_dotenv


load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _integer(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = int(os.getenv(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _id_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    try:
        return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must contain comma-separated numeric IDs") from exc


def _media_base_url() -> str:
    value = (
        os.getenv("OPENROUTER_MEDIA_BASE_URL", "").strip()
        or os.getenv("OPENROUTER_BASE_URL", "").strip()
        or os.getenv("OPENAI_MEDIA_BASE_URL", "").strip()
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "OPENROUTER_MEDIA_BASE_URL must be an HTTPS URL without credentials, "
            "query, or fragment"
        )
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(
        (".local", ".localhost", ".internal", ".lan", ".home")
    ):
        raise ValueError("OPENROUTER_MEDIA_BASE_URL cannot target a local host")
    try:
        address = ip_address(host)
    except ValueError:
        return value
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError("OPENROUTER_MEDIA_BASE_URL cannot target a private address")
    return value


def _azure_speech_region() -> str:
    value = os.getenv("AZURE_SPEECH_REGION", "").strip()
    if value and re.fullmatch(r"[a-z0-9-]{2,40}", value) is None:
        raise ValueError(
            "AZURE_SPEECH_REGION must contain 2-40 lowercase letters, "
            "numbers, or hyphens"
        )
    return value


@dataclass(frozen=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    legacy_session_string: str
    ai_api_key: str
    ai_base_url: str
    ai_model: str
    account_encryption_key: str
    memory_ttl_hours: int
    memory_history_limit: int
    memory_db_path: str
    dashboard_enabled: bool
    dashboard_username: str
    dashboard_password: str
    dashboard_port: int
    max_accounts: int
    log_level: str
    legacy_gender: str
    legacy_stage: str
    legacy_style: str
    legacy_group_ids: frozenset[int]
    legacy_ignore_sender_ids: frozenset[int]
    legacy_group_reply_probability: float
    legacy_reply_on_mention: bool
    legacy_reply_on_reply: bool
    legacy_typing_delay_min_seconds: float
    legacy_typing_delay_max_seconds: float
    legacy_proactive_enabled: bool
    legacy_proactive_idle_minutes: int
    legacy_proactive_min_interval_minutes: int
    legacy_proactive_max_interval_minutes: int
    legacy_max_proactive_per_day: int
    openai_media_api_key: str = ""
    openai_media_base_url: str = "https://openrouter.ai/api/v1"
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    media_image_model: str = "x-ai/grok-imagine-image-quality"
    media_tts_model: str = "x-ai/grok-voice-tts-1.0"
    media_video_model: str = "x-ai/grok-imagine-video-1.5"
    media_moderation_model: str = "x-ai/grok-4.20"
    ai_uses_openrouter_key: bool = False
    migrate_existing_accounts_to_grok_adult: bool = False
    # 本地部署：explicit client（Venice 露骨/审计）独立 key，默认回退 ai_api_key
    explicit_ai_api_key: str = ""
    # 本地部署：explicit client 独立 base_url（Venice/OpenRouter），默认回退 ai_base_url
    explicit_ai_base_url: str = ""
    # 本地部署：explicit client 独立模型（Venice 无审查），默认回退 ai_model
    explicit_ai_model: str = ""
    # 本地部署：放行 localhost/私有 IP 的 AI Base URL（本地 TAIDE 无认证）
    allow_local_ai_url: bool = False

    @property
    def media_provider_readiness(self) -> dict[str, bool]:
        media_ready = bool(self.openai_media_api_key)
        return {
            "openrouter_media": media_ready,
            "azure_speech": bool(
                self.azure_speech_key and self.azure_speech_region
            ),
        }

    @property
    def media_uses_openrouter(self) -> bool:
        host = (urlparse(self.openai_media_base_url).hostname or "").lower()
        return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def load_settings() -> Settings:
    gender = os.getenv("ACCOUNT_GENDER", "male").strip().lower()
    stage = os.getenv("ACCOUNT_STAGE", "old_member").strip().lower()
    if gender not in {"male", "female"}:
        raise ValueError("ACCOUNT_GENDER must be male or female")
    if stage not in {"old_member", "observer"}:
        raise ValueError("ACCOUNT_STAGE must be old_member or observer")

    delay_min = _number("TYPING_DELAY_MIN_SECONDS", 1.5, 0, 60)
    delay_max = _number("TYPING_DELAY_MAX_SECONDS", 5, 0, 60)
    if delay_max < delay_min:
        raise ValueError("TYPING_DELAY_MAX_SECONDS cannot be lower than the minimum")

    proactive_min = _integer("PROACTIVE_MIN_INTERVAL_MINUTES", 25, 1)
    proactive_max = _integer("PROACTIVE_MAX_INTERVAL_MINUTES", 60, 1)
    if proactive_max < proactive_min:
        raise ValueError("PROACTIVE_MAX_INTERVAL_MINUTES cannot be lower than the minimum")

    # Railway's config-as-code healthcheck targets the dashboard's /health
    # route, so the deployable default keeps the password-protected dashboard
    # enabled. Operators can still opt out when not using railway.json.
    dashboard_enabled = _boolean("DASHBOARD_ENABLED", True)
    dashboard_password = os.getenv("DASHBOARD_PASSWORD", "")
    if dashboard_enabled and len(dashboard_password) < 12:
        raise ValueError("DASHBOARD_PASSWORD must contain at least 12 characters")

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    ai_api_key = openrouter_api_key or os.getenv("AI_API_KEY", "").strip()
    ai_base_url = (
        os.getenv("AI_BASE_URL", "").strip()
        or os.getenv("OPENROUTER_BASE_URL", "").strip()
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")

    return Settings(
        tg_api_id=int(_required("TG_API_ID")),
        tg_api_hash=_required("TG_API_HASH"),
        legacy_session_string=os.getenv("TG_SESSION_STRING", "").strip(),
        ai_api_key=ai_api_key,
        ai_base_url=ai_base_url,
        ai_model=os.getenv(
            "AI_MODEL",
            "cognitivecomputations/dolphin-mistral-24b-venice-edition",
        ).strip()
        or "cognitivecomputations/dolphin-mistral-24b-venice-edition",
        account_encryption_key=_required("ACCOUNT_ENCRYPTION_KEY"),
        memory_ttl_hours=_integer("MEMORY_TTL_HOURS", 24, 1),
        memory_history_limit=_integer("MEMORY_HISTORY_LIMIT", 20, 1),
        memory_db_path=os.getenv("MEMORY_DB_PATH", "/data/memory.db"),
        dashboard_enabled=dashboard_enabled,
        dashboard_username=os.getenv("DASHBOARD_USERNAME", "admin").strip() or "admin",
        dashboard_password=dashboard_password,
        dashboard_port=_integer("PORT", 8000, 1),
        # Railway may lower this per environment, while the console itself
        # remains capped at the requested 20 managed accounts.
        max_accounts=_integer("MAX_ACCOUNTS", 20, 1, 20),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        legacy_gender=gender,
        legacy_stage=stage,
        legacy_style=os.getenv("ACCOUNT_STYLE", "").strip(),
        legacy_group_ids=_id_set("GROUP_CHAT_IDS"),
        legacy_ignore_sender_ids=_id_set("IGNORE_SENDER_IDS"),
        legacy_group_reply_probability=_number("GROUP_REPLY_PROBABILITY", 0.35, 0, 1),
        legacy_reply_on_mention=_boolean("REPLY_ON_MENTION", True),
        legacy_reply_on_reply=_boolean("REPLY_ON_REPLY", True),
        legacy_typing_delay_min_seconds=delay_min,
        legacy_typing_delay_max_seconds=delay_max,
        legacy_proactive_enabled=_boolean("PROACTIVE_ENABLED", True),
        legacy_proactive_idle_minutes=_integer("PROACTIVE_IDLE_MINUTES", 15, 1),
        legacy_proactive_min_interval_minutes=proactive_min,
        legacy_proactive_max_interval_minutes=proactive_max,
        legacy_max_proactive_per_day=_integer("MAX_PROACTIVE_PER_DAY", 24, 0),
        openai_media_api_key=(
            os.getenv("OPENROUTER_MEDIA_API_KEY", "").strip()
            or openrouter_api_key
            or os.getenv("OPENAI_MEDIA_API_KEY", "").strip()
        ),
        openai_media_base_url=_media_base_url(),
        azure_speech_key=os.getenv("AZURE_SPEECH_KEY", "").strip(),
        azure_speech_region=_azure_speech_region(),
        media_image_model=(
            os.getenv("MEDIA_IMAGE_MODEL", "").strip()
            or "x-ai/grok-imagine-image-quality"
        ),
        media_tts_model=(
            os.getenv("MEDIA_TTS_MODEL", "").strip()
            or "x-ai/grok-voice-tts-1.0"
        ),
        media_video_model=(
            os.getenv("MEDIA_VIDEO_MODEL", "").strip()
            or "x-ai/grok-imagine-video-1.5"
        ),
        media_moderation_model=(
            os.getenv("MEDIA_MODERATION_MODEL", "").strip()
            or "x-ai/grok-4.20"
        ),
        ai_uses_openrouter_key=bool(openrouter_api_key),
        migrate_existing_accounts_to_grok_adult=_boolean(
            "MIGRATE_EXISTING_ACCOUNTS_TO_GROK_ADULT",
            False,
        ),
        explicit_ai_api_key=(
            os.getenv("EXPLICIT_AI_API_KEY", "").strip()
            or os.getenv("VENICE_API_KEY", "").strip()
        ),
        explicit_ai_base_url=(
            os.getenv("EXPLICIT_AI_BASE_URL", "").strip()
            or os.getenv("OPENROUTER_BASE_URL", "").strip()
            or "https://openrouter.ai/api/v1"
        ),
        explicit_ai_model=(
            os.getenv("EXPLICIT_AI_MODEL", "").strip()
            or os.getenv("AI_MODEL", "").strip()
        ),
        allow_local_ai_url=_boolean("ALLOW_LOCAL_AI_URL", False),
    )
