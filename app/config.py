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
    tg_session_string: str
    ai_api_key: str
    ai_base_url: str
    ai_model: str
    account_gender: str
    account_stage: str
    account_style: str
    group_chat_ids: frozenset[int]
    ignore_sender_ids: frozenset[int]
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
    memory_ttl_hours: int
    memory_history_limit: int
    memory_db_path: str
    log_level: str

    @property
    def role_key(self) -> str:
        return f"{self.account_gender}_{self.account_stage}"


def load_settings() -> Settings:
    gender = os.getenv("ACCOUNT_GENDER", "").strip().lower()
    stage = os.getenv("ACCOUNT_STAGE", "").strip().lower()
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

    return Settings(
        tg_api_id=int(_required("TG_API_ID")),
        tg_api_hash=_required("TG_API_HASH"),
        tg_session_string=_required("TG_SESSION_STRING"),
        ai_api_key=_required("AI_API_KEY"),
        ai_base_url=os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        ai_model=_required("AI_MODEL"),
        account_gender=gender,
        account_stage=stage,
        account_style=os.getenv("ACCOUNT_STYLE", "").strip(),
        group_chat_ids=_id_set("GROUP_CHAT_IDS"),
        ignore_sender_ids=_id_set("IGNORE_SENDER_IDS"),
        group_reply_probability=_number("GROUP_REPLY_PROBABILITY", 0.35, 0, 1),
        reply_on_mention=_boolean("REPLY_ON_MENTION", True),
        reply_on_reply=_boolean("REPLY_ON_REPLY", True),
        typing_delay_min_seconds=delay_min,
        typing_delay_max_seconds=delay_max,
        proactive_enabled=_boolean("PROACTIVE_ENABLED", True),
        proactive_idle_minutes=_integer("PROACTIVE_IDLE_MINUTES", 15, 1),
        proactive_min_interval_minutes=proactive_min,
        proactive_max_interval_minutes=proactive_max,
        max_proactive_per_day=_integer("MAX_PROACTIVE_PER_DAY", 24, 0),
        memory_ttl_hours=_integer("MEMORY_TTL_HOURS", 24, 1),
        memory_history_limit=_integer("MEMORY_HISTORY_LIMIT", 30, 1),
        memory_db_path=os.getenv("MEMORY_DB_PATH", "/data/memory.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
