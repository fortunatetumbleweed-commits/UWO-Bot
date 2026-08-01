"""§13.23 Phase A — Shore segment commitment state machine tests.

Replays sector data from hug_debug_20260601_164805 t280 (healthy
channel), t338 (last healthy before junction), and t341 (junction
ambiguity) to assert the state machine's decisions.
"""
from __future__ import annotations

from brain.goals.shore_segment import (
    ASSOC_THRESH_DEG,
    EWMA_ALPHA,
    HEALTHY_FRAC_MIN,
    RECOMMIT_AFTER_MISSES,
    ShoreSegmentMemory,
    evaluate_segment,
    memory_to_dict,
)


# Sample frac values from the live trace at the three port-side LSQ
# sectors (sec 14, 12, 10) — used for the "clean fit" criterion.
T280_FRACS = [0.30, 0.93, 0.95]
T281_FRACS = [0.26, 0.89, 0.95]
T338_FRACS = [0.95, 0.43, 0.90]
T341_FRACS = [0.39, 0.85, 0.92]


def test_bootstrap_clean_fit_commits():
    """First acted tick with 2+ high-frac samples → INIT commit."""
    decision = evaluate_segment(
        memory=None,
        fresh_theta_err_deg=-6.9,
        ship_heading_deg=215.0,
        samples_frac=T280_FRACS,
        current_lat=9.62, current_lon=33.18,
        endpoint_lat=8.0, endpoint_lon=32.5,
        side="port", current_tick=280,
    )
    assert decision.action == "act"
    assert decision.memory is not None
    assert decision.memory.committed_tick == 280
    # The override at init equals the fresh θ_err (no smoothing yet).
    assert abs(decision.theta_err_override - (-6.9)) < 1e-6


def test_bootstrap_unclean_fit_acts_without_commit():
    """First acted tick with < 2 high-frac samples → ACT but no memory."""
    decision = evaluate_segment(
        memory=None,
        fresh_theta_err_deg=10.0,
        ship_heading_deg=0.0,
        samples_frac=[0.10, 0.40, 0.20],  # nothing above HEALTHY_FRAC_MIN
        current_lat=10.0, current_lon=33.0,
        endpoint_lat=8.0, endpoint_lon=32.5,
        side="port", current_tick=0,
    )
    assert decision.action == "act"
    assert decision.memory is None
    assert decision.theta_err_override == 10.0


def test_match_within_threshold_ewma_updates():
    """Fresh tangent within 30° of memory → EWMA blend."""
    # Memory at 210°, fresh comes in at 215° (Δ=5°).
    mem = ShoreSegmentMemory(
        tangent_world_deg=210.0,
        anchor_lat=10.0, anchor_lon=33.0,
        committed_tick=10, last_update_tick=10,
        side_at_commit="port", consecutive_misses=0,
    )
    decision = evaluate_segment(
        memory=mem,
        fresh_theta_err_deg=0.0,         # so fresh_tw = heading
        ship_heading_deg=215.0,           # fresh_tw = 215°
        samples_frac=T280_FRACS,
        current_lat=10.0, current_lon=33.0,
        endpoint_lat=8.0, endpoint_lon=32.5,
        side="port", current_tick=11,
    )
    assert decision.action == "act"
    assert decision.memory is not None
    # EWMA: new = 210 + 0.3 · (215 − 210) = 211.5
    assert abs(decision.memory.tangent_world_deg - 211.5) < 0.01
    assert decision.memory.consecutive_misses == 0
    # Override should equal new committed minus heading.
    expected_override = ((211.5 - 215.0 + 180.0) % 360.0) - 180.0
    assert abs(decision.theta_err_override - expected_override) < 0.01


def test_disagreement_first_miss_holds():
    """Fresh disagrees by > 30° but only 1st miss → HOLD."""
    # Memory at 200° (channel direction).  Fresh would be 280° (large
    # deviation).  consecutive_misses=0 going in.
    mem = ShoreSegmentMemory(
        tangent_world_deg=200.0,
        anchor_lat=10.0, anchor_lon=33.0,
        committed_tick=10, last_update_tick=10,
        side_at_commit="port", consecutive_misses=0,
    )
    decision = evaluate_segment(
        memory=mem,
        fresh_theta_err_deg=80.0,
        ship_heading_deg=200.0,           # fresh_tw = 280°
        samples_frac=T341_FRACS,           # clean by frac check
        current_lat=10.0, current_lon=33.0,
        # No dest provided so destination-anchor flip skipped.
        endpoint_lat=None, endpoint_lon=None,
        side="port", current_tick=11,
    )
    assert decision.action == "hold"
    assert decision.memory is not None
    assert decision.memory.consecutive_misses == 1
    assert decision.memory.tangent_world_deg == 200.0   # unchanged


