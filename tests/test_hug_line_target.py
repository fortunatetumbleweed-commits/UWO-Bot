"""§13.17.2 — Tests for the hug-line target generator (Slice 1+2).

The hug line is the line parallel to the shore tangent, at perpendicular
distance `d_star` from the shore, on the bot's configured side.  The
target each tick is `bot's perpendicular projection onto the hug line +
L · unit(tangent)`.

This is the classical Lekkas-Fossen ILOS path-following form with the
"path" computed dynamically from current minimap perception.
"""
from __future__ import annotations

import math

import pytest

from brain.goals.hug_shore import (
    _extract_shore_line,
    _hug_line_waypoint,
    _hug_mode_waypoint,
    _max_angular_spread_deg,
    POINT_PURSUIT_L,
)


TOL = 1e-6
_SECTOR_BEARINGS = (
    0.0, 22.5, 45.0, 67.5, 90.0, 112.5, 135.0, 157.5,
    180.0, -157.5, -135.0, -112.5, -90.0, -67.5, -45.0, -22.5,
)


class _Sec:
    __slots__ = ("land_fraction", "nearest_dist", "is_observed", "bearing_deg")
    def __init__(self, frac, dist, bearing):
        self.land_fraction = frac
        self.nearest_dist  = dist
        self.is_observed   = True
        self.bearing_deg   = bearing


class _Nav:
    __slots__ = ("sectors", "ship_heading_deg")


def _build_nav(specs, heading=0.0):
    # §13.20: auto-expand legacy 8-spec format to 16 by inserting
    # empty in-between sectors at the new (odd) indices.
    if len(specs) == 8:
        expanded = []
        for spec in specs:
            expanded.append(spec)
            expanded.append((0.0, None))
        specs = expanded
    nav = _Nav()
    nav.sectors = tuple(_Sec(f, d, b)
                        for (f, d), b in zip(specs, _SECTOR_BEARINGS))
    nav.ship_heading_deg = heading
    return nav


# ── Slice 2: _hug_line_waypoint geometry ──────────────────────────────

def test_hug_line_aligned_no_drift_forward_step():
    """Bot exactly on hug line (d = d_star), tangent = N.
    Target = bot + L · unit(N) = (bot_lat + L, bot_lon)."""
    target = _hug_line_waypoint(
        current_lat=30.0, current_lon=30.0,
        tangent_compass_deg=0.0,  # N
        perpendicular_distance=0.20, side="starboard", d_star=0.20,
    )
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L)) < TOL
    assert abs(target[1] - 30.0) < TOL


def test_hug_line_drifted_offshore_pulls_target_toward_shore_starboard():
    """Starboard hug, bot drifted offshore (d > d_star).
    Target sits east of bot (toward starboard side), pulling
    bot back to the hug line as it steers toward target."""
    target = _hug_line_waypoint(
        current_lat=30.0, current_lon=30.0,
        tangent_compass_deg=0.0,  # N
        perpendicular_distance=0.30, side="starboard", d_star=0.20,
        # drift = +0.10 → target.lon should be +0.10 east of bot
    )
    # Forward (tangent) component: lat += L
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L)) < TOL
    # Perpendicular (toward shore on starboard) component: lon += 0.10
    assert abs(target[1] - (30.0 + 0.10)) < TOL


def test_hug_line_too_close_pushes_target_offshore_starboard():
    """Bot inside the hug line (d < d_star) → target sits west of bot
    (away from shore)."""
    target = _hug_line_waypoint(
        current_lat=30.0, current_lon=30.0,
        tangent_compass_deg=0.0,
        perpendicular_distance=0.10, side="starboard", d_star=0.20,
        # drift = -0.10 → target.lon should be -0.10 west of bot
    )
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L)) < TOL
    assert abs(target[1] - (30.0 - 0.10)) < TOL


def test_hug_line_port_mirrors_starboard_perpendicular():
    """Port hug, same drift → perpendicular goes the OTHER way."""
    target_stbd = _hug_line_waypoint(
        30.0, 30.0, tangent_compass_deg=0.0,
        perpendicular_distance=0.30, side="starboard", d_star=0.20,
    )
    target_port = _hug_line_waypoint(
        30.0, 30.0, tangent_compass_deg=0.0,
        perpendicular_distance=0.30, side="port", d_star=0.20,
    )
    # Same forward, opposite perpendicular.
    assert abs(target_stbd[0] - target_port[0]) < TOL
    assert abs(target_stbd[1] - 30.0) == pytest.approx(0.10, abs=TOL)
    assert abs(target_port[1] - 30.0) == pytest.approx(0.10, abs=TOL)
    # And opposite signs.
    assert (target_stbd[1] - 30.0) > 0
    assert (target_port[1] - 30.0) < 0


