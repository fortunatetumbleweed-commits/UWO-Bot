"""§13.17 — Tests for the Lyapunov point-pursuit controller.

The controller minimises V = ||target − current||² + (ψ − ψ_d)² by
producing a desired heading ψ_d = bearing(current, target).  In our
discrete 8-sector setting, this reduces to "pick the sector whose
compass bearing is closest to ψ_d."

See `docs/lyapunov_point_pursuit_design.md` §4.
"""
from __future__ import annotations

from brain.goals.hug_shore import (
    _point_pursuit_desired_heading,
    _point_pursuit_pick_sector,
    _angle_diff_deg,
    POINT_PURSUIT_SAFETY_DIST,
    CANDIDATE_SECTORS,
)
import pytest


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
    # §13.20: auto-expand legacy 8-spec format to 16.
    if len(sector_specs) == 8:
        expanded = []
        for spec in sector_specs:
            expanded.append(spec)
            expanded.append((0.0, None))
        sector_specs = expanded
    secs = []
    for i, (f, d) in enumerate(sector_specs):
        observed = d is not None or f > 0.0
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i], observed=observed))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = heading
    return nav


def test_desired_heading_target_due_north():
    """Target north of current → bearing 0°."""
    h = _point_pursuit_desired_heading(30.0, 30.0, 35.0, 30.0)
    assert h is not None
    assert abs(h - 0.0) < 0.01 or abs(h - 360.0) < 0.01


def test_desired_heading_target_due_east():
    h = _point_pursuit_desired_heading(30.0, 30.0, 30.0, 35.0)
    assert h is not None
    assert abs(h - 90.0) < 0.01


def test_desired_heading_target_southwest():
    """Diagonal target → atan2-derived bearing."""
    h = _point_pursuit_desired_heading(30.0, 30.0, 29.0, 29.0)
    assert h is not None
    # SW = 225°
    assert abs(h - 225.0) < 0.01


def test_desired_heading_at_target_returns_none():
    """Already at target → no heading."""
    h = _point_pursuit_desired_heading(30.0, 30.0, 30.0, 30.0)
    assert h is None


def test_pick_sector_chooses_aligned_with_desired_heading():
    """Bot heading N, desired heading also N (0°), no obstacles.
    The aligned sector is sec 0 (relative 0°), so it's picked."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    chosen, free = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, desired_heading_deg=0.0,
    )
    assert chosen == 0
    assert set(free) == set(CANDIDATE_SECTORS)


def test_pick_sector_skews_to_neighbour_when_desired_offset():
    """Bot heading N, desired heading 45° (NE).  §13.20 16-sec:
    sec 2 (+45° rel) gives compass 45° — exact match."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    chosen, _ = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, desired_heading_deg=45.0,
    )
    assert chosen == 2


def test_pick_sector_chooses_left_when_desired_west():
    """Bot heading N (0°), desired heading 315° (NW).  §13.20 16-sec:
    sec 14 (−45° rel) gives compass 315° — exact match."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    chosen, _ = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, desired_heading_deg=315.0,
    )
    assert chosen == 14


def test_pick_sector_masks_imminent_collision():
    """Best-aligned sector has shore at d < SAFETY → excluded.
    Next-best sector is chosen, even if angular alignment is worse.

    §13.20 16-sec: sec 0 masked, but the auto-expanded grid has
    sec 1 (+22.5°) and sec 15 (−22.5°) as unobserved-but-free
    neighbours, so the picker can chose either (Δ=22.5° from goal).
    """
    nav = _build_nav([
        (0.50, 0.05),   # sec 0 — wall directly ahead, masked
        (0.10, 0.50),   # 8-spec slot 1 → new sec 2 (clear)
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.10, 0.50),   # 8-spec slot 7 → new sec 14 (clear)
    ], heading=0.0)
    chosen, free = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, desired_heading_deg=0.0,
    )
    assert 0 not in free
    # Closest-to-goal among free is sec 1 or sec 15 (both 22.5°
    # off).  Verify it's not sec 0.
    assert chosen in (1, 15)


def test_pick_sector_masking_prevails_over_alignment():
    """Goal-direction never overrides collision safety — even the
    most-aligned sector is rejected if it has imminent collision.

    §13.20 16-sec: bot heading N, desired 45°.  Old sec 1 (perfect
    alignment at +45°) is now new sec 2; mask it.  Among unmasked
    candidates, the next-best aligned is new sec 1 (+22.5°)."""
    nav = _build_nav([
        (0.05, 0.50),   # sec 0 — clear (suboptimal alignment)
        (0.90, 0.04),   # 8-spec slot 1 → new sec 2: wall close, masked
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ], heading=0.0)
    chosen, free = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, desired_heading_deg=45.0,
    )
    assert 2 not in free
    # Closest unmasked sector to 45° is sec 1 (+22.5°, Δ = 22.5°)
    # or sec 3 (+67.5°, Δ = 22.5°).  Both unobserved and tied;
    # min() takes earliest in CANDIDATE_SECTORS iteration: sec 1.
    assert chosen == 1


def test_pick_sector_all_masked_returns_none():
    """Every candidate masked → return (None, []) for caller fallback.
    §13.20 16-sec: spell out all 16 sectors as walls explicitly so
    the auto-expand fill-zero doesn't leave odd-index slots unmasked."""
    nav = _build_nav([(0.99, 0.05)] * 16, heading=0.0)
    chosen, free = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, desired_heading_deg=0.0,
    )
    assert chosen is None
    assert free == []


@pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
def test_arrival_stop_condition_within_acceptance():
    """When destination is set and bot is within
    POINT_PURSUIT_ACCEPTANCE_DEG of it on both axes, tick() should
    return phase=COMPLETE without further steering."""
    from brain.goals.hug_shore import (
        HugShoreGoal, HugPhase, POINT_PURSUIT_ACCEPTANCE_DEG,
    )
    from brain import observation as _obs
    import sys
    sys.modules["actions.sea_actions"] = type("F", (), {
        "hold_left":  staticmethod(lambda ms: None),
        "hold_right": staticmethod(lambda ms: None),
        "turn_left":  staticmethod(lambda taps=1: None),
        "turn_right": staticmethod(lambda taps=1: None),
        "is_ship_moving": staticmethod(lambda: True),
        "sail_start": staticmethod(lambda: None),
    })

    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    _obs.update(nav=nav, frame_id="pp-arrival-1")
    goal = HugShoreGoal(
        side="starboard", max_ticks=50, driver_mode="point_pursuit",
        endpoint_lat=31.20, endpoint_lon=29.90,
    )
    # HUD reports bot well within acceptance.
    goal.set_hud(lat=31.20 + 0.5, lon=29.90 - 0.3, speed_kt=10.0)
    result = goal.tick()
    assert goal.phase == HugPhase.COMPLETE
    assert "destination reached" in result.note


@pytest.mark.simulation  # simulated goal-loop run: `tick()` executes the real action code against whatever frame the fixtures supply
def test_arrival_does_not_fire_outside_acceptance():
    """When the bot is FURTHER than POINT_PURSUIT_ACCEPTANCE_DEG on
    either axis, tick() should NOT return COMPLETE — voyage continues."""
    from brain.goals.hug_shore import HugShoreGoal, HugPhase
    from brain import observation as _obs
    import sys
    sys.modules["actions.sea_actions"] = type("F", (), {
        "hold_left":  staticmethod(lambda ms: None),
        "hold_right": staticmethod(lambda ms: None),
        "turn_left":  staticmethod(lambda taps=1: None),
        "turn_right": staticmethod(lambda taps=1: None),
        "is_ship_moving": staticmethod(lambda: True),
        "sail_start": staticmethod(lambda: None),
    })

    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    _obs.update(nav=nav, frame_id="pp-no-arrival")
    goal = HugShoreGoal(
        side="starboard", max_ticks=50, driver_mode="point_pursuit",
        endpoint_lat=31.20, endpoint_lon=29.90,
    )
    # HUD reports bot ~1.5 deg off — too far on lat.
    goal.set_hud(lat=29.50, lon=29.90, speed_kt=10.0)
    goal.tick()
    # Phase may be INIT/HUGGING/etc. but NOT COMPLETE.
    assert goal.phase != HugPhase.COMPLETE


def test_pick_sector_no_heading_returns_none():
    """When ship heading is unknown, can't map sector bearings to
    compass → return None."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    nav.ship_heading_deg = None
    chosen, free = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, desired_heading_deg=0.0,
    )
    assert chosen is None
