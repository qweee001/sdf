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
    assert s.water_cross_talk_probability > 0
    assert s.ai_disable_thinking is False
    assert s.vision_model == "test-model"
    assert s.image_model
    assert s.speech_model
    assert s.video_model
    assert s.media_enabled is False
    assert s.media_daily_budget_usd == 10.0
    assert s.media_max_input_bytes == 8 * 1024 * 1024


def test_ai_disable_thinking_from_env(monkeypatch):
    monkeypatch.setenv("AI_DISABLE_THINKING", "true")
    assert load_settings().ai_disable_thinking is True


def test_non_finite_media_budget_falls_back_to_finite_default(monkeypatch):
    monkeypatch.setenv("MEDIA_DAILY_BUDGET_USD", "NaN")
    budget = load_settings().media_daily_budget_usd
    assert math.isfinite(budget)
    assert budget == 10.0
