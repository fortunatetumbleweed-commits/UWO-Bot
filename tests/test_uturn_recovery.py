"""§13.27 — Tests for the stuck detector + U-turn recovery module."""
from __future__ import annotations

from brain.goals.uturn_recovery import (
    DRIFT_THRESHOLD_DEG,
    EXIT_TOLERANCE_DEG,
    FORWARD_ARC_SECTORS,
    FWD_CLEAR_THRESHOLD,
    REARM_DISTANCE_DEG,
    StuckDetector,
    UTurnState,
    forward_clearance_mean,
    recovery_command,
    should_exit,
    signed_bow_to_target,
    WINDOW_TICKS,
)


# ── Stub nav sector for testing forward_clearance_mean ──────────────────────


class _Sec:
    __slots__ = ("nearest_dist",)
    def __init__(self, d):
        self.nearest_dist = d


class _Nav:
    __slots__ = ("sectors",)
    def __init__(self, sector_dists):
        # sector_dists is a 16-element list of nearest_dist values
        self.sectors = tuple(_Sec(d) for d in sector_dists)


def _open_water_nav():
    return _Nav([0.5] * 16)


def _blocked_forward_nav():
    """Mean forward clearance well below FWD_CLEAR_THRESHOLD."""
    dists = [0.5] * 16
    for idx in FORWARD_ARC_SECTORS:
        dists[idx] = 0.10
    return _Nav(dists)


# ── forward_clearance_mean ──────────────────────────────────────────────────


def test_forward_clearance_mean_basic():
    nav = _open_water_nav()
    assert forward_clearance_mean(nav) == 0.5


def test_forward_clearance_mean_ignores_none():
    dists = [0.5] * 16
    dists[0] = None  # bow sector unobserved
    nav = _Nav(dists)
    # Mean of the four remaining forward sectors (14, 15, 1, 2)
    assert forward_clearance_mean(nav) == 0.5


def test_forward_clearance_mean_all_none_returns_none():
    nav = _Nav([None] * 16)
    assert forward_clearance_mean(nav) is None


# ── signed_bow_to_target ────────────────────────────────────────────────────


def test_signed_bow_to_target_simple_cases():
    # Target dead astern of bow at 0° = +180° (or -180°, ambiguous — we
    # return +180 by the (% + 180) convention; either is correct).
    assert abs(abs(signed_bow_to_target(0.0, 180.0)) - 180.0) < 1e-6
    # Target 45° starboard of north-facing bow
    assert signed_bow_to_target(0.0, 45.0) == 45.0
    # Target 45° port (counterclockwise) of north
    assert signed_bow_to_target(0.0, 315.0) == -45.0
    # Wrap: bow at 350°, target at 10° → +20° (closer via stbd)
    assert signed_bow_to_target(350.0, 10.0) == 20.0


# ── recovery_command ────────────────────────────────────────────────────────


def test_recovery_command_picks_shorter_direction():
    # Target 45° to the right of bow → right turn
    direction, mag = recovery_command(0.0, 45.0)
    assert direction == "right"
    assert mag == 45.0
    # Target 45° to the left → left turn
    direction, mag = recovery_command(0.0, 315.0)
    assert direction == "left"
    assert mag == 45.0


def test_recovery_command_caps_magnitude():
    # Target ~180° behind → magnitude capped at RECOVERY_HOLD_MAX_DEG
    direction, mag = recovery_command(0.0, 179.0)
    assert mag <= 90.0


# ── should_exit ─────────────────────────────────────────────────────────────


def test_should_exit_within_tolerance():
    # Bow within EXIT_TOLERANCE_DEG of target → exit
    assert should_exit(0.0, EXIT_TOLERANCE_DEG - 1.0)
    assert should_exit(0.0, -(EXIT_TOLERANCE_DEG - 1.0) % 360)
    assert not should_exit(0.0, EXIT_TOLERANCE_DEG + 5.0)


# ── StuckDetector — window + signals ────────────────────────────────────────


