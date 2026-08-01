"""§13.17 — Tests for the Goal-Point Generator.

The generator dispatches on (side, destination) presence to produce a
single intermediate target point each tick.  See
`docs/lyapunov_point_pursuit_design.md` §3.
"""
from __future__ import annotations

import math

import pytest

from brain.goals.hug_shore import (
    _generate_waypoint,
    _hug_mode_waypoint,
    _destination_mode_waypoint,
    POINT_PURSUIT_L,
)


# Tolerance for floating-point lat/lon comparison.
TOL = 1e-6


def test_destination_mode_due_north():
    """Bot at (30, 30), destination at (35, 30) — straight north.
    Target sits L units north of current."""
    target = _destination_mode_waypoint(30.0, 30.0, 35.0, 30.0)
    expected_lat = 30.0 + POINT_PURSUIT_L
    assert abs(target[0] - expected_lat) < TOL
    assert abs(target[1] - 30.0) < TOL


def test_destination_mode_due_east():
    target = _destination_mode_waypoint(30.0, 30.0, 30.0, 35.0)
    assert abs(target[0] - 30.0) < TOL
    assert abs(target[1] - (30.0 + POINT_PURSUIT_L)) < TOL


def test_destination_mode_diagonal_northeast():
    """Diagonal — target is L units along the LOS, on the NE diagonal."""
    target = _destination_mode_waypoint(30.0, 30.0, 31.0, 31.0)
    diag = math.sqrt(2) / 2
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L * diag)) < 1e-4
    assert abs(target[1] - (30.0 + POINT_PURSUIT_L * diag)) < 1e-4


def test_destination_mode_clamps_at_destination():
    """When destination is within L of current, target IS the destination."""
    target = _destination_mode_waypoint(30.0, 30.0,
                                             30.0 + 0.01, 30.0 + 0.01)
    # Distance 0.01·sqrt(2) ≈ 0.014, well within L (~0.10).
    assert target == (30.0 + 0.01, 30.0 + 0.01)


def test_hug_mode_returns_none_without_lyap_state():
    """No Lyapunov state available → no target point."""
    target = _hug_mode_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        lyap_state=None, side="starboard",
    )
    assert target is None


def test_hug_mode_aligned_with_tangent_no_drift():
    """When bot is already at d_star (no drift) and bow is on tangent
    (θ_err=0), target sits L units along current heading."""
    target = _hug_mode_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,  # N
        lyap_state=(0.20, 0.20, 0.0),  # d == d_star, θ_err=0
        side="starboard",
    )
    # Tangent = bow + θ_err = 0° (N).  Target = current + L·(cos0, sin0).
    assert target is not None
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L)) < TOL
    assert abs(target[1] - 30.0) < TOL


def test_hug_mode_tangent_rotates_target():
    """Bow heading N but tangent is NE (θ_err=+45°) → target NE of bot.
    θ_err is in DEGREES (matches `_lyapunov_state` and the live path)."""
    target = _hug_mode_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        lyap_state=(0.20, 0.20, 45.0),
        side="starboard",
    )
    diag = math.sqrt(2) / 2
    assert target is not None
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L * diag)) < TOL
    assert abs(target[1] - (30.0 + POINT_PURSUIT_L * diag)) < TOL


def test_hug_mode_lateral_correction_toward_shore_starboard():
    """Starboard hug, bot drifted offshore (d > d_star) → target nudges
    perpendicular toward starboard side (shore is on right)."""
    # Bot heading N, tangent aligned (θ_err=0), but d > d_star.
    target = _hug_mode_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        lyap_state=(0.30, 0.20, 0.0),   # drifted 0.10 offshore
        side="starboard",
    )
    # Pure tangent target would be (30 + L, 30).  Lateral correction
    # adds an east (starboard for N-bound) nudge.
    assert target is not None
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L)) < TOL  # forward unchanged
    assert target[1] > 30.0  # nudged east toward starboard shore


