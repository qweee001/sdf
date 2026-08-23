from __future__ import annotations

import math
import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少必要環境變數: {name}")
    return value


def _float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
        return value if math.isfinite(value) else float(default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _hosts(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip() or default
    return tuple(
        sorted(
            {
                item.strip().lower().rstrip(".")
                for item in raw.split(",")
                if item.strip()
            }
        )
    )


@dataclass
class Settings:
    acceptance_test_mode: bool
    # Telegram
    tg_api_id: int
    tg_api_hash: str

    # 加密（帳號 session 用）
    account_encryption_key: str

    # AI（預設 RunPod 本機部署）
    ai_api_key: str
    ai_base_url: str
    ai_model: str
    ai_temperature: float
    ai_max_tokens: int
    ai_timeout: float
    ai_disable_thinking: bool
    vision_model: str
    image_model: str
    speech_model: str
    video_model: str
    media_enabled: bool
    media_daily_budget_usd: float
    media_max_input_bytes: int
    media_generation_timeout: float
    media_download_hosts: tuple[str, ...]

    # 控制台
    dashboard_user: str
    dashboard_pass: str
    dashboard_port: int

    # 資料庫
    db_path: str

    # 記憶
    memory_max_messages: int
    memory_ttl_hours: int

    # 回覆行為
    base_reply_probability: float
    water_cross_talk_probability: float

    # 主動發言
    proactive_enabled: bool
    proactive_max_per_day: int
    proactive_min_interval_minutes: float
    proactive_loop_min_seconds: float
    proactive_loop_max_seconds: float

    # 打字延遲（秒）
    min_typing_delay: float
    max_typing_delay: float


def load_settings() -> Settings:
    ai_model = os.getenv("AI_MODEL", "").strip()
    acceptance_test_mode = _bool("ACCEPTANCE_TEST_MODE", False)
    return Settings(
        acceptance_test_mode=acceptance_test_mode,
        tg_api_id=int(_required("TG_API_ID")),
        tg_api_hash=_required("TG_API_HASH"),
        account_encryption_key=_required("ACCOUNT_ENCRYPTION_KEY"),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_base_url=(
            os.getenv("AI_BASE_URL", "").strip()
            or "https://9ghyzu98lbv2mf-8000.proxy.runpod.net/v1"
        ).rstrip("/"),
        ai_model=ai_model,
        ai_temperature=_float("AI_TEMPERATURE", 0.85),
        ai_max_tokens=_int("AI_MAX_TOKENS", 200),
        ai_timeout=_float("AI_TIMEOUT", 60),
        ai_disable_thinking=_bool("AI_DISABLE_THINKING", False),
        vision_model=os.getenv("VISION_MODEL", "").strip() or ai_model,
        image_model=(
            os.getenv("IMAGE_MODEL", "").strip()
            or "google/imagen-4.0-fast-generate-001"
        ),
        speech_model=(
            os.getenv("SPEECH_MODEL", "").strip()
            or "openai/tts-1"
        ),
        video_model=(
            os.getenv("VIDEO_MODEL", "").strip()
            or "minimax/minimax-h3"
        ),
        media_enabled=_bool("MEDIA_ENABLED", False),
        media_daily_budget_usd=_float("MEDIA_DAILY_BUDGET_USD", 10.0),
        media_max_input_bytes=_int(
            "MEDIA_MAX_INPUT_BYTES", 8 * 1024 * 1024
        ),
        media_generation_timeout=_float(
            "MEDIA_GENERATION_TIMEOUT", 300
        ),
        media_download_hosts=_hosts(
            "MEDIA_DOWNLOAD_HOSTS",
            "cdn.hailuoai.com,*.hailuoai.com,*.minimax.io,*.minimax.chat",
        ),
        dashboard_user=os.getenv("DASHBOARD_USER", "admin").strip(),
        dashboard_pass=_required("DASHBOARD_PASS"),
        dashboard_port=_int("PORT", 8000),
        db_path=os.getenv("DB_PATH", "/data/chat.db").strip(),
        memory_max_messages=_int("MEMORY_MAX_MESSAGES", 30),
        memory_ttl_hours=_int("MEMORY_TTL_HOURS", 24),
        base_reply_probability=_float("BASE_REPLY_PROBABILITY", 0.35),
        water_cross_talk_probability=(
            1.0
            if acceptance_test_mode
            else _float("WATER_CROSS_TALK_PROBABILITY", 0.65)
        ),
        proactive_enabled=_bool("PROACTIVE_ENABLED", True),
        proactive_max_per_day=_int("PROACTIVE_MAX_PER_DAY", 4),
        proactive_min_interval_minutes=(
            1.0
            if acceptance_test_mode
            else _float("PROACTIVE_MIN_INTERVAL_MINUTES", 45)
        ),
        proactive_loop_min_seconds=(
            5.0
            if acceptance_test_mode
            else _float("PROACTIVE_LOOP_MIN_SECONDS", 240)
        ),
        proactive_loop_max_seconds=(
            8.0
            if acceptance_test_mode
            else _float("PROACTIVE_LOOP_MAX_SECONDS", 720)
        ),
        min_typing_delay=_float("MIN_TYPING_DELAY", 1.5),
        max_typing_delay=_float("MAX_TYPING_DELAY", 4.0),
    )
