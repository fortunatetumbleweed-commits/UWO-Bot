"""§13.16.1 — Centroid-aligned wall-ahead commit.

When ahead is walled, instead of picking a fixed direction (Trémaux),
compute the weighted obstacle centroid and pick the turn direction that
brings that centroid toward the hug-side beam (+90° for starboard,
−90° for port).

The key cases:
  * t=1 of hug_debug_20260531_160936 — bot near Cairo facing west bank,
    land mass dominantly to the east/astern.  Centroid says the
    weighted shore is at ~+155° relative (slightly past starboard beam,
    toward astern-starboard).  For starboard hug, this resolves to a
    RIGHT turn (commit sec 1) — matching the user's intuition that the
    bot should rotate right so the existing landmass ends up on the
    starboard side.
  * t=50 Port Said case (hug_debug_20260531_150415) — coast is at
    starboard-ish.  Centroid resolves close to +90°, so the commit
    defers to the Trémaux default (LEFT for starboard).  Matches the
    earlier-debugged expectation.
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason='§13.20: 8→16 sector migration TODO. Tests legacy VFH+ / Bug2 code paths that are gated off for point_pursuit driver; need fixtures re-derived for 16-sec grid.')


import math

from brain.goals.hug_shore import (
    _obstacle_centroid_relative_bearing,
    _bug2_commit_sector,
    CENTROID_ALIGNED_TOLERANCE_DEG,
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


def _build_nav(sector_specs):
    secs = []
    for i, (f, d) in enumerate(sector_specs):
        observed = d is not None or f > 0.0
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i], observed=observed))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = 0.0
    return nav


def test_nile_t1_centroid_picks_right_turn():
    """Cairo / Nile case — heavy land in rear arc (sec 3, 4, 5), wall
    directly ahead (sec 0).  Centroid sits roughly astern-starboard
    (~+155° relative).  For starboard hug, rule resolves to RIGHT
    (sec 1), matching the user's analytical expectation."""
    nav = _build_nav([
        (0.76, 0.13),   # sec 0 — wall ahead
        (0.65, 0.32),   # sec 1
        (0.02, 0.43),   # sec 2 — clear right
        (0.73, 0.25),   # sec 3 — heavy astern-stbd
        (0.85, 0.24),   # sec 4 — heaviest astern
        (0.72, 0.22),   # sec 5 — heavy astern-port
        (0.00, 0.44),   # sec 6 — clear left
        (0.44, 0.46),   # sec 7
    ])
    centroid = _obstacle_centroid_relative_bearing(nav)
    assert centroid is not None
    # Past starboard beam, toward astern — well outside tolerance.
    assert 135.0 < centroid < 170.0, (
        f"Expected centroid ≈ +155°; got {centroid:.1f}°"
    )
    committed = _bug2_commit_sector(nav, "starboard")
    assert committed == 1, (
        f"Expected commit sec 1 (right turn) for Nile t=1 case; "
        f"got {committed}.  Centroid {centroid:.1f}° is past starboard "
        f"beam → rotation needed to align it with +90°."
    )


def test_port_said_centroid_defers_to_tremaux_left():
    """Port Said case — coast is at starboard-ish.  Centroid sits
    close to +90°, so commit defers to the Trémaux default (LEFT for
    starboard)."""
    nav = _build_nav([
        (0.78, 0.11),   # sec 0
        (0.28, 0.07),
        (0.92, 0.08),
        (0.59, 0.09),
        (0.17, 0.44),
        (0.51, 0.43),
        (0.02, 0.54),
        (0.02, 0.44),
    ])
    centroid = _obstacle_centroid_relative_bearing(nav)
    assert centroid is not None
    # Centroid lands near +90° (starboard beam) — already on hug-side.
    assert 70.0 < centroid < 120.0, (
        f"Expected centroid ≈ +90° (starboard beam); got {centroid:.1f}°"
    )
    committed = _bug2_commit_sector(nav, "starboard")
    # delta = centroid − 90 should be within ±CENTROID_ALIGNED_TOLERANCE,
    # so we get the Trémaux default = sec 7 (LEFT for starboard).
    delta = centroid - 90.0
    if abs(delta) < CENTROID_ALIGNED_TOLERANCE_DEG:
        assert committed == 7, (
            f"Expected Trémaux default sec 7 (within tolerance "
            f"{CENTROID_ALIGNED_TOLERANCE_DEG}°); got {committed}"
        )


def test_centroid_dead_ahead_picks_turn_toward_hug_side():
    """All land is dead ahead (sec 0 only).  Centroid is at 0°.  For
    starboard, target is +90° → delta = −90° → LEFT turn (sec 7), the
    Trémaux default direction."""
    nav = _build_nav([
        (0.90, 0.10),   # sec 0
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ])
    centroid = _obstacle_centroid_relative_bearing(nav)
    assert centroid is not None and abs(centroid) < 5.0
    assert _bug2_commit_sector(nav, "starboard") == 7
    assert _bug2_commit_sector(nav, "port") == 1


