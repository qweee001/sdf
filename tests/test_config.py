import math

import pytest

from app.config import load_settings


def test_load_settings_full():
    s = load_settings()
    assert s.tg_api_id == 30279608
    assert s.tg_api_hash == "test_hash"
    assert s.dashboard_port == 8000
    assert s.ai_model == "test-model"
    assert 0 < s.base_reply_probability < 1


def test_missing_required_var_raises():
    import os

    old = os.environ.pop("TG_API_ID")
    try:
        with pytest.raises(ValueError, match="TG_API_ID"):
            load_settings()
    finally:
        os.environ["TG_API_ID"] = old


def test_defaults_present():
    s = load_settings()
    assert s.memory_max_messages == 30
    assert s.memory_ttl_hours == 24
    assert s.min_typing_delay < s.max_typing_delay
    assert s.proactive_max_per_day >= 1
    assert s.water_cross_talk_probability == 0.65
    assert s.proactive_loop_min_seconds == 240
    assert s.proactive_loop_max_seconds == 720
    assert s.acceptance_test_mode is False
    assert s.continuous_activity_mode is False
    assert s.continuous_activity_interval_seconds == 10.0
    assert s.ai_disable_thinking is False
    assert s.vision_model == "gemini-3.5-flash-lite"
    assert s.image_model == "google/imagen-4.0-fast-generate-001"
    assert s.image_fallback_model == "google/imagen-4.0-generate-001"
    assert s.speech_model
    assert s.video_model
    assert s.media_enabled is True
    assert s.voice_media_enabled is False
    assert s.media_daily_budget_usd == 10.0
    assert s.media_max_input_bytes == 8 * 1024 * 1024


def test_ai_disable_thinking_from_env(monkeypatch):
    monkeypatch.setenv("AI_DISABLE_THINKING", "true")
    assert load_settings().ai_disable_thinking is True


def test_acceptance_mode_is_deterministic_and_bounded(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_TEST_MODE", "true")
    s = load_settings()
    assert s.acceptance_test_mode is True
    assert s.water_cross_talk_probability == 1.0
    assert s.proactive_min_interval_minutes == 1.0
    assert s.proactive_loop_min_seconds == 5.0
    assert s.proactive_loop_max_seconds == 8.0


def test_continuous_activity_env_is_explicit_and_interval_is_bounded(monkeypatch):
    monkeypatch.setenv("CONTINUOUS_ACTIVITY_MODE", "true")
    monkeypatch.setenv("CONTINUOUS_ACTIVITY_INTERVAL_SECONDS", "1")
    settings = load_settings()
    assert settings.continuous_activity_mode is True
    assert settings.continuous_activity_interval_seconds == 10.0


def test_non_finite_media_budget_falls_back_to_finite_default(monkeypatch):
    monkeypatch.setenv("MEDIA_DAILY_BUDGET_USD", "NaN")
    budget = load_settings().media_daily_budget_usd
    assert math.isfinite(budget)
    assert budget == 10.0


def test_realtime_video_config_uses_url_token_and_bounded_timing(monkeypatch):
    monkeypatch.setenv("VIDEO_REALTIME_URL", "https://wan.example/")
    monkeypatch.setenv("VIDEO_REALTIME_TOKEN", "wan-token")
    monkeypatch.setenv("VIDEO_REALTIME_REQUEST_TIMEOUT", "-50")
    monkeypatch.setenv("VIDEO_REALTIME_POLL_TIMEOUT", "9999")
    monkeypatch.setenv("VIDEO_REALTIME_POLL_INTERVAL", "0")
    monkeypatch.setenv("VIDEO_REALTIME_DOWNLOAD_TIMEOUT", "NaN")

    settings = load_settings()

    assert settings.video_realtime_url == "https://wan.example"
    assert settings.video_realtime_token == "wan-token"
    assert settings.video_realtime_request_timeout == 1.0
    assert settings.video_realtime_poll_timeout == 600.0
    assert settings.video_realtime_poll_interval == 0.1
    assert settings.video_realtime_download_timeout == 60.0


def test_daily_voice_pregen_env_is_ignored_fail_closed(monkeypatch):
    monkeypatch.setenv("VOICE_DAILY_PREGEN", "true")
    assert load_settings().voice_daily_pre_gen is False