def test_hug_mode_lateral_correction_toward_shore_port():
    """Port hug, drifted offshore → target nudges west (port side)."""
    target = _hug_mode_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        lyap_state=(0.30, 0.20, 0.0),
        side="port",
    )
    assert target is not None
    assert abs(target[0] - (30.0 + POINT_PURSUIT_L)) < TOL
    assert target[1] < 30.0  # nudged west toward port shore


def test_generator_neither_returns_none():
    """No side, no destination → refuse (None)."""
    t = _generate_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        side=None, endpoint_lat=None, endpoint_lon=None,
        lyap_state=(0.20, 0.20, 0.0),
    )
    assert t is None


def test_generator_destination_only():
    """Only destination set → destination-mode LOS target."""
    t = _generate_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        side=None, endpoint_lat=35.0, endpoint_lon=30.0,
        lyap_state=None,
    )
    assert t is not None
    assert abs(t[0] - (30.0 + POINT_PURSUIT_L)) < TOL


def test_generator_hug_only():
    """Only side set, valid Lyapunov state → hug-mode tangent target."""
    t = _generate_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        side="starboard", endpoint_lat=None, endpoint_lon=None,
        lyap_state=(0.20, 0.20, 0.0),
    )
    assert t is not None


def test_generator_combined_is_weighted_blend():
    """Both set → weighted average of hug and destination targets.
    With dest pointing N and hug tangent pointing N, results coincide.
    With different directions, the blend lies between them."""
    # Hug tangent = N, destination = E.  Blend should be NE-ish.
    t = _generate_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        side="starboard", endpoint_lat=30.0, endpoint_lon=35.0,
        lyap_state=(0.20, 0.20, 0.0),
    )
    assert t is not None
    # Hug target ≈ (30+L, 30); dest target ≈ (30, 30+L).
    # Blend with w=0.5 → (30 + L/2, 30 + L/2).
    assert t[0] > 30.0 and t[0] < 30.0 + POINT_PURSUIT_L
    assert t[1] > 30.0 and t[1] < 30.0 + POINT_PURSUIT_L


def test_hug_mode_gated_when_target_sector_has_no_shore():
    """§13.17.1 — When the configured-side target sector has no observed
    shore (sec 2 frac < SHORE_VISIBLE_FRAC for starboard), the hug
    contribution is suppressed.  Otherwise Lyapunov falls through to
    bow-target shore and produces a wrong-side target — observed at
    Cairo departure t=1 of hug_debug_20260531_200352."""
    from brain.goals.hug_shore import SHORE_VISIBLE_FRAC

    # Build nav with shore on bow (sec 1) but NOT on starboard beam (sec 2).
    # This mirrors the t=1 Cairo case.
    class _Sec:
        __slots__ = ("land_fraction", "nearest_dist", "is_observed",
                     "bearing_deg")
        def __init__(self, f, d, b):
            self.land_fraction = f
            self.nearest_dist = d
            self.is_observed = True
            self.bearing_deg = b
    class _Nav:
        __slots__ = ("sectors", "ship_heading_deg")
    nav = _Nav()
    nav.ship_heading_deg = 0.0
    # §13.20: 16 sectors of 22.5°.  In-between sectors (idx 1, 3, ...)
    # default to empty so the bearing-test semantics from the 8-sector
    # era are preserved.
    nav.sectors = (
        _Sec(0.75, 0.35, 0.0),     # 0: ahead has shore
        _Sec(0.00, None, 22.5),    # 1: in-between (no data)
        _Sec(0.59, 0.33, 45.0),    # 2: bow-stbd has shore
        _Sec(0.00, None, 67.5),    # 3
        _Sec(0.02, 0.45, 90.0),    # 4: stbd beam CLEAR
        _Sec(0.00, None, 112.5),   # 5
        _Sec(0.80, 0.23, 135.0),   # 6
        _Sec(0.00, None, 157.5),   # 7
        _Sec(0.89, 0.15, 180.0),   # 8
        _Sec(0.00, None, -157.5),  # 9
        _Sec(0.75, 0.20, -135.0),  # 10
        _Sec(0.00, None, -112.5),  # 11
        _Sec(0.00, 0.42, -90.0),   # 12
        _Sec(0.00, None, -67.5),   # 13
        _Sec(0.43, 0.48, -45.0),   # 14
        _Sec(0.00, None, -22.5),   # 15
    )
    # With nav provided and target side empty, hug-mode returns None
    # even when lyap_state is non-None.
    t = _hug_mode_waypoint(
        30.0, 30.0, 0.0, lyap_state=(0.20, 0.20, 0.0),
        side="starboard", nav=nav,
    )
    assert t is None
    # Without nav (legacy callers), no gate fires.
    t_legacy = _hug_mode_waypoint(
        30.0, 30.0, 0.0, lyap_state=(0.20, 0.20, 0.0),
        side="starboard", nav=None,
    )
    assert t_legacy is not None