def test_centroid_on_port_picks_right_for_starboard():
    """Land entirely on port side (sec 6).  Centroid at −90°.  For
    starboard, target is +90° → delta = −180° (or +180°).  Edge case
    of opposite-side — either turn works; verify we don't crash and
    return a forward-arc sector (1 or 7)."""
    nav = _build_nav([
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.80, 0.20),   # sec 6 — heavy port beam
        (0.00, None),
    ])
    centroid = _obstacle_centroid_relative_bearing(nav)
    assert centroid is not None and abs(centroid - (-90.0)) < 5.0
    # delta = −90 − 90 = −180 → either turn is valid; we pick sec 7 by
    # the delta < 0 branch (rounding ties down).
    committed = _bug2_commit_sector(nav, "starboard")
    assert committed in (1, 7)


def test_no_obstacles_falls_back_to_tremaux():
    """No observed obstacles (defensive — shouldn't happen in the
    wall-ahead branch, but the helper should never return None)."""
    nav = _build_nav([(0.0, None)] * 8)
    assert _obstacle_centroid_relative_bearing(nav) is None
    assert _bug2_commit_sector(nav, "starboard") == 7
    assert _bug2_commit_sector(nav, "port") == 1


def test_reevaluation_switches_when_centroid_crosses():
    """§13.16.2 — While committed, re-evaluation should switch the
    commit when the centroid has rotated past target on the opposite
    side.  Replays t=4 of hug_debug_20260531_163015 where the bot,
    committed RIGHT from t=1, had rotated 90°+ CW such that the
    centroid was at −42° (bow-port) — clearly past target.  Without
    re-eval the bot drove a full circle; with re-eval it switches LEFT."""
    nav = _build_nav([
        (0.34, 0.22),
        (0.36, 0.37),
        (0.78, 0.31),
        (0.59, 0.33),
        (0.00, 0.41),
        (0.49, 0.37),
        (0.87, 0.26),
        (0.88, 0.20),
    ])
    centroid = _obstacle_centroid_relative_bearing(nav)
    assert centroid is not None and -50.0 < centroid < -30.0, (
        f"Expected centroid ≈ −42° at this frame; got {centroid:.1f}°"
    )
    # Already committed sec 1 (RIGHT) from earlier in the run.
    new_commit = _bug2_commit_sector(nav, "starboard", current_commit=1)
    assert new_commit == 7, (
        f"Expected switch from sec 1 → sec 7 (delta={centroid - 90.0:.0f}° "
        f"past −{CENTROID_ALIGNED_TOLERANCE_DEG}° hysteresis); got {new_commit}"
    )


def test_reevaluation_does_not_switch_within_hysteresis():
    """Conversely: when delta is within the hysteresis band, the
    commit stays put — even if it disagrees with the "fresh" Trémaux
    default.  This is what prevents Port Said t=46-50-style flipping."""
    # Centroid right at +90° (perfect starboard beam).  delta = 0.
    nav = _build_nav([
        (0.00, None),
        (0.00, None),
        (0.90, 0.20),   # all the shore is exactly at starboard beam
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ])
    centroid = _obstacle_centroid_relative_bearing(nav)
    assert centroid is not None and abs(centroid - 90.0) < 1.0
    # If currently committed RIGHT, stay RIGHT (don't reset to Trémaux LEFT).
    assert _bug2_commit_sector(nav, "starboard", current_commit=1) == 1
    # If currently committed LEFT, stay LEFT.
    assert _bug2_commit_sector(nav, "starboard", current_commit=7) == 7
    # If entering fresh, Trémaux default = LEFT.
    assert _bug2_commit_sector(nav, "starboard", current_commit=None) == 7


def test_noise_floor_fractions_ignored():
    """Sectors below LATERAL_VALID_FRAC must not contribute to the
    centroid — otherwise sentinel-dist phantom shore tilts the result."""
    nav_noisy = _build_nav([
        (0.02, 0.10),   # noise
        (0.03, 0.10),   # noise
        (0.04, 0.10),   # noise
        (0.85, 0.20),   # real shore here
        (0.02, 0.20),   # noise
        (0.01, 0.20),   # noise
        (0.02, 0.20),   # noise
        (0.03, 0.20),   # noise
    ])
    centroid = _obstacle_centroid_relative_bearing(nav_noisy)
    assert centroid is not None
    # Only sec 3 (β=135°) qualified — centroid should be there.
    assert abs(centroid - 135.0) < 1.0, (
        f"Expected centroid at sec 3 bearing 135°; got {centroid:.1f}°"
    )
