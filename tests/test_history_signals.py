"""Phase A — tests for TickRecord history + _derive_signals.

These tests cover the derived signals only.  They build a fake history
deque and verify each signal independently, without invoking the rest
of the policy.  Policy integrations using these signals are tested in
the regression suite once Phase A.2 ships.
"""
from collections import deque

from brain.goals.hug_shore import (
    HugPhase, TickRecord, HistorySignals, _derive_signals,
    HISTORY_WINDOW, SHORE_VISIBLE_FRAC,
)


from vision.navigation_view import SECTOR_COUNT

# Beam indices in the 16-sector model that `_derive_signals` reads for drift rate
# (`t_idx = 4 if side == "starboard" else 12`). These tests used to build 8-element
# readings with the beam at index 2, which is a 22.5°-per-sector mismatch: index 2 is
# bow-starboard in the real model, so the drift tests were measuring an all-clear
# sector and asserting on a rate that was structurally 0.0 — and the port case indexed
# past the end of the tuple.
_STBD_BEAM = 4      # 90°
_PORT_BEAM = 12     # 270°


def _rec(tick, sectors=None, heading=None, lat=None, lon=None,
         speed_kt=None, commanded_deg=0.0, actual_delta=None,
         phase=HugPhase.HUGGING):
    """Concise TickRecord factory.  `sectors` defaults to all-clear."""
    if sectors is None:
        sectors = tuple((0.0, 1.0) for _ in range(SECTOR_COUNT))
    return TickRecord(
        tick=tick, wall_time=float(tick),
        heading=heading, raw_heading=heading, rejected=False,
        lat=lat, lon=lon, speed_kt=speed_kt,
        sectors=sectors,
        commanded_deg=commanded_deg,
        actual_delta=actual_delta,
        phase=phase, chosen_sector=None, ideal_sector=None, cost_dump={},
    )


def test_empty_history_returns_defaults():
    sig = _derive_signals(deque(), "starboard")
    assert isinstance(sig, HistorySignals)
    assert sig.target_shore_last_seen_at_tick is None
    assert sig.recent_rotation_direction == 0
    assert sig.consecutive_same_direction_turns == 0
    assert sig.world_motion_bearing is None
    assert sig.median_speed_kt is None