def test_hug_line_tangent_rotates_target():
    """Tangent NE → forward step on NE diagonal."""
    target = _hug_line_waypoint(
        30.0, 30.0, tangent_compass_deg=45.0,
        perpendicular_distance=0.20, side="starboard", d_star=0.20,
    )
    diag = math.sqrt(2) / 2
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L * diag)) < TOL
    assert abs(target[1] - (30.0 + POINT_PURSUIT_L * diag)) < TOL


def test_hug_line_bearing_to_target_decreases_as_bot_approaches():
    """Lyapunov convergence property: as drift decreases, the bearing
    from bot to target rotates back toward pure-tangent direction."""
    def bearing_off_tangent(drift):
        target = _hug_line_waypoint(
            30.0, 30.0, tangent_compass_deg=0.0,
            perpendicular_distance=0.20 + drift,
            side="starboard", d_star=0.20,
        )
        dlat = target[0] - 30.0
        dlon = target[1] - 30.0
        # Bearing relative to tangent (north).  Just atan(perp/tan).
        return abs(math.degrees(math.atan2(dlon, dlat)))
    assert bearing_off_tangent(0.30) > bearing_off_tangent(0.10)
    assert bearing_off_tangent(0.10) > bearing_off_tangent(0.01)


# ── Slice 1: _extract_shore_line gating ───────────────────────────────────

def test_extract_shore_line_returns_state_when_target_side_has_shore():
    """sec 2 has shore (starboard side) → return (tangent, d, d_star)."""
    nav = _build_nav([
        (0.30, 0.40),    # sec 0
        (0.40, 0.30),    # sec 1
        (0.85, 0.20),    # sec 2 — target side, has shore
        (0.50, 0.25),    # sec 3
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ], heading=0.0)
    res = _extract_shore_line(nav, "starboard", 0.0)
    assert res is not None
    tangent, d, d_star = res
    # d > 0, d_star > 0, tangent in [0, 360).
    assert d > 0
    assert d_star > 0
    assert 0 <= tangent < 360


def test_extract_shore_line_returns_none_when_target_side_empty():
    """sec 2 has frac < SHORE_VISIBLE_FRAC → return None."""
    nav = _build_nav([
        (0.30, 0.40),
        (0.40, 0.30),
        (0.02, 0.40),    # sec 2 — target side, NO real shore
        (0.50, 0.25),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ], heading=0.0)
    res = _extract_shore_line(nav, "starboard", 0.0)
    assert res is None


# ── Integration: _hug_mode_waypoint with nav ──────────────────────────

def test_hug_mode_with_nav_uses_hug_line_path():
    """When nav is provided and target side has shore, hug-mode routes
    through _extract_shore_line + _hug_line_waypoint and the result
    matches that direct composition (i.e. the new geometry, not the
    legacy tangent-projection path)."""
    nav = _build_nav([
        (0.10, 0.40),
        (0.20, 0.30),
        (0.85, 0.30),    # sec 2 — target side; has shore
        (0.30, 0.30),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ], heading=0.0)
    # Direct composition: extract → hug-line.
    extracted = _extract_shore_line(nav, "starboard", 0.0)
    assert extracted is not None
    tangent, d, d_star = extracted
    expected = _hug_line_waypoint(
        30.0, 30.0, tangent, d, "starboard", d_star,
    )
    # Integration through _hug_mode_waypoint with nav must match.
    actual = _hug_mode_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        lyap_state=None,  # ignored when nav is provided
        side="starboard", nav=nav,
    )
    assert actual is not None
    assert abs(actual[0] - expected[0]) < TOL
    assert abs(actual[1] - expected[1]) < TOL


def test_hug_mode_with_nav_returns_none_when_target_side_empty():
    """§13.17.1 gate still fires through the new path: target side
    empty → no hug contribution."""
    nav = _build_nav([
        (0.30, 0.40),
        (0.40, 0.30),
        (0.02, 0.40),    # sec 2 empty
        (0.50, 0.25),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ], heading=0.0)
    target = _hug_mode_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        lyap_state=(0.30, 0.20, 0.0), side="starboard", nav=nav,
    )
    assert target is None