def test_combined_mode_falls_back_to_destination_when_target_side_empty():
    """§13.17.1 — When target side is empty and destination is set, the
    combined-mode target should equal the destination-mode target
    (hug contribution is zero)."""
    class _Sec:
        __slots__ = ("land_fraction", "nearest_dist", "is_observed",
                     "bearing_deg")
        def __init__(self, f, d, b):
            self.land_fraction = f
            self.nearest_dist = d
            self.is_observed = True
            self.bearing_deg = b
    class _Nav:
        __slots__ = ("sectors", "ship_heading_deg")
    nav = _Nav()
    nav.ship_heading_deg = 0.0
    # §13.20: 16 sectors of 22.5°.  Old "sec 2" (target side, 90°)
    # is now idx 4.
    nav.sectors = (
        _Sec(0.75, 0.35, 0.0),     # 0
        _Sec(0.00, None, 22.5),    # 1
        _Sec(0.59, 0.33, 45.0),    # 2
        _Sec(0.00, None, 67.5),    # 3
        _Sec(0.02, 0.45, 90.0),    # 4 ← below threshold (target side)
        _Sec(0.00, None, 112.5),   # 5
        _Sec(0.80, 0.23, 135.0),   # 6
        _Sec(0.00, None, 157.5),   # 7
        _Sec(0.89, 0.15, 180.0),   # 8
        _Sec(0.00, None, -157.5),  # 9
        _Sec(0.75, 0.20, -135.0),  # 10
        _Sec(0.00, None, -112.5),  # 11
        _Sec(0.00, 0.42, -90.0),   # 12
        _Sec(0.00, None, -67.5),   # 13
        _Sec(0.43, 0.48, -45.0),   # 14
        _Sec(0.00, None, -22.5),   # 15
    )
    t_combined = _generate_waypoint(
        current_lat=30.0, current_lon=30.0, ship_heading_deg=0.0,
        side="starboard",
        endpoint_lat=35.0, endpoint_lon=30.0,
        lyap_state=(0.20, 0.20, 0.0),
        nav=nav,
    )
    # Should equal destination-only target.
    t_dest_only = _destination_mode_waypoint(30.0, 30.0, 35.0, 30.0)
    assert t_combined is not None
    assert abs(t_combined[0] - t_dest_only[0]) < TOL
    assert abs(t_combined[1] - t_dest_only[1]) < TOL


def test_generator_missing_position_returns_none():
    """No HUD lat/lon → can't compute → None."""
    t = _generate_waypoint(
        current_lat=None, current_lon=None, ship_heading_deg=0.0,
        side="starboard", endpoint_lat=35.0, endpoint_lon=30.0,
        lyap_state=(0.20, 0.20, 0.0),
    )
    assert t is None


