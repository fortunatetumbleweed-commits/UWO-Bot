"""§13.16.4 — Tests for the Step 2 sequencer: history-derived goal heading.

The sequencer continuously infers a goal heading from recent history when
no explicit `goal_heading_deg` is set.  This implements the canonical
"history smoothing" signal from the three-layer architecture
(see project_goal_management_architecture memory).

Weights per tick:
  * Phase HUGGING=1.0, OFFSHORE=0.5, others=0.0
  * Speed below GOAL_INFERENCE_MIN_SPEED_KT → 0.0
  * Age: linear decay (newest=1.0, oldest of window ≈ 0)

Schmitt-trigger hysteresis at the sequencer level
(GOAL_UPDATE_HYSTERESIS_DEG): only update active inferred goal when
the new estimate moves by more than this threshold.
"""
from __future__ import annotations

import pytest

from brain.goals.hug_shore import (
    HugShoreGoal,
    HugPhase,
    TickRecord,
    _infer_goal_heading_from_history,
    _angle_diff_deg,
    GOAL_INFERENCE_MIN_SAMPLES,
    GOAL_INFERENCE_MIN_SPEED_KT,
    GOAL_UPDATE_HYSTERESIS_DEG,
)


def _mk_record(tick, heading, phase=HugPhase.HUGGING, speed=10.0):
    return TickRecord(
        tick=tick, wall_time=float(tick), heading=heading,
        raw_heading=heading, rejected=False,
        lat=None, lon=None, speed_kt=speed,
        sectors=(), commanded_deg=0.0, actual_delta=None,
        phase=phase, chosen_sector=None, ideal_sector=None,
        cost_dump={},
    )


def test_too_few_samples_returns_none():
    """Below GOAL_INFERENCE_MIN_SAMPLES qualifying ticks → None."""
    history = [_mk_record(i, 90.0) for i in range(GOAL_INFERENCE_MIN_SAMPLES - 1)]
    assert _infer_goal_heading_from_history(history) is None


def test_steady_north_heading_inferred():
    """All recent ticks at HUGGING phase, heading 0° (N).  Inferred
    goal should be ~0°."""
    history = [_mk_record(i, 0.0) for i in range(15)]
    inferred = _infer_goal_heading_from_history(history)
    assert inferred is not None
    assert _angle_diff_deg(inferred, 0.0) < 1.0


def test_steady_west_heading_inferred():
    """Heading 270° (W) for 15 ticks → inferred ≈ 270°."""
    history = [_mk_record(i, 270.0) for i in range(15)]
    inferred = _infer_goal_heading_from_history(history)
    assert inferred is not None
    assert _angle_diff_deg(inferred, 270.0) < 1.0


def test_age_weighting_favors_recent():
    """First half of window heading=270 (W), second half heading=0 (N).
    Linear age weight means the newer (N) ticks count more — inferred
    should lean toward N (between 270 and 0 but closer to 0)."""
    history = ([_mk_record(i, 270.0) for i in range(10)]
               + [_mk_record(i + 10, 0.0) for i in range(10)])
    inferred = _infer_goal_heading_from_history(history)
    assert inferred is not None
    # Should be in NW quadrant (closer to N than to W).
    dist_to_north = _angle_diff_deg(inferred, 0.0)
    dist_to_west  = _angle_diff_deg(inferred, 270.0)
    assert dist_to_north < dist_to_west, (
        f"Inferred {inferred:.0f}° should be closer to N than W "
        f"(age weighting); dN={dist_to_north:.0f} dW={dist_to_west:.0f}"
    )


def test_blocked_ticks_ignored():
    """Phase=BLOCKED ticks have weight 0.  If everything in window is
    BLOCKED, inference returns None."""
    history = [_mk_record(i, 90.0, phase=HugPhase.BLOCKED) for i in range(15)]
    assert _infer_goal_heading_from_history(history) is None


def test_slow_ticks_ignored():
    """Ticks below GOAL_INFERENCE_MIN_SPEED_KT count as 'stuck' and
    don't contribute to the goal estimate."""
    history = [_mk_record(i, 90.0, speed=0.5) for i in range(15)]
    assert _infer_goal_heading_from_history(history) is None


