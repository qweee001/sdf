"""場景狀態機純邏輯測試（不碰 DB／Telegram）。"""

from __future__ import annotations

import random

import pytest

from app import scene
from app.scene import (
    AFTERCARE,
    BUILD,
    CLOSED,
    DORMANT,
    PEAK,
    WARM,
    SceneSignals,
    next_stage,
    should_open,
)


def sig(**kwargs) -> SceneSignals:
    return SceneSignals(**kwargs)


def test_warm_needs_hysteresis_before_build():
    # 第一則露骨訊息：stage_age 還沒到滯回時間 → 留在 warm
    assert next_stage(WARM, sig(explicit_msgs_in_window=1, stage_age_seconds=10)).stage == WARM
    # 滯回時間到了但只有一則 → 還是 warm
    assert next_stage(WARM, sig(explicit_msgs_in_window=1, stage_age_seconds=120)).stage == WARM
    # 兩則 + 滯回過了 → build
    out = next_stage(WARM, sig(explicit_msgs_in_window=2, stage_age_seconds=120))
    assert (out.stage, out.reason) == (BUILD, "warm_escalated")


def test_build_to_peak_needs_density_and_not_hot():
    assert next_stage(BUILD, sig(density=0.3)).stage == BUILD
    assert next_stage(BUILD, sig(density=0.6)).stage == PEAK
    # 已經過熱就不推 peak（否則一路加溫）——但 heat 在 build 段先看冷卻出口
    assert next_stage(BUILD, sig(density=0.9, heat_fired=True)).stage == BUILD


def test_peak_exits_are_bounded():
    assert next_stage(PEAK, sig(heat_fired=True)).stage == AFTERCARE
    assert next_stage(PEAK, sig(peak_hits=scene.PEAK_MAX_HITS)).stage == AFTERCARE
    assert next_stage(PEAK, sig(idle_seconds=scene.PEAK_IDLE_SECONDS)).stage == AFTERCARE
    # 都不到 → 留在 peak
    assert next_stage(PEAK, sig(peak_hits=1, idle_seconds=30)).stage == PEAK


def test_aftercare_closes_after_one_line_and_has_timeout():
    assert next_stage(AFTERCARE, sig(aftercare_sent=0)).stage == AFTERCARE
    assert next_stage(AFTERCARE, sig(aftercare_sent=1)).stage == CLOSED
    out = next_stage(
        AFTERCARE,
        sig(aftercare_sent=0, age_seconds=scene.SCENE_MAX_SECONDS + 301),
    )
    assert out.stage == CLOSED


def test_max_age_beats_everything():
    out = next_stage(BUILD, sig(age_seconds=scene.SCENE_MAX_SECONDS, density=0.9))
    assert (out.stage, out.reason) == (AFTERCARE, "max_age")


def test_idle_dormants_and_revives_to_warm():
    assert next_stage(BUILD, sig(idle_seconds=scene.DORMANT_IDLE_SECONDS)).stage == DORMANT
    assert next_stage(DORMANT, sig(idle_seconds=10, explicit_msgs_in_window=1)).stage == WARM
    # 沒人撩就一直是 dormant
    assert next_stage(DORMANT, sig(idle_seconds=300)).stage == DORMANT


def test_closed_is_terminal():
    assert next_stage(CLOSED, sig(density=1.0, heat_fired=True)).stage == CLOSED


@pytest.mark.parametrize("stage", [WARM, BUILD, PEAK, AFTERCARE])
def test_directive_always_mentions_goal(stage):
    text = scene.directive(stage, "浴室")
    assert "浴室" in text
    assert text.startswith("【場景・")


def test_directive_for_closed_and_dormant_is_empty():
    assert scene.directive(CLOSED, "浴室") == ""
    assert scene.directive(DORMANT, "浴室") == ""


def test_keeps_goal_only_allows_warm_switch():
    assert scene.keeps_goal(WARM) is True
    assert scene.keeps_goal(BUILD) is False
    assert scene.keeps_goal(PEAK) is False


def test_should_open_prefers_booking_then_mention_then_axes():
    assert should_open(explicit_axes=1, mentioned=False, booking_asked=False, cooldown_left=0) == "explicit"
    assert should_open(explicit_axes=0, mentioned=True, booking_asked=False, cooldown_left=0) == "mentioned"
    assert should_open(explicit_axes=0, mentioned=True, booking_asked=True, cooldown_left=0) == "booking"
    # 冷卻中一律不開
    assert should_open(explicit_axes=2, mentioned=True, booking_asked=True, cooldown_left=5) == ""


def test_sample_goal_is_from_goals_and_deterministic_with_seed():
    rng = random.Random(7)
    goal = scene.sample_goal(rng)
    assert goal in scene.GOALS
    assert goal == scene.sample_goal(random.Random(7))