def test_detector_does_not_fire_before_window_full():
    det = StuckDetector()
    # Record fewer than WINDOW_TICKS samples — should never fire.
    for t in range(WINDOW_TICKS - 1):
        det.record(t, 18.0, 31.0, 0.10)
    assert not det.check()


def test_detector_fires_when_both_signals_hold():
    """Position barely moves AND forward clearance is low for the whole
    window → fire."""
    det = StuckDetector()
    for t in range(WINDOW_TICKS):
        # tiny jitter, well below DRIFT_THRESHOLD_DEG
        det.record(t, 18.0 + 0.001 * (t % 2), 31.0, 0.10)
    assert det.check()


def test_detector_does_not_fire_when_moving_freely():
    det = StuckDetector()
    # Bot is moving steadily (cruise speed) — drift accumulates fast
    for t in range(WINDOW_TICKS):
        det.record(t, 18.0 + 0.05 * t, 31.0, 0.10)  # 0.05° per tick
    assert not det.check()


def test_detector_does_not_fire_when_forward_clear():
    det = StuckDetector()
    # Bot is barely moving but forward arc is open — could be drifting
    # at a port; not stuck in the sense we care about.
    for t in range(WINDOW_TICKS):
        det.record(t, 18.0, 31.0, 0.50)  # open water
    assert not det.check()


def test_detector_ignores_partial_samples():
    """A sample with any None field is dropped; detector only counts
    complete observations."""
    det = StuckDetector()
    for t in range(WINDOW_TICKS):
        # Every other tick has missing data — detector buffer fills
        # slower but eventually fires once enough complete samples land.
        if t % 2 == 0:
            det.record(t, 18.0, 31.0, 0.10)
        else:
            det.record(t, None, 31.0, 0.10)  # missing lat
    # Only ~WINDOW_TICKS/2 complete samples — not enough.
    assert not det.check()


# ── StuckDetector — disarm / re-arm hysteresis ──────────────────────────────


def test_disarm_blocks_immediate_refire():
    det = StuckDetector()
    for t in range(WINDOW_TICKS):
        det.record(t, 18.0, 31.0, 0.10)
    assert det.check()
    det.disarm_at(18.0, 31.0)
    # Same position keeps coming in — should NOT re-fire.
    for t in range(WINDOW_TICKS, 2 * WINDOW_TICKS):
        det.record(t, 18.0, 31.0, 0.10)
    assert not det.check()


def test_disarm_releases_after_movement():
    det = StuckDetector()
    for t in range(WINDOW_TICKS):
        det.record(t, 18.0, 31.0, 0.10)
    det.disarm_at(18.0, 31.0)
    # Bot moves far enough away (> REARM_DISTANCE_DEG), then re-stalls.
    far_lat = 18.0 + REARM_DISTANCE_DEG + 0.05
    # Need to fill a fresh window of stalled samples after movement.
    for t in range(WINDOW_TICKS):
        det.record(WINDOW_TICKS + t, far_lat, 31.0, 0.10)
    assert det.check()  # re-armed and stuck again


# ── UTurnState dataclass smoke ──────────────────────────────────────────────


def test_uturn_state_fields():
    s = UTurnState(
        entry_tick=42,
        latched_target_deg=180.0,
        entry_lat=18.0,
        entry_lon=31.0,
    )
    assert s.entry_tick == 42
    assert s.latched_target_deg == 180.0


# ── Tunable invariants — drift the test catches a deliberate retune ─────────


def test_tunables_in_sane_range():
    assert 10 <= WINDOW_TICKS <= 60
    assert 0.05 <= DRIFT_THRESHOLD_DEG <= 0.5
    assert 0.10 <= FWD_CLEAR_THRESHOLD <= 0.40
    assert 10.0 <= EXIT_TOLERANCE_DEG <= 45.0
    assert REARM_DISTANCE_DEG >= DRIFT_THRESHOLD_DEG  # must outrun drift
