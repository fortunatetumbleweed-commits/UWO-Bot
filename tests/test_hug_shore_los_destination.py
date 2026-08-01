"""§13.16.5 — Tests for Step 4: destination-based LOS guidance.

When the HugShoreGoal is configured with a destination (lat, lon), it
computes the compass bearing from the bot's current HUD position to
the destination each tick.  This bearing becomes the *primary*
deliberative anchor for `_effective_goal_heading_deg()`, sitting above
the §13.16.4 history-inferred sequencer signal — the canonical
Lekkas-Fossen marine-USV LOS guidance pattern.

Priority order (3-layer architecture):
  1. Explicit manual override (`goal_heading_deg`)
  2. **LOS bearing to destination** (this file)
  3. History-inferred (§13.16.4)
  4. None → centroid fallback (§13.16.1)
"""
from __future__ import annotations

import pytest

from brain.goals.hug_shore import (
    HugShoreGoal,
    HugPhase,
    TickRecord,
    _angle_diff_deg,
)


def test_bearing_due_north_when_destination_north():
    """Bot at Cairo (30.12, 30.47), destination at (35.00, 30.47) —
    straight north → bearing 0°."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=35.00, endpoint_lon=30.47,
    )
    goal.set_hud(lat=30.12, lon=30.47)
    bearing = goal._los_bearing_deg()
    assert bearing is not None
    assert _angle_diff_deg(bearing, 0.0) < 1.0


def test_bearing_due_east_when_destination_east():
    """Destination east of current → bearing 90°."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=30.12, endpoint_lon=35.00,
    )
    goal.set_hud(lat=30.12, lon=30.47)
    bearing = goal._los_bearing_deg()
    assert bearing is not None
    assert _angle_diff_deg(bearing, 90.0) < 1.0


def test_bearing_due_south_when_destination_south():
    """Destination south → bearing 180°."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=25.00, endpoint_lon=30.47,
    )
    goal.set_hud(lat=30.12, lon=30.47)
    bearing = goal._los_bearing_deg()
    assert bearing is not None
    assert _angle_diff_deg(bearing, 180.0) < 1.0


def test_bearing_due_west_when_destination_west():
    """Destination west → bearing 270°."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=30.12, endpoint_lon=25.00,
    )
    goal.set_hud(lat=30.12, lon=30.47)
    bearing = goal._los_bearing_deg()
    assert bearing is not None
    assert _angle_diff_deg(bearing, 270.0) < 1.0


def test_bearing_diagonal_northeast():
    """Destination at +1 lat, +1 lon (equal NE) → bearing 45°."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=31.12, endpoint_lon=31.47,
    )
    goal.set_hud(lat=30.12, lon=30.47)
    bearing = goal._los_bearing_deg()
    assert bearing is not None
    assert _angle_diff_deg(bearing, 45.0) < 1.0


def test_bearing_updates_as_position_changes():
    """As the bot moves toward the destination, the LOS bearing
    updates each tick — pure-function property check."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=35.00, endpoint_lon=30.47,
    )
    # Start due south of destination → bearing N (0°).
    goal.set_hud(lat=30.12, lon=30.47)
    bearing_1 = goal._los_bearing_deg()
    # Now drift slightly east → bearing shifts NW (a bit < 0° i.e. close to 350°).
    goal.set_hud(lat=30.12, lon=31.47)
    bearing_2 = goal._los_bearing_deg()
    # Now drift far east → bearing shifts more.
    goal.set_hud(lat=30.12, lon=33.00)
    bearing_3 = goal._los_bearing_deg()
    assert bearing_1 is not None and bearing_2 is not None and bearing_3 is not None
    assert _angle_diff_deg(bearing_1, 0.0) < 1.0
    # Bearings 2 and 3 should be on the NW side (toward 350° / 320° range).
    assert _angle_diff_deg(bearing_2, 0.0) > 5.0
    assert _angle_diff_deg(bearing_3, 0.0) > _angle_diff_deg(bearing_2, 0.0)


def test_bearing_none_when_no_destination():
    """No destination set → None."""
    goal = HugShoreGoal(side="port", max_ticks=10, driver_mode="vfh")
    goal.set_hud(lat=30.12, lon=30.47)
    assert goal._los_bearing_deg() is None