def test_hug_mode_tangent_anchors_on_destination():
    """§13.21 — When the bot's bow has drifted past the shore-along axis,
    the unanchored tangent (bow + θ_err) points AWAY from the
    destination.  With destination provided, `_hug_mode_waypoint` must
    flip to the opposing tangent direction (180° apart) so the bot
    pursues the goal-anchored "leave point".

    Setup mirrors hug_debug_20260601_161713 t192-t197: bot is roughly
    facing west (heading 270°) but destination is south (LOS 180°);
    shore on starboard runs N-S.  θ_err=+90° gives tangent 360°≡0° (N).
    The destination-anchored alternate is 180° (S), which is correct.
    """
    class _Sec:
        __slots__ = ("land_fraction", "nearest_dist", "is_observed",
                     "bearing_deg")
        def __init__(self, f, d, b):
            self.land_fraction = f
            self.nearest_dist = d
            self.is_observed = True
            self.bearing_deg = b

    class _Nav:
        __slots__ = ("sectors", "ship_heading_deg")

    # Shore on starboard (sec 4 for ship heading 270° = bearing 0° world).
    # Mark sec 4 as having shore so the §13.17.1 gate passes; the LSQ
    # input doesn't matter for this test — we override lyap_state below.
    nav = _Nav()
    nav.ship_heading_deg = 270.0
    nav.sectors = tuple(
        _Sec(0.6 if i == 4 else 0.0,
             0.30 if i == 4 else None,
             i * 22.5)
        for i in range(16)
    )

    # Patch _lyapunov_state to return θ_err = +90° (tangent ≡ heading + 90°)
    # and d == d_star (no lateral correction, so target lat sign isolates
    # the tangent-direction choice).
    import brain.goals.hug_shore as hs
    orig = hs._lyapunov_state
    hs._lyapunov_state = lambda nav, side: (0.20, 0.20, 90.0)
    try:
        # Without destination — unanchored tangent picks (270 + 90) = 0° (N).
        t_unanchored = _hug_mode_waypoint(
            current_lat=18.230, current_lon=32.420,
            ship_heading_deg=270.0,
            lyap_state=None, side="starboard", nav=nav,
        )
        assert t_unanchored is not None
        # Target north of bot → lat increases.
        assert t_unanchored[0] > 18.230

        # With destination south of bot (lat 14.0, lon 32.420), LOS = 180°.
        # The alternate tangent (180°) is closer to LOS than 0° → flip.
        t_anchored = _hug_mode_waypoint(
            current_lat=18.230, current_lon=32.420,
            ship_heading_deg=270.0,
            lyap_state=None, side="starboard", nav=nav,
            endpoint_lat=14.000, endpoint_lon=32.420,
        )
        assert t_anchored is not None
        # Target south of bot → lat decreases.
        assert t_anchored[0] < 18.230
    finally:
        hs._lyapunov_state = orig


def test_hug_mode_tangent_keeps_default_when_aligned_with_destination():
    """§13.21 — When the bow-anchored tangent already points roughly
    toward the destination, destination anchoring is a no-op."""
    class _Sec:
        __slots__ = ("land_fraction", "nearest_dist", "is_observed",
                     "bearing_deg")
        def __init__(self, f, d, b):
            self.land_fraction = f
            self.nearest_dist = d
            self.is_observed = True
            self.bearing_deg = b

    class _Nav:
        __slots__ = ("sectors", "ship_heading_deg")

    nav = _Nav()
    nav.ship_heading_deg = 0.0  # bow N
    nav.sectors = tuple(
        _Sec(0.6 if i == 4 else 0.0,
             0.30 if i == 4 else None,
             i * 22.5)
        for i in range(16)
    )

    import brain.goals.hug_shore as hs
    orig = hs._lyapunov_state
    # θ_err = 0 → tangent = bow = 0° (N).  Destination also N.
    hs._lyapunov_state = lambda nav, side: (0.30, 0.20, 0.0)
    try:
        t = _hug_mode_waypoint(
            current_lat=30.0, current_lon=30.0,
            ship_heading_deg=0.0,
            lyap_state=None, side="starboard", nav=nav,
            endpoint_lat=35.0, endpoint_lon=30.0,
        )
        assert t is not None
        # Target stays north (no flip).
        assert t[0] > 30.0
    finally:
        hs._lyapunov_state = orig