# ── §13.21 — Bug2 temporal commitment ─────────────────────────────────────

def _shore_nav(heading_deg):
    return _build_nav([
        (0.30, 0.40),
        (0.40, 0.30),
        (0.85, 0.20),    # sec 2 — starboard shore
        (0.50, 0.25),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ], heading=heading_deg)


def test_commitment_keeps_tangent_when_aligned_with_committed():
    """Committed direction agrees with the bow-driven tangent →
    tangent unchanged."""
    nav = _shore_nav(heading_deg=0.0)
    baseline = _extract_shore_line(nav, "starboard", 0.0)
    assert baseline is not None
    tangent_baseline = baseline[0]
    # Committed direction = the baseline tangent → no flip.
    res = _extract_shore_line(
        nav, "starboard", 0.0,
        committed_direction=tangent_baseline,
    )
    assert res is not None
    assert abs(res[0] - tangent_baseline) < TOL


def test_commitment_flips_tangent_when_committed_is_180_off():
    """Committed direction = baseline tangent + 180° → tangent flips
    to the alt direction.  This is the t366-style fix: even when bow
    has rotated, the committed direction holds the wp on the
    pre-bounce side."""
    nav = _shore_nav(heading_deg=0.0)
    baseline = _extract_shore_line(nav, "starboard", 0.0)
    assert baseline is not None
    tangent_baseline = baseline[0]
    committed = (tangent_baseline + 180.0) % 360.0
    res = _extract_shore_line(
        nav, "starboard", 0.0,
        committed_direction=committed,
    )
    assert res is not None
    # Should now equal the alt (committed-aligned) direction.
    expected = (tangent_baseline + 180.0) % 360.0
    assert abs(res[0] - expected) < TOL


def test_commitment_overrides_endpoint_los():
    """When both committed_direction and endpoint are provided, the
    committed direction wins (temporal context > single-tick LOS).
    Scenario: bot near (30, 30), endpoint due south pulls LOS to 180°,
    but committed=0° (north) holds the tangent on the northbound
    direction."""
    nav = _shore_nav(heading_deg=0.0)
    baseline = _extract_shore_line(nav, "starboard", 0.0)
    assert baseline is not None
    tangent_baseline = baseline[0]
    # Build a contradictory pair: committed agrees with baseline;
    # endpoint LOS would push toward the alt.  Committed should win.
    tangent_alt = (tangent_baseline + 180.0) % 360.0
    # Choose endpoint so LOS bearing ≈ tangent_alt — this exercises
    # the precedence rule: committed > endpoint > bow.
    endpoint_lat = 30.0 + math.cos(math.radians(tangent_alt))
    endpoint_lon = 30.0 + math.sin(math.radians(tangent_alt))
    res = _extract_shore_line(
        nav, "starboard", 0.0,
        current_lat=30.0, current_lon=30.0,
        endpoint_lat=endpoint_lat, endpoint_lon=endpoint_lon,
        committed_direction=tangent_baseline,
    )
    assert res is not None
    # Committed (baseline) wins; tangent is NOT flipped to alt.
    assert abs(res[0] - tangent_baseline) < TOL


def test_no_commitment_falls_back_to_endpoint_los():
    """Sanity check: existing endpoint-LOS behaviour preserved when
    committed_direction is None."""
    nav = _shore_nav(heading_deg=0.0)
    baseline = _extract_shore_line(nav, "starboard", 0.0)
    assert baseline is not None
    tangent_baseline = baseline[0]
    tangent_alt = (tangent_baseline + 180.0) % 360.0
    # Endpoint placed so LOS ≈ tangent_alt → tangent should flip.
    endpoint_lat = 30.0 + math.cos(math.radians(tangent_alt))
    endpoint_lon = 30.0 + math.sin(math.radians(tangent_alt))
    res = _extract_shore_line(
        nav, "starboard", 0.0,
        current_lat=30.0, current_lon=30.0,
        endpoint_lat=endpoint_lat, endpoint_lon=endpoint_lon,
        # committed_direction omitted → endpoint LOS in effect
    )
    assert res is not None
    assert abs(res[0] - tangent_alt) < TOL


# ── §13.21 — Coverage-aware tangent scoring (forward info gain) ───────────

def _fill_coverage_along_bearing(coverage, lat0, lon0, bearing_deg,
                                   distance_deg, samples):
    """Helper: record cells along a bearing as visited, simulating
    a stretch of trajectory the bot already covered."""
    import math
    br = math.radians(bearing_deg)
    for k in range(samples + 1):
        frac = k / samples
        lat = lat0 + frac * distance_deg * math.cos(br)
        lon = lon0 + frac * distance_deg * math.sin(br)
        coverage.record(k, lat, lon)


