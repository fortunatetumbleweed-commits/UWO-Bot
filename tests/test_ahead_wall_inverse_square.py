"""§13.8 — Tests for the inverse-square ahead-wall cost.

The standard `_obstacle_cost` saturates at 1.0, which let sec 0 stay
cheapest even with a wall directly ahead (other sectors could exceed
1.0 via virtual wall + rotation penalty + ideal distance).  Live
collision at hug_debug_20260531_072226 t=1: bot held at frac=0.83
dist=0.10 because every turn cost > 0.83 = its capped obstacle cost.

`_ahead_obstacle_cost` uses inverse-square scaling on sec 0 only,
gated on frac > AHEAD_WALL_GATE so noise pixels don't trigger it.

These tests verify:
  1. Wall directly ahead (high frac, close dist) → cost grows past 1.0
  2. Cost grows quadratically as distance shrinks (1/r² behaviour)
  3. Frac gate prevents noise-pixel cases from triggering the formula
  4. Far ahead-shore (dist > OBSTACLE_RANGE) falls through to the
     original linear cost
  5. The cost actually flips the policy's pick when wall is ahead
     (regression of the live t=1 collision)
"""
from __future__ import annotations

import pytest

from brain.goals.hug_shore import (
    _ahead_obstacle_cost,
    _obstacle_cost,
    _score_sectors,
    AHEAD_WALL_GATE,
    OBSTACLE_MIN_DIST,
    OBSTACLE_RANGE,
    WALL_DIST_INIT,
)


class _FakeSec:
    __slots__ = ("land_fraction", "nearest_dist", "is_observed", "bearing_deg")
    def __init__(self, frac, dist, bearing=0.0, observed=True):
        self.land_fraction = frac
        self.nearest_dist  = dist
        self.is_observed   = observed
        self.bearing_deg   = bearing


class _FakeNav:
    __slots__ = ("sectors", "ship_heading_deg")


def test_wall_directly_ahead_exceeds_unit_cap():
    """Live collision t=1: frac=0.83 dist=0.10.  Old cost capped at
    0.83; new formula must exceed 2.0 so it dominates the turn
    sectors' typical 1.0-1.5 costs."""
    sec = _FakeSec(frac=0.83, dist=0.10)
    cost = _ahead_obstacle_cost(sec)
    assert cost > 2.0, (
        f"Wall directly ahead should be very expensive (>2.0), got {cost:.2f}"
    )


def test_inverse_square_scaling():
    """Halving the distance should ~quadruple the cost (1/r²)."""
    # Same frac, two distances both < OBSTACLE_RANGE.
    far  = _FakeSec(frac=0.80, dist=0.20)
    near = _FakeSec(frac=0.80, dist=0.10)
    cost_far  = _ahead_obstacle_cost(far)
    cost_near = _ahead_obstacle_cost(near)
    ratio = cost_near / cost_far
    assert 3.5 <= ratio <= 4.5, (
        f"Halving distance should ~quadruple cost; got ratio {ratio:.2f}"
    )


def test_frac_gate_blocks_noise_pixels():
    """Noise-pixel case: frac=0.01, dist=0.05.  Without the gate,
    inverse-square would yield 0.01 × 36 = 0.36 cost.  With the gate,
    it falls through to the original linear formula instead."""
    noise = _FakeSec(frac=0.01, dist=0.05)
    new_cost = _ahead_obstacle_cost(noise)
    old_cost = _obstacle_cost(noise)
    assert new_cost == pytest.approx(old_cost, abs=0.01)


def test_just_above_gate_does_trigger():
    """frac just above AHEAD_WALL_GATE should trigger the new formula."""
    sec_above = _FakeSec(frac=AHEAD_WALL_GATE + 0.01, dist=0.05)
    sec_below = _FakeSec(frac=AHEAD_WALL_GATE - 0.01, dist=0.05)
    cost_above = _ahead_obstacle_cost(sec_above)
    cost_below = _ahead_obstacle_cost(sec_below)
    # Above-gate uses inverse-square; below-gate uses old linear.
    # Above-gate cost should be substantially higher despite similar frac.
    assert cost_above > cost_below * 2


def test_far_shore_falls_through_to_linear():
    """When dist >= OBSTACLE_RANGE the formula falls through to
    `_obstacle_cost` — far obstacles get linear treatment."""
    sec = _FakeSec(frac=0.5, dist=OBSTACLE_RANGE + 0.05)
    new_cost = _ahead_obstacle_cost(sec)
    old_cost = _obstacle_cost(sec)
    assert new_cost == pytest.approx(old_cost, abs=0.01)


def test_min_dist_floor_prevents_division_blowup():
    """At extreme proximity (dist → 0), the floor caps the cost
    rather than letting it explode to infinity."""
    extreme = _FakeSec(frac=0.90, dist=0.001)
    cost = _ahead_obstacle_cost(extreme)
    # With OBSTACLE_MIN_DIST clamp at 0.03 and frac=0.90:
    # 0.90 × (OBSTACLE_RANGE/0.03)² is large but finite.
    assert cost > 50.0   # still very expensive — bot must avoid
    assert cost < 1e6    # but not literally infinite