def test_disagreement_after_n_misses_recommits_when_clean():
    """Fresh disagrees again AND samples are clean AND consecutive_misses
    will reach RECOMMIT_AFTER_MISSES → RECOMMIT to fresh tangent."""
    assert RECOMMIT_AFTER_MISSES >= 2   # design lock
    mem = ShoreSegmentMemory(
        tangent_world_deg=200.0,
        anchor_lat=10.0, anchor_lon=33.0,
        committed_tick=10, last_update_tick=10,
        side_at_commit="port",
        consecutive_misses=RECOMMIT_AFTER_MISSES - 1,
    )
    decision = evaluate_segment(
        memory=mem,
        fresh_theta_err_deg=80.0,
        ship_heading_deg=200.0,
        samples_frac=T341_FRACS,
        current_lat=10.0, current_lon=33.0,
        endpoint_lat=None, endpoint_lon=None,
        side="port", current_tick=11,
    )
    assert decision.action == "act"
    assert decision.memory is not None
    assert decision.memory.tangent_world_deg == 280.0
    assert decision.memory.consecutive_misses == 0
    assert decision.memory.committed_tick == 11


def test_disagreement_unclean_holds_forever():
    """Fresh disagrees AND samples are unclean → HOLD regardless of
    consecutive misses (we never re-acquire on a bad fit)."""
    mem = ShoreSegmentMemory(
        tangent_world_deg=200.0,
        anchor_lat=10.0, anchor_lon=33.0,
        committed_tick=10, last_update_tick=10,
        side_at_commit="port",
        consecutive_misses=10,   # well past RECOMMIT_AFTER_MISSES
    )
    decision = evaluate_segment(
        memory=mem,
        fresh_theta_err_deg=80.0,
        ship_heading_deg=200.0,
        samples_frac=[0.10, 0.10, 0.10],   # all below frac threshold
        current_lat=10.0, current_lon=33.0,
        endpoint_lat=None, endpoint_lon=None,
        side="port", current_tick=11,
    )
    assert decision.action == "hold"
    assert decision.memory.consecutive_misses == 11


def test_no_data_when_pose_missing():
    """No heading or no position → "no_data" decision, memory preserved."""
    mem = ShoreSegmentMemory(
        tangent_world_deg=200.0,
        anchor_lat=10.0, anchor_lon=33.0,
        committed_tick=10, last_update_tick=10,
        side_at_commit="port", consecutive_misses=0,
    )
    decision = evaluate_segment(
        memory=mem,
        fresh_theta_err_deg=0.0,
        ship_heading_deg=None,           # missing
        samples_frac=T280_FRACS,
        current_lat=10.0, current_lon=33.0,
        endpoint_lat=None, endpoint_lon=None,
        side="port", current_tick=11,
    )
    assert decision.action == "no_data"
    assert decision.memory is mem   # unchanged identity


def test_replay_t338_to_t341_holds_on_fresh_disagreement():
    """§13.23 reference incident.  Memory was committed during the clean
    channel run (around t280-t320) and reflects a world-frame tangent in
    the 200-210° band (down-river).  At t341 the bot has just completed
    a U-turn and the fresh LSQ at the Y-junction returns a wildly
    different tangent.  The state machine must HOLD instead of letting
    the bad tangent drive the waypoint into shore.
    """
    # Approximate the post-EWMA committed state at the point bot enters
    # the Y junction (last clean tracking before the U-turn).
    mem = ShoreSegmentMemory(
        tangent_world_deg=210.0,    # representative WSW channel-direction
        anchor_lat=9.0, anchor_lon=33.0,
        committed_tick=281, last_update_tick=320,
        side_at_commit="port", consecutive_misses=0,
    )
    # At t341 the fresh fit gives θ_err=−20.6° with heading 328°, so
    # post-§13.21-flip the fresh world tangent is 127° (ESE).  Memory
    # at 210° disagrees by ≈ 83° (large).  Even though sec 12 and sec 10
    # have high frac (CLEAN by Phase A criterion), this is only the 1st
    # consecutive miss, so the state machine must HOLD.
    decision = evaluate_segment(
        memory=mem,
        fresh_theta_err_deg=-20.6,
        ship_heading_deg=327.8,
        samples_frac=T341_FRACS,
        # bot pos at t341 from trace:
        current_lat=8.68, current_lon=33.05,
        # Synthesise dest so that the dest-anchor flip activates (mirrors
        # the live behaviour); LOS bearing from (8.68, 33.05) to
        # (8.0, 32.5) is roughly SSW (~219°).
        endpoint_lat=8.0, endpoint_lon=32.5,
        side="port", current_tick=341,
    )
    assert decision.action == "hold"
    assert decision.memory is not None
    assert decision.memory.consecutive_misses == 1
    # Memory tangent unchanged — bot didn't re-commit to the bad fit.
    assert decision.memory.tangent_world_deg == 210.0


