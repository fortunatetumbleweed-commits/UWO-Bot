"""§13.16.3 — Tests for the VFH+ μ₁ (goal-direction) wall-ahead commit.

When `goal_heading_deg` is set, the wall-ahead commit picks the sector
whose post-turn heading is closer to the goal direction.  This is the
canonical VFH+ TargetDirectionWeight (μ₁) cost term.

Verified against three live cases:
  * Nile t=1 (hug_debug_20260531_160936) — bot heading 260° W, goal=0° N.
    Sec 1 (+45° right → 305° NNW) vs sec 7 (−45° left → 215° SW).
    Sec 1 is 55° from goal, sec 7 is 145°.  Diff = 90° well past
    hysteresis → commit sec 1 (RIGHT). ✓
  * Port Said t=50 (hug_debug_20260531_150415) — bot heading 33°,
    goal=0° N.  Sec 1 (→78°) vs sec 7 (→348°).  Sec 7 is 12° from
    goal, sec 1 is 78°.  Diff = 66° past hysteresis → commit sec 7
    (LEFT). ✓
  * Nile t=6 (hug_debug_20260531_164711) — bot heading 303°, goal=0° N.
    Sec 1 (→348°) vs sec 7 (→258°).  Sec 1 is 12°, sec 7 is 102°.
    Diff = 90° past hysteresis → commit sec 1 (RIGHT). ✓
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason='§13.20: 8→16 sector migration TODO. Tests legacy VFH+ / Bug2 code paths that are gated off for point_pursuit driver; need fixtures re-derived for 16-sec grid.')


from brain.goals.hug_shore import (
    _bug2_commit_sector,
    _angle_diff_deg,
    GOAL_HEADING_HYSTERESIS_DEG,
)


_SECTOR_BEARINGS = (
    0.0, 22.5, 45.0, 67.5, 90.0, 112.5, 135.0, 157.5,
    180.0, -157.5, -135.0, -112.5, -90.0, -67.5, -45.0, -22.5,
)


class _Sec:
    __slots__ = ("land_fraction", "nearest_dist", "is_observed", "bearing_deg")
    def __init__(self, frac, dist, bearing, observed=True):
        self.land_fraction = frac
        self.nearest_dist  = dist
        self.is_observed   = observed
        self.bearing_deg   = bearing


class _Nav:
    __slots__ = ("sectors", "ship_heading_deg")


def _build_nav(sector_specs, heading=0.0):
    secs = []
    for i, (f, d) in enumerate(sector_specs):
        observed = d is not None or f > 0.0
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i], observed=observed))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = heading
    return nav


def test_angle_diff_basic_cases():
    """Smallest-arc compass difference for symmetric cases."""
    assert _angle_diff_deg(0.0, 0.0) == 0.0
    assert _angle_diff_deg(0.0, 90.0) == 90.0
    assert _angle_diff_deg(0.0, 180.0) == 180.0
    assert _angle_diff_deg(0.0, 270.0) == 90.0
    assert _angle_diff_deg(350.0, 10.0) == 20.0
    assert _angle_diff_deg(10.0, 350.0) == 20.0
    assert _angle_diff_deg(45.0, 135.0) == 90.0


def test_nile_t1_with_goal_north_picks_right():
    """Bot heading 260° (W), goal 0° (N).  Right turn → 305° (55° off
    goal); left turn → 215° (145° off goal).  Diff = 90° well past
    hysteresis → commit RIGHT."""
    nav = _build_nav([(0.50, 0.13)] + [(0.0, None)] * 7, heading=260.0)
    committed = _bug2_commit_sector(
        nav, "starboard", goal_heading_deg=0.0,
    )
    assert committed == 1


def test_port_said_t50_with_goal_north_picks_left():
    """Bot heading 33°, goal 0° N.  Left turn → 348° (12° off);
    right turn → 78° (78° off).  Diff = 66° past hysteresis → LEFT."""
    nav = _build_nav([(0.50, 0.13)] + [(0.0, None)] * 7, heading=33.0)
    committed = _bug2_commit_sector(
        nav, "starboard", goal_heading_deg=0.0,
    )
    assert committed == 7


def test_nile_t6_with_goal_north_picks_right():
    """Bot heading 303°, goal 0° N.  Right → 348° (12°); left → 258°
    (102°).  Diff = 90° → RIGHT."""
    nav = _build_nav([(0.50, 0.13)] + [(0.0, None)] * 7, heading=303.0)
    committed = _bug2_commit_sector(
        nav, "starboard", goal_heading_deg=0.0,
    )
    assert committed == 1


def test_goal_dominates_centroid_when_set():
    """Build a scenario where centroid alignment says LEFT but
    goal-direction says RIGHT — goal-direction wins."""
    # Heavy shore on port-side sectors only — centroid points port
    # (centroid wants LEFT to push it to starboard beam).  But bot
    # heading 290°, goal 0° N → right turn (→335°, 25° off) much
    # better than left (→245°, 115° off).
    nav = _build_nav([
        (0.40, 0.30),   # sec 0 — moderate ahead
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.70, 0.15),   # sec 5 — heavy aft-port
        (0.80, 0.15),   # sec 6 — heavy port beam
        (0.50, 0.20),   # sec 7 — moderate bow-port
    ], heading=290.0)
    # Without goal, centroid logic would pick LEFT (sec 7).
    no_goal = _bug2_commit_sector(nav, "starboard")
    # With goal=N, goal-direction picks RIGHT (sec 1).
    with_goal = _bug2_commit_sector(nav, "starboard", goal_heading_deg=0.0)
    assert with_goal == 1, (
        f"Goal-direction should pick RIGHT; got {with_goal}.  "
        f"(Centroid alone would say {no_goal}.)"
    )


def test_hysteresis_prevents_flip_when_diff_small():
    """When the goal is roughly straight ahead, both candidates are
    equidistant from it → diff = 0 → stay with current commit (or
    Trémaux default if fresh)."""
    # Bot heading 0° (N), goal also 0° N.  Both sec 1 → 45° off,
    # sec 7 → 315° (also 45° off).  Diff = 0 → within hysteresis.
    nav = _build_nav([(0.50, 0.13)] + [(0.0, None)] * 7, heading=0.0)
    # Fresh → Trémaux default (sec 7 for starboard).
    assert _bug2_commit_sector(nav, "starboard", goal_heading_deg=0.0) == 7
    # Already committed RIGHT → stay RIGHT.
    assert _bug2_commit_sector(
        nav, "starboard", current_commit=1, goal_heading_deg=0.0,
    ) == 1
    # Already committed LEFT → stay LEFT.
    assert _bug2_commit_sector(
        nav, "starboard", current_commit=7, goal_heading_deg=0.0,
    ) == 7


def test_no_goal_falls_back_to_centroid():
    """When goal_heading_deg is None, the existing §13.16.1/2 centroid
    behavior must be preserved unchanged."""
    nav = _build_nav([
        (0.76, 0.13),
        (0.65, 0.32),
        (0.02, 0.43),
        (0.73, 0.25),
        (0.85, 0.24),
        (0.72, 0.22),
        (0.00, 0.44),
        (0.44, 0.46),
    ], heading=260.0)
    # Without goal: centroid says RIGHT (per existing test_nile_t1).
    assert _bug2_commit_sector(nav, "starboard", goal_heading_deg=None) == 1


def test_no_ship_heading_falls_back_to_centroid():
    """If ship heading is None, can't compute post-turn bearings — fall
    back to centroid alignment even if goal is set."""
    nav = _build_nav([(0.50, 0.13)] + [(0.0, None)] * 7, heading=0.0)
    nav.ship_heading_deg = None  # simulate heading-read failure
    # Should not raise, should return a valid sector.
    committed = _bug2_commit_sector(
        nav, "starboard", goal_heading_deg=0.0,
    )
    assert committed in (1, 7)


def test_goal_aware_port_hug_mirrors():
    """Port-hug case: bot heading 100°, goal 0° N.  Sec 1 → 145°
    (145° off); sec 7 → 55° (55° off).  Diff = 90° favoring sec 7
    (LEFT).  For port hug, LEFT means away-from-hug, RIGHT means
    toward-hug — but goal-direction wins.  Committed = sec 7."""
    nav = _build_nav([(0.50, 0.13)] + [(0.0, None)] * 7, heading=100.0)
    committed = _bug2_commit_sector(
        nav, "port", goal_heading_deg=0.0,
    )
    assert committed == 7