def test_bearing_none_when_no_hud_position():
    """Destination set but no HUD reading → None."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=35.00, endpoint_lon=30.47,
    )
    # No set_hud call.
    assert goal._los_bearing_deg() is None


def test_priority_explicit_overrides_los():
    """Manual `goal_heading_deg` wins over LOS bearing."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        goal_heading_deg=90.0,           # explicit E
        endpoint_lat=35.00,            # would compute as N
        endpoint_lon=30.47,
    )
    goal.set_hud(lat=30.12, lon=30.47)
    # Effective goal = explicit, not LOS.
    assert goal._effective_goal_heading_deg() == 90.0


def test_priority_los_overrides_inferred():
    """LOS bearing wins over history-inferred goal."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=35.00, endpoint_lon=30.47,
    )
    goal.set_hud(lat=30.12, lon=30.47)
    # Prime the inferred goal with WEST.
    for i in range(15):
        goal.history.append(TickRecord(
            tick=i, wall_time=float(i), heading=270.0, raw_heading=270.0,
            rejected=False, lat=None, lon=None, speed_kt=10.0,
            sectors=(), commanded_deg=0.0, actual_delta=None,
            phase=HugPhase.HUGGING, chosen_sector=None,
            ideal_sector=None, cost_dump={},
        ))
    goal._update_inferred_goal_heading()
    # Inferred goal should be W (~270°).  But LOS says N (~0°).
    assert goal._inferred_goal_heading_deg is not None
    assert _angle_diff_deg(goal._inferred_goal_heading_deg, 270.0) < 1.0
    # Effective goal should be LOS (N), not inferred (W).
    effective = goal._effective_goal_heading_deg()
    assert effective is not None
    assert _angle_diff_deg(effective, 0.0) < 1.0, (
        f"LOS should override inferred; got effective={effective:.0f}°, "
        f"inferred={goal._inferred_goal_heading_deg:.0f}°"
    )


def test_cairo_departure_north_scenario():
    """Replays the user's Cairo case: bot at (30.12, 30.47), wants to
    go north to a port like Alexandria (~31.2, 29.9 in game coords).
    Without LOS, the bot's inferred goal locks onto whatever direction
    it's been wandering.  With LOS, it always pulls toward the
    destination.  After a few ticks of going wrong (W), the LOS still
    says N — and that's what the reactive layer should consume."""
    goal = HugShoreGoal(
        side="port", max_ticks=10, driver_mode="vfh",
        endpoint_lat=31.20, endpoint_lon=29.90,   # ~Alexandria-ish
    )
    # Simulate bot starting at Cairo and wandering W for a few ticks.
    cairo_lat, cairo_lon = 30.12, 30.47
    for i, hdg in enumerate([268, 285, 305, 280, 245, 207]):
        # Bot moves slightly westward each tick.
        goal.set_hud(lat=cairo_lat + 0.01 * i, lon=cairo_lon - 0.10 * i)
        goal.history.append(TickRecord(
            tick=i, wall_time=float(i), heading=hdg, raw_heading=hdg,
            rejected=False, lat=cairo_lat + 0.01 * i,
            lon=cairo_lon - 0.10 * i, speed_kt=10.0,
            sectors=(), commanded_deg=0.0, actual_delta=None,
            phase=HugPhase.HUGGING, chosen_sector=None,
            ideal_sector=None, cost_dump={},
        ))
    goal._update_inferred_goal_heading()
    # Inferred goal is the wandering W heading (~266°).
    if goal._inferred_goal_heading_deg is not None:
        assert _angle_diff_deg(goal._inferred_goal_heading_deg, 270.0) < 30.0
    # But the LOS bearing should be roughly NW (destination is N and
    # slightly W of starting position; bot's also drifted W so it's
    # now more directly N of starting → bearing will be N to NW).
    los = goal._los_bearing_deg()
    assert los is not None
    # Effective goal must be the LOS, NOT the inferred-westward.
    effective = goal._effective_goal_heading_deg()
    assert effective is not None
    assert effective == los, (
        f"Effective should equal LOS; got effective={effective:.0f}°, "
        f"los={los:.0f}°, inferred={goal._inferred_goal_heading_deg}"
    )
    # And the LOS bearing should point generally toward N (not W).
    # Specifically: Alexandria-ish is at lat 31.20, current lat 30.17 →
    # dlat = +1.03 (north).  Current lon 29.97, dest lon 29.90 → dlon
    # = -0.07 (very slightly west).  Bearing ≈ ~356° (just W of N).
    assert _angle_diff_deg(los, 0.0) < 30.0, (
        f"LOS should point ~N; got {los:.0f}°"
    )