def test_pinched_passage_escalates_peel_to_beam_opposite():
    """§13.9 — when wall-follow override fires AND bow-opposite has
    obstacle too (pinched passage), escalate from bow-opposite (45°
    peel) to beam-opposite (90° peel) so both obstacles end up on
    the hug side after the turn.

    Reproduces live hug_debug_20260531_075222 t=153: bot peeled 45°
    left and sailed into the island that was at sec 7 frac=0.12
    dist=0.22.  After fix, _ideal_sector returns sec 6 (90° peel)."""
    from brain.goals.hug_shore import (
        _ideal_sector, _AHEAD_OPPOSITE_SECTOR, _BEAM_OPPOSITE_SECTOR,
    )

    class _Sec:
        __slots__ = ('land_fraction', 'nearest_dist', 'is_observed', 'bearing_deg')
        def __init__(self, frac, dist, bearing):
            self.land_fraction = frac; self.nearest_dist = dist
            self.is_observed = True; self.bearing_deg = bearing

    class _Nav:
        __slots__ = ('sectors', 'ship_heading_deg')

    # Reconstruct live t=153 sectors exactly.
    nav = _Nav()
    nav.sectors = (
        _Sec(0.18, 0.21, 0.0),     # 0 — ahead, modest
        _Sec(0.44, 0.07, 45.0),    # 1 — bow-right, close
        _Sec(0.71, 0.06, 90.0),    # 2 — beam-right, very close
        _Sec(0.46, 0.11, 135.0),   # 3 — astern-right
        _Sec(0.01, 0.50, 180.0),   # 4 — back
        _Sec(0.00, None, 225.0),   # 5 — astern-left, empty
        _Sec(0.00, None, 270.0),   # 6 — beam-left, empty (peel target)
        _Sec(0.12, 0.22, 315.0),   # 7 — bow-left, ISLAND
    )
    nav.ship_heading_deg = 305.4

    ideal = _ideal_sector(nav, side="starboard",
                          wall_distance=0.17, signals=None)
    assert ideal == _BEAM_OPPOSITE_SECTOR["starboard"], (
        f"Pinched passage should escalate to beam-opposite (sec "
        f"{_BEAM_OPPOSITE_SECTOR['starboard']}); got sec {ideal}.  "
        f"Without §13.9, the wall-follow override would return "
        f"{_AHEAD_OPPOSITE_SECTOR['starboard']} (45° peel) and the bot "
        f"would sail straight into the island."
    )


def test_escalation_does_not_fire_when_bow_opposite_clear():
    """§13.9 escalation only kicks in when bow-opposite has obstacle.
    When bow-opposite is clear (zero frac), the live t=153 sector
    layout should NOT escalate to beam-opposite — verifying the
    pinch-detection gate is conditional, not blanket."""
    from brain.goals.hug_shore import (
        _ideal_sector, _BEAM_OPPOSITE_SECTOR,
    )

    class _Sec:
        __slots__ = ('land_fraction', 'nearest_dist', 'is_observed', 'bearing_deg')
        def __init__(self, frac, dist, bearing):
            self.land_fraction = frac; self.nearest_dist = dist
            self.is_observed = True; self.bearing_deg = bearing

    class _Nav:
        __slots__ = ('sectors', 'ship_heading_deg')

    # Start from the t=153 pinch and clear out the bow-opposite sector
    # (no island).  Now escalation should NOT fire.
    nav = _Nav()
    nav.sectors = (
        _Sec(0.18, 0.21, 0.0),     # 0 — ahead, modest
        _Sec(0.44, 0.07, 45.0),    # 1 — bow-right, close
        _Sec(0.71, 0.06, 90.0),    # 2 — beam-right, very close
        _Sec(0.46, 0.11, 135.0),
        _Sec(0.01, 0.50, 180.0),
        _Sec(0.00, None, 225.0),
        _Sec(0.00, None, 270.0),
        _Sec(0.00, None, 315.0),   # 7 — bow-left CLEAR (was 0.12/0.22 island)
    )
    nav.ship_heading_deg = 305.4

    ideal = _ideal_sector(nav, side="starboard",
                          wall_distance=0.17, signals=None)
    assert ideal != _BEAM_OPPOSITE_SECTOR["starboard"], (
        f"With clear bow-opposite, escalation to beam-opposite should "
        f"NOT fire; got sec {ideal}"
    )


def test_live_t1_collision_regression():
    """End-to-end policy check: at the live t=1 collision conditions
    (sec 0 frac=0.83 dist=0.18, other sectors at typical hug shapes),
    the policy must NOT pick sec 0 (hold straight into wall)."""
    # Approximate the t=1 sector configuration.
    sectors = []
    for i in range(8):
        if i == 0:
            sectors.append(_FakeSec(0.83, 0.18, 0.0))     # wall ahead
        elif i == 1:
            sectors.append(_FakeSec(0.46, 0.15, 45.0))    # bow-right
        elif i == 2:
            sectors.append(_FakeSec(0.06, 0.30, 90.0))    # beam-right
        elif i == 3:
            sectors.append(_FakeSec(0.83, 0.15, 135.0))   # astern-right
        elif i == 7:
            sectors.append(_FakeSec(0.54, 0.20, -45.0))   # bow-left
        else:
            sectors.append(_FakeSec(0.0, 1.0, float(i*45)))
    nav = _FakeNav()
    nav.sectors = tuple(sectors)
    nav.ship_heading_deg = 282.0

    costs, ideal, _, _ = _score_sectors(
        nav, side="starboard", wall_distance=WALL_DIST_INIT, signals=None,
    )
    best_idx = min(costs, key=costs.get)
    assert best_idx != 0, (
        f"With a wall directly ahead, policy should not pick hold; "
        f"got best_idx={best_idx}.  costs={costs}"
    )
