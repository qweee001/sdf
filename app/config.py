from __future__ import annotations

import os
from dataclasses import dataclass

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


def _integer(name: str, default: int, minimum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
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

    dashboard_enabled = _boolean("DASHBOARD_ENABLED", False)
    dashboard_password = os.getenv("DASHBOARD_PASSWORD", "")
    if dashboard_enabled and len(dashboard_password) < 12:
        raise ValueError("DASHBOARD_PASSWORD must contain at least 12 characters")

    return Settings(
        tg_api_id=int(_required("TG_API_ID")),
        tg_api_hash=_required("TG_API_HASH"),
        legacy_session_string=os.getenv("TG_SESSION_STRING", "").strip(),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_base_url=os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        ai_model=os.getenv("AI_MODEL", "gpt-5-mini").strip() or "gpt-5-mini",
        account_encryption_key=_required("ACCOUNT_ENCRYPTION_KEY"),
        memory_ttl_hours=_integer("MEMORY_TTL_HOURS", 24, 1),
        memory_history_limit=_integer("MEMORY_HISTORY_LIMIT", 30, 1),
        memory_db_path=os.getenv("MEMORY_DB_PATH", "/data/memory.db"),
        dashboard_enabled=dashboard_enabled,
        dashboard_username=os.getenv("DASHBOARD_USERNAME", "admin").strip() or "admin",
        dashboard_password=dashboard_password,
        dashboard_port=_integer("PORT", 8000, 1),
        max_accounts=_integer("MAX_ACCOUNTS", 10, 1),
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
    )
