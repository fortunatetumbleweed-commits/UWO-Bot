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


def _sixteen(recorded, sec_cls):
    """Spread an EIGHT-sector reading (45 deg) across the SIXTEEN-sector view (22.5 deg).

    These fixtures predate the change to SECTOR_COUNT=16, and `_score_sectors` indexes 12 and
    14, so an 8-tuple raised IndexError at hug_shore.py:1274. The FEATURE is fine — the live
    view and `ALL_SECTORS` both say 16 — only the captures lagged (fixed 2026-08-31).

    `recorded` maps OLD sector index -> (land_fraction, nearest_dist). Each lands at the same
    bearing, i.e. new index 2i. The odd sectors between were never captured at 45-degree
    resolution and are INTERPOLATED from their neighbours rather than filled with open water:
    clear sea there would hand the policy escape routes the real situation did not offer, and
    these cases are about every turn looking expensive.
    """
    def _lerp(a, b):
        if a[1] is None or b[1] is None:
            dist = a[1] if b[1] is None else b[1]
        else:
            dist = (a[1] + b[1]) / 2.0
        return ((a[0] + b[0]) / 2.0, dist)

    at = {2 * i: v for i, v in recorded.items()}
    out = []
    for i in range(16):
        if i in at:
            frac, dist = at[i]
        else:
            frac, dist = _lerp(at.get((i - 1) % 16, (0.0, 1.0)),
                               at.get((i + 1) % 16, (0.0, 1.0)))
        out.append(sec_cls(frac, dist, float(i * 22.5)))
    return tuple(out)



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
    nav.sectors = _sixteen({
        0: (0.18, 0.21),    # ahead, modest
        1: (0.44, 0.07),    # bow-right, close
        2: (0.71, 0.06),    # beam-right, very close
        3: (0.46, 0.11),    # astern-right
        4: (0.01, 0.50),    # back
        5: (0.00, None),    # astern-left, empty
        6: (0.00, None),    # beam-left, empty (peel target)
        7: (0.12, 0.22),    # bow-left, ISLAND
    }, _Sec)
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
    nav.sectors = _sixteen({
        0: (0.18, 0.21),    # ahead, modest
        1: (0.44, 0.07),    # bow-right, close
        2: (0.71, 0.06),    # beam-right, very close
        3: (0.46, 0.11),
        4: (0.01, 0.50),
        5: (0.00, None),
        6: (0.00, None),
        7: (0.00, None),    # bow-left CLEAR (was 0.12/0.22 island)
    }, _Sec)
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
    # The t=1 configuration, re-expressed for a SIXTEEN-sector view.
    #
    # This fixture was written against the old EIGHT-sector view at 45 degrees. The navigation
    # view now yields SECTOR_COUNT=16 at 22.5 degrees (vision/minimap_navigation_view.py), and
    # `_score_sectors` indexes 12 and 14 — so an 8-tuple raised IndexError at hug_shore.py:1274
    # and these three tests had been failing since the change. The FEATURE is fine: the live
    # view and `ALL_SECTORS` agree at 16. Only this fixture lagged (fixed 2026-08-31).
    #
    # The recorded readings sit at the same BEARINGS, which land on the even sectors (old i ->
    # new 2i). The odd sectors between them were never captured at 45-degree resolution, so
    # they are INTERPOLATED from their neighbours rather than invented as open water — filling
    # them with clear sea would hand the policy escape routes the real situation did not offer,
    # and the point of this case is that every turn looked expensive.
    recorded = {                       # ship-relative bearing -> (land_fraction, nearest_dist)
        0:  (0.83, 0.18),              # wall ahead
        2:  (0.46, 0.15),              # bow-right    (45 deg)
        4:  (0.06, 0.30),              # beam-right   (90 deg)
        6:  (0.83, 0.15),              # astern-right (135 deg)
        14: (0.54, 0.20),              # bow-left     (-45 deg)
    }
    sectors = []
    for i in range(16):
        if i in recorded:
            frac, dist = recorded[i]
        elif i % 2 == 1:               # between two known readings: interpolate
            lo = recorded.get((i - 1) % 16, (0.0, 1.0))
            hi = recorded.get((i + 1) % 16, (0.0, 1.0))
            frac, dist = ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0)
        else:
            frac, dist = (0.0, 1.0)    # even sectors the 8-sector capture showed as clear
        sectors.append(_FakeSec(frac, dist, float(i * 22.5)))
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