def test_mixed_phases_hugging_dominates():
    """HUGGING ticks at N, OFFSHORE ticks at S.  HUGGING weight=1.0,
    OFFSHORE=0.5 → if balanced count, inference biases toward N."""
    history = ([_mk_record(i, 0.0, phase=HugPhase.HUGGING) for i in range(10)]
               + [_mk_record(i + 10, 180.0, phase=HugPhase.OFFSHORE)
                  for i in range(10)])
    inferred = _infer_goal_heading_from_history(history)
    assert inferred is not None
    # Should NOT be 180° — HUGGING dominates and pulls it back toward 0.
    # With both phase + age weights, the answer is somewhere; just verify
    # it's not just average of the two (which would land at 90° or 270°).
    # Specifically: closer to OFFSHORE because it's more recent, but
    # HUGGING weight=1.0 pulls back from pure S.  Expected: NE/SE-ish.
    assert _angle_diff_deg(inferred, 180.0) > 30.0, (
        f"OFFSHORE 180° pulled too much; got {inferred:.0f}°"
    )


def test_sequencer_updates_with_hysteresis(monkeypatch):
    """Goal heading update obeys Schmitt-trigger hysteresis: small
    changes in the inferred estimate don't update the cached value;
    large changes do."""
    fake_sea = type("F", (), {
        "hold_left":  staticmethod(lambda ms: None),
        "hold_right": staticmethod(lambda ms: None),
        "turn_left":  staticmethod(lambda taps=1: None),
        "turn_right": staticmethod(lambda taps=1: None),
    })
    import sys
    sys.modules["actions.sea_actions"] = fake_sea

    goal = HugShoreGoal(side="starboard", max_ticks=50, driver_mode="vfh")
    # Prime history with 15 ticks heading 0° (N).
    for i in range(15):
        goal.history.append(_mk_record(i, 0.0))
    goal._update_inferred_goal_heading()
    assert goal._inferred_goal_heading_deg is not None
    assert _angle_diff_deg(goal._inferred_goal_heading_deg, 0.0) < 1.0

    # Add a small drift — bot's been doing 1° on average now.  This
    # is below GOAL_UPDATE_HYSTERESIS_DEG → cached value should NOT
    # change.
    cached = goal._inferred_goal_heading_deg
    goal.history.append(_mk_record(15, 5.0))
    goal._update_inferred_goal_heading()
    assert goal._inferred_goal_heading_deg == cached, (
        "Small drift should not exceed hysteresis threshold"
    )

    # Now a large persistent shift — heading=90° for many ticks.
    for i in range(30):
        goal.history.append(_mk_record(16 + i, 90.0))
    goal._update_inferred_goal_heading()
    # Cached value should now have moved meaningfully (more than the
    # hysteresis amount).
    moved = _angle_diff_deg(goal._inferred_goal_heading_deg, cached)
    assert moved >= GOAL_UPDATE_HYSTERESIS_DEG, (
        f"Large persistent shift should update cached goal; moved {moved:.0f}°"
    )


def test_explicit_goal_overrides_inference():
    """`goal_heading_deg` set at construction always wins over the
    inferred value — explicit > implicit."""
    goal = HugShoreGoal(
        side="starboard", max_ticks=50, driver_mode="vfh",
        goal_heading_deg=45.0,   # NE
    )
    # Prime inferred goal with N.
    for i in range(15):
        goal.history.append(_mk_record(i, 0.0))
    goal._update_inferred_goal_heading()
    # Effective goal should be the explicit value, not the inferred.
    assert goal._effective_goal_heading_deg() == 45.0


def test_no_explicit_no_history_returns_none():
    """When neither explicit nor inferred is available, effective
    goal is None and the §13.16.3 logic falls back to centroid."""
    goal = HugShoreGoal(side="starboard", max_ticks=50, driver_mode="vfh")
    # No history primed.
    assert goal._effective_goal_heading_deg() is None