def test_default_waypoint_generator_is_pre_phase_a():
    """§13.23 — gate Phase A behind an opt-in flag, default pre-Phase A.
    Constructed without `waypoint_generator` and without the env var,
    HugShoreGoal resolves to "pre_phase_a"."""
    import os
    import sys
    sys.modules["actions.sea_actions"] = type("F", (), {
        "hold_left":  staticmethod(lambda ms: None),
        "hold_right": staticmethod(lambda ms: None),
        "turn_left":  staticmethod(lambda taps=1: None),
        "turn_right": staticmethod(lambda taps=1: None),
        "is_ship_moving": staticmethod(lambda: True),
        "sail_start": staticmethod(lambda: None),
    })
    from brain.goals.hug_shore import HugShoreGoal
    prev = os.environ.pop("UWO_WAYPOINT_GENERATOR", None)
    try:
        goal = HugShoreGoal(side="port", driver_mode="point_pursuit")
        assert goal.waypoint_generator == "pre_phase_a"
    finally:
        if prev is not None:
            os.environ["UWO_WAYPOINT_GENERATOR"] = prev


def test_env_var_selects_phase_a_when_no_ctor_arg():
    import os
    import sys
    sys.modules["actions.sea_actions"] = type("F", (), {
        "hold_left":  staticmethod(lambda ms: None),
        "hold_right": staticmethod(lambda ms: None),
        "turn_left":  staticmethod(lambda taps=1: None),
        "turn_right": staticmethod(lambda taps=1: None),
        "is_ship_moving": staticmethod(lambda: True),
        "sail_start": staticmethod(lambda: None),
    })
    from brain.goals.hug_shore import HugShoreGoal
    prev = os.environ.get("UWO_WAYPOINT_GENERATOR")
    os.environ["UWO_WAYPOINT_GENERATOR"] = "phase_a"
    try:
        goal = HugShoreGoal(side="port", driver_mode="point_pursuit")
        assert goal.waypoint_generator == "phase_a"
    finally:
        if prev is None:
            os.environ.pop("UWO_WAYPOINT_GENERATOR", None)
        else:
            os.environ["UWO_WAYPOINT_GENERATOR"] = prev


def test_ctor_arg_overrides_env_var():
    import os
    import sys
    sys.modules["actions.sea_actions"] = type("F", (), {
        "hold_left":  staticmethod(lambda ms: None),
        "hold_right": staticmethod(lambda ms: None),
        "turn_left":  staticmethod(lambda taps=1: None),
        "turn_right": staticmethod(lambda taps=1: None),
        "is_ship_moving": staticmethod(lambda: True),
        "sail_start": staticmethod(lambda: None),
    })
    from brain.goals.hug_shore import HugShoreGoal
    prev = os.environ.get("UWO_WAYPOINT_GENERATOR")
    os.environ["UWO_WAYPOINT_GENERATOR"] = "phase_a"
    try:
        goal = HugShoreGoal(side="port", driver_mode="point_pursuit",
                              waypoint_generator="pre_phase_a")
        assert goal.waypoint_generator == "pre_phase_a"
    finally:
        if prev is None:
            os.environ.pop("UWO_WAYPOINT_GENERATOR", None)
        else:
            os.environ["UWO_WAYPOINT_GENERATOR"] = prev


def test_invalid_waypoint_generator_raises():
    import sys
    sys.modules["actions.sea_actions"] = type("F", (), {
        "hold_left":  staticmethod(lambda ms: None),
        "hold_right": staticmethod(lambda ms: None),
        "turn_left":  staticmethod(lambda taps=1: None),
        "turn_right": staticmethod(lambda taps=1: None),
        "is_ship_moving": staticmethod(lambda: True),
        "sail_start": staticmethod(lambda: None),
    })
    from brain.goals.hug_shore import HugShoreGoal
    import pytest
    with pytest.raises(ValueError, match="waypoint_generator"):
        HugShoreGoal(side="port", driver_mode="point_pursuit",
                      waypoint_generator="bogus")


def test_memory_to_dict_serialises_for_trace():
    mem = ShoreSegmentMemory(
        tangent_world_deg=210.5,
        anchor_lat=9.0, anchor_lon=33.0,
        committed_tick=10, last_update_tick=20,
        side_at_commit="port", consecutive_misses=2,
    )
    d = memory_to_dict(mem)
    assert d == {
        "tangent_world_deg":   210.5,
        "anchor_lat":          9.0,
        "anchor_lon":          33.0,
        "committed_tick":      10,
        "last_update_tick":    20,
        "side_at_commit":      "port",
        "consecutive_misses":  2,
    }
    assert memory_to_dict(None) is None