def test_coverage_veto_flips_tangent_when_chosen_side_fully_visited():
    """The t366-class fix: committed direction would point into
    already-traveled cells; coverage veto flips it to the unvisited
    side."""
    from brain.goals.coverage_tracker import CoverageTracker
    nav = _shore_nav(heading_deg=0.0)
    baseline = _extract_shore_line(nav, "starboard", 0.0)
    assert baseline is not None
    tangent_baseline = baseline[0]
    tangent_alt = (tangent_baseline + 180.0) % 360.0
    coverage = CoverageTracker()
    # Visit the cells along the BASELINE direction (the wrong one in
    # this scenario): all forward cells along tangent_baseline are
    # now in coverage.visited.
    _fill_coverage_along_bearing(
        coverage, lat0=30.0, lon0=30.0,
        bearing_deg=tangent_baseline,
        distance_deg=0.5, samples=5,
    )
    # No committed direction → tangent_baseline initially picked by
    # bow + θ_err.  Coverage veto should flip to tangent_alt.
    res = _extract_shore_line(
        nav, "starboard", 0.0,
        current_lat=30.0, current_lon=30.0,
        coverage=coverage,
    )
    assert res is not None
    assert abs(res[0] - tangent_alt) < TOL


def test_coverage_no_veto_when_signal_below_margin():
    """When forward cells on both sides are unvisited (open water),
    the veto does NOT fire — commitment (or bow) wins."""
    from brain.goals.coverage_tracker import CoverageTracker
    nav = _shore_nav(heading_deg=0.0)
    baseline = _extract_shore_line(nav, "starboard", 0.0)
    assert baseline is not None
    tangent_baseline = baseline[0]
    # Empty coverage tracker — no cells visited anywhere → both
    # candidates have equal unvisited count → no veto.
    coverage = CoverageTracker()
    res = _extract_shore_line(
        nav, "starboard", 0.0,
        current_lat=30.0, current_lon=30.0,
        coverage=coverage,
    )
    assert res is not None
    assert abs(res[0] - tangent_baseline) < TOL


# ── §13.21 — Commitment warm-up filter (_max_angular_spread_deg) ─────────

def test_max_angular_spread_empty_and_single():
    assert _max_angular_spread_deg([]) == 0.0
    assert _max_angular_spread_deg([42.0]) == 0.0


def test_max_angular_spread_tight_cluster():
    # 290°, 293°, 291° — spread 3°
    s = _max_angular_spread_deg([290.0, 293.0, 291.0])
    assert s <= 3.5


def test_max_angular_spread_handles_wraparound():
    # 358°, 2°, 5° wrap across 0° — spread 7°, not 356°
    s = _max_angular_spread_deg([358.0, 2.0, 5.0])
    assert s <= 8.0


def test_max_angular_spread_catches_voyage_170442_pattern():
    # t1=138° (misread), t2=293°, t3=293° — spread ≈ 155°.
    # Must exceed the 15° stability gate, deferring commitment init.
    s = _max_angular_spread_deg([138.0, 293.0, 293.0])
    assert s > 90.0


def test_coverage_veto_overrides_committed_direction():
    """Strong coverage signal can flip a wrong commitment — the
    safety valve described in the §13.21 docstring.  Setup: committed
    direction = tangent_baseline; ALL cells in that direction are
    visited; alt direction is unvisited → veto fires."""
    from brain.goals.coverage_tracker import CoverageTracker
    nav = _shore_nav(heading_deg=0.0)
    baseline = _extract_shore_line(nav, "starboard", 0.0)
    assert baseline is not None
    tangent_baseline = baseline[0]
    tangent_alt = (tangent_baseline + 180.0) % 360.0
    coverage = CoverageTracker()
    _fill_coverage_along_bearing(
        coverage, lat0=30.0, lon0=30.0,
        bearing_deg=tangent_baseline,
        distance_deg=0.5, samples=5,
    )
    res = _extract_shore_line(
        nav, "starboard", 0.0,
        current_lat=30.0, current_lon=30.0,
        committed_direction=tangent_baseline,
        coverage=coverage,
    )
    assert res is not None
    # Coverage vetoes the committed wrong direction.
    assert abs(res[0] - tangent_alt) < TOL