def test_target_shore_last_seen_starboard():
    """For starboard hug, target sectors are 1 (bow-stbd), 2 (stbd),
    3 (astern-stbd).  Loaded ones should be detected."""
    # Tick 5 had sector 2 loaded (stbd-beam); ticks 6+ are all clear.
    sectors_loaded = tuple(
        (0.8 if i == 2 else 0.0, 0.1 if i == 2 else 1.0)
        for i in range(SECTOR_COUNT)
    )
    history = deque([
        _rec(1), _rec(2), _rec(3), _rec(4),
        _rec(5, sectors=sectors_loaded),
        _rec(6), _rec(7),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.target_shore_last_seen_at_tick == 5
    assert sig.target_shore_last_seen_sector == 2


def test_target_shore_last_seen_picks_most_recent():
    """If multiple recent ticks have shore, the LAST one wins."""
    def loaded(i):
        return tuple((0.5 if j == i else 0.0, 0.1) for j in range(SECTOR_COUNT))
    history = deque([
        _rec(1, sectors=loaded(2)),
        _rec(2, sectors=loaded(1)),
        _rec(3),
        _rec(4, sectors=loaded(3)),
        _rec(5),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.target_shore_last_seen_at_tick == 4
    assert sig.target_shore_last_seen_sector == 3   # astern-stbd


def test_target_shore_for_port_side_uses_opposite_indices():
    """For port hug, target sectors are 5, 6, 7."""
    sectors = tuple(
        (0.7 if i == 6 else 0.0, 0.1) for i in range(SECTOR_COUNT)
    )
    history = deque([_rec(1, sectors=sectors)])
    sig = _derive_signals(history, "port")
    assert sig.target_shore_last_seen_at_tick == 1
    assert sig.target_shore_last_seen_sector == 6


def test_opposite_shore_last_seen():
    """For starboard hug, opposite sectors are 5, 6, 7."""
    sectors = tuple(
        (0.7 if i == 6 else 0.0, 0.1) for i in range(SECTOR_COUNT)
    )
    history = deque([_rec(1, sectors=sectors)])
    sig = _derive_signals(history, "starboard")
    assert sig.opposite_shore_last_seen_at_tick == 1


def test_rotation_direction_right():
    """Cumulative actual_delta over last 3 ticks > 15 → right."""
    history = deque([
        _rec(1, actual_delta=10.0),
        _rec(2, actual_delta=20.0),
        _rec(3, actual_delta=5.0),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.recent_rotation_direction == +1


def test_rotation_direction_left():
    history = deque([
        _rec(1, actual_delta=-20.0),
        _rec(2, actual_delta=-30.0),
        _rec(3, actual_delta=-10.0),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.recent_rotation_direction == -1


def test_rotation_direction_neutral_when_mixed():
    history = deque([
        _rec(1, actual_delta=+30.0),
        _rec(2, actual_delta=-25.0),
        _rec(3, actual_delta=-10.0),
    ])
    # cumulative = -5, |−5| < 15 → neutral
    sig = _derive_signals(history, "starboard")
    assert sig.recent_rotation_direction == 0


def test_rotation_falls_back_to_commanded_when_no_actual():
    """If actual_delta isn't recorded yet (e.g., latest tick), use
    commanded_deg as a proxy for rotation direction."""
    history = deque([
        _rec(1, commanded_deg=+45.0),
        _rec(2, commanded_deg=+45.0),
        _rec(3, commanded_deg=0.0),   # most recent — hold
    ])
    sig = _derive_signals(history, "starboard")
    # cumulative commanded = +90 → right
    assert sig.recent_rotation_direction == +1


def test_consecutive_same_direction_turns_counts_run():
    history = deque([
        _rec(1, commanded_deg=+45.0),  # turn right
        _rec(2, commanded_deg=+45.0),
        _rec(3, commanded_deg=+90.0),  # still right (same sign)
        _rec(4, commanded_deg=+45.0),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.consecutive_same_direction_turns == 4


def test_consecutive_same_direction_breaks_on_hold():
    history = deque([
        _rec(1, commanded_deg=+45.0),
        _rec(2, commanded_deg=0.0),    # hold breaks
        _rec(3, commanded_deg=+45.0),
        _rec(4, commanded_deg=+45.0),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.consecutive_same_direction_turns == 2


def test_consecutive_same_direction_breaks_on_reverse():
    history = deque([
        _rec(1, commanded_deg=+45.0),
        _rec(2, commanded_deg=-45.0),  # reverse
        _rec(3, commanded_deg=-45.0),
        _rec(4, commanded_deg=-45.0),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.consecutive_same_direction_turns == 3


def test_world_motion_bearing_east():
    """Δlon = +0.5°, Δlat = 0 → bearing should be ~90° (east)."""
    history = deque([
        _rec(1, lat=33.50, lon=14.00),
        _rec(2, lat=33.50, lon=14.10),
        _rec(3, lat=33.50, lon=14.20),
        _rec(4, lat=33.50, lon=14.50),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.world_motion_bearing is not None
    assert abs(sig.world_motion_bearing - 90.0) < 2.0


def test_world_motion_bearing_north():
    history = deque([
        _rec(1, lat=33.00, lon=14.00),
        _rec(2, lat=33.50, lon=14.00),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.world_motion_bearing is not None
    assert abs(sig.world_motion_bearing - 0.0) < 2.0 or \
           abs(sig.world_motion_bearing - 360.0) < 2.0


def test_world_motion_bearing_none_when_displacement_too_small():
    """Below 0.02° accumulated displacement, bearing is None
    (quantization floor)."""
    history = deque([
        _rec(1, lat=33.00, lon=14.00),
        _rec(2, lat=33.005, lon=14.005),  # tiny movement
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.world_motion_bearing is None


def test_median_speed_takes_5_most_recent():
    history = deque([
        _rec(1, speed_kt=50.0),   # outlier
        _rec(2, speed_kt=10.0),
        _rec(3, speed_kt=11.0),
        _rec(4, speed_kt=12.0),
        _rec(5, speed_kt=11.0),
        _rec(6, speed_kt=10.0),
    ])
    sig = _derive_signals(history, "starboard")
    # last 5 readings: 10, 11, 12, 11, 10 → median 11
    assert sig.median_speed_kt == 11.0


def test_target_drift_rate_receding_is_positive():
    """Target-beam dist grows by ~0.05/tick → drift_rate > 0."""
    def sectors_with_t_dist(d):
        return tuple(
            (0.30, d) if i == _STBD_BEAM else (0.0, 1.0)
            for i in range(SECTOR_COUNT)
        )
    history = deque([
        _rec(1, sectors=sectors_with_t_dist(0.20)),
        _rec(2, sectors=sectors_with_t_dist(0.25)),
        _rec(3, sectors=sectors_with_t_dist(0.30)),
        _rec(4, sectors=sectors_with_t_dist(0.35)),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.target_drift_rate is not None
    assert sig.target_drift_rate > 0
    assert abs(sig.target_drift_rate - 0.05) < 1e-6


def test_target_drift_rate_approaching_is_negative():
    """Target-beam dist shrinks → drift_rate < 0."""
    def sectors_with_t_dist(d):
        return tuple(
            (0.30, d) if i == _STBD_BEAM else (0.0, 1.0)
            for i in range(SECTOR_COUNT)
        )
    history = deque([
        _rec(1, sectors=sectors_with_t_dist(0.40)),
        _rec(2, sectors=sectors_with_t_dist(0.30)),
        _rec(3, sectors=sectors_with_t_dist(0.20)),
    ])
    sig = _derive_signals(history, "starboard")
    assert sig.target_drift_rate is not None
    assert sig.target_drift_rate < 0


def test_target_drift_rate_stable_is_near_zero():
    """Target dist holds steady → drift_rate ~ 0."""
    sectors = tuple(
        (0.30, 0.20) if i == _STBD_BEAM else (0.0, 1.0)
        for i in range(SECTOR_COUNT)
    )
    history = deque([_rec(t, sectors=sectors) for t in range(1, 5)])
    sig = _derive_signals(history, "starboard")
    assert sig.target_drift_rate is not None
    assert abs(sig.target_drift_rate) < 1e-6


def test_target_drift_rate_uses_port_sector_for_port_side():
    """For port-side hug, drift rate is read off the port beam."""
    history = deque([
        _rec(1, sectors=tuple(
            (0.30, 0.20) if i == _PORT_BEAM else (0.0, 1.0) for i in range(SECTOR_COUNT)
        )),
        _rec(2, sectors=tuple(
            (0.30, 0.30) if i == _PORT_BEAM else (0.0, 1.0) for i in range(SECTOR_COUNT)
        )),
    ])
    sig = _derive_signals(history, "port")
    assert sig.target_drift_rate is not None
    assert sig.target_drift_rate > 0


def test_target_drift_rate_none_with_short_history():
    history = deque([_rec(1)])
    sig = _derive_signals(history, "starboard")
    assert sig.target_drift_rate is None


def test_median_speed_skips_none_readings():
    history = deque([
        _rec(1, speed_kt=None),
        _rec(2, speed_kt=11.0),
        _rec(3, speed_kt=None),
        _rec(4, speed_kt=12.0),
        _rec(5, speed_kt=10.0),
    ])
    sig = _derive_signals(history, "starboard")
    # readable: 11, 12, 10 → sorted 10, 11, 12 → median 11
    assert sig.median_speed_kt == 11.0
