"""§13.7 — Tests for the lateral-balance term on sec 0.

The term fixes the channel-follower's missing corridor-centering
signal: when the bot drifts past the calibrated `wall_distance`, sec 0
should pay a proportional penalty so the cost gap to sec 1 shrinks
smoothly with drift instead of waiting for the categorical
`_ideal_sector` flip.

These tests assert the three sub-cases enumerated in §13.7.2:

  1. Shore parallel (at d_target): no penalty on sec 0
  2. Shore curves into bow / tight hug (T < wall_distance): no penalty
  3. Shore curves away (T > wall_distance): proportional penalty

Plus the noise-floor guard: tiny `frac` should NOT trigger the term
even if `nearest_dist` looks like d_target (the 1-pixel-phantom case).
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason='§13.20: 8→16 sector migration TODO. Tests legacy VFH+ / Bug2 code paths that are gated off for point_pursuit driver; need fixtures re-derived for 16-sec grid.')


import pytest

from brain.goals.hug_shore import (
    _score_sectors,
    K_LATERAL,
    K_LATERAL_BARRIER,
    LATERAL_BARRIER_THRESHOLD,
    LATERAL_DEADBAND,
    LATERAL_VALID_FRAC,
    MARGIN_MIN_SCALE,
    MARGIN_SATURATION,
    T_DIST_EMA_ALPHA,
    WALL_DIST_INIT,
)


class _FakeSector:
    __slots__ = ("land_fraction", "nearest_dist", "is_observed", "bearing_deg")
    def __init__(self, frac, dist, bearing):
        self.land_fraction = frac
        self.nearest_dist  = dist
        self.is_observed   = True
        self.bearing_deg   = bearing


class _FakeNav:
    __slots__ = ("sectors", "ship_heading_deg")


def _build_nav(target_frac, target_dist, ahead_clear=True):
    """Build a starboard-hug nav with shore on beam-right (sec 2)
    at the given frac/dist, sky clear everywhere else."""
    sectors = []
    for i in range(8):
        if i == 2:
            sectors.append(_FakeSector(target_frac, target_dist, 90.0))
        elif i == 0 and not ahead_clear:
            sectors.append(_FakeSector(0.3, 0.10, 0.0))
        else:
            sectors.append(_FakeSector(0.0, 1.0, float(i*45)))
    nav = _FakeNav()
    nav.sectors = tuple(sectors)
    nav.ship_heading_deg = 90.0
    return nav


def _sec0_cost(nav, wall_distance=WALL_DIST_INIT):
    costs, _, _, _ = _score_sectors(
        nav, "starboard", wall_distance=wall_distance, signals=None,
    )
    return costs.get(0)


def test_shore_parallel_at_d_target_no_lateral_penalty():
    """Sub-case 1: bot at calibrated hug distance, shore parallel
    on beam.  lateral_drift = 0, no penalty applied to sec 0."""
    nav_at = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT)
    nav_above = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT + 0.001)
    assert _sec0_cost(nav_at) == pytest.approx(_sec0_cost(nav_above), abs=0.01)


def test_tight_hug_below_d_target_no_lateral_penalty():
    """Sub-case 2: bot closer than calibration (T.dist < wall_distance).
    max(0, drift) = 0 — the real-shore obstacle cost + rotation_penalty
    already handle 'too close,' no double-counting from this term."""
    nav_tight = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT - 0.05)
    nav_at    = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT)
    # Both should give the same sec 0 cost from the lateral term (both
    # produce lateral_drift = 0).  Other terms may differ but should
    # not differ BY the lateral contribution.
    diff = abs(_sec0_cost(nav_tight) - _sec0_cost(nav_at))
    assert diff < 0.05    # well below K_LATERAL × any realistic drift


def test_drifting_offshore_penalises_sec0_proportionally():
    """Sub-case 3: bot drifted offshore (T.dist > wall_distance).
    sec 0 cost rises in proportion to drift magnitude."""
    nav_drifted = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT + 0.10)
    nav_drifted_more = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT + 0.20)
    cost_baseline    = _sec0_cost(_build_nav(0.30, WALL_DIST_INIT))
    cost_drifted     = _sec0_cost(nav_drifted)
    cost_drifted_more = _sec0_cost(nav_drifted_more)

    # Penalty climbs with drift
    assert cost_drifted > cost_baseline
    assert cost_drifted_more > cost_drifted
    # Roughly proportional to K_LATERAL × drift
    assert (cost_drifted - cost_baseline) == pytest.approx(
        K_LATERAL * 0.10, abs=0.05,
    )


def test_noise_floor_guard_rejects_phantom_dist():
    """1-pixel-phantom case: frac ≈ 0 with low nearest_dist.  The
    visibility check should refuse to compute lateral_drift from a
    reading that has essentially no land area to back it up."""
    nav_phantom = _build_nav(
        target_frac=0.001,                   # below LATERAL_VALID_FRAC
        target_dist=WALL_DIST_INIT,          # would look like 'perfect hug'
    )
    nav_real_at_target = _build_nav(
        target_frac=0.30, target_dist=WALL_DIST_INIT,
    )
    # Same target_dist, but the phantom case should produce the same
    # baseline cost (lateral_drift = 0 because frac too low) — and
    # crucially, the real case at d_target also has lateral_drift = 0
    # so the costs should converge on the no-penalty baseline.
    diff = abs(_sec0_cost(nav_phantom) - _sec0_cost(nav_real_at_target))
    assert diff < 0.1


def test_no_dist_reading_no_lateral_penalty():
    """When target sector has no nearest_dist (None), the term is
    skipped — same as today's behaviour when shore is invisible."""
    sectors = [_FakeSector(0.0, None, float(i*45)) for i in range(8)]
    nav = _FakeNav()
    nav.sectors = tuple(sectors)
    nav.ship_heading_deg = 90.0
    # Should not raise, and sec 0's cost should be finite.
    cost = _sec0_cost(nav)
    assert cost is not None
    assert cost < 5.0    # sanity: no runaway value


def test_lateral_deadband_ignores_small_drifts():
    """Fix A (Cairo-Anatolia t=160-163 oscillation): small natural
    fluctuations around d_target shouldn't fire the lateral term.
    A drift smaller than LATERAL_DEADBAND must produce zero penalty
    so the bot doesn't flip to turn-target every time T.nearest_dist
    wobbles by 1-2%."""
    # Drift just below the deadband — should be ignored.
    just_under = LATERAL_DEADBAND - 0.005
    nav_just_under = _build_nav(
        target_frac=0.30, target_dist=WALL_DIST_INIT + just_under,
    )
    # Drift well past the deadband — should fire.
    well_past = LATERAL_DEADBAND + 0.10
    nav_well_past = _build_nav(
        target_frac=0.30, target_dist=WALL_DIST_INIT + well_past,
    )
    nav_at = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT)
    cost_at         = _sec0_cost(nav_at)
    cost_just_under = _sec0_cost(nav_just_under)
    cost_well_past  = _sec0_cost(nav_well_past)
    # Just-under drift produces no extra cost on sec 0 (the deadband
    # zeros the lateral term).
    assert abs(cost_just_under - cost_at) < 0.02
    # Well-past drift produces at least the lateral lift — actual cost
    # difference includes both the lateral term and the categorical
    # _ideal_sector flip (sec 2 frac drops past corridor threshold), so
    # we only assert a lower bound.
    min_lateral_lift = K_LATERAL * (well_past - LATERAL_DEADBAND)
    assert (cost_well_past - cost_at) >= min_lateral_lift * 0.9


def test_barrier_fires_beyond_threshold():
    """§13.13 — quadratic barrier kicks in when drift exceeds
    LATERAL_BARRIER_THRESHOLD.  Below threshold: linear only.
    Above: linear + K_BARRIER × (excess)²."""
    # Build nav with a target dist that produces a known drift.
    target_dist_at = WALL_DIST_INIT + LATERAL_DEADBAND + LATERAL_BARRIER_THRESHOLD
    # Just below barrier threshold — barrier should be ~0.
    nav_below = _build_nav(target_frac=0.30, target_dist=target_dist_at - 0.001)
    # Well above — barrier should dominate.
    nav_above = _build_nav(target_frac=0.30, target_dist=target_dist_at + 0.10)

    cost_below = _sec0_cost(nav_below)
    cost_above = _sec0_cost(nav_above)

    # Above-threshold cost should exceed below-threshold by at least the
    # linear contribution of the extra 0.10 drift, plus the quadratic
    # barrier contribution (≈ 20 × 0.10² = 0.2).
    linear_extra = K_LATERAL * 0.10
    barrier_extra = K_LATERAL_BARRIER * 0.10 ** 2
    expected_increase = linear_extra + barrier_extra
    actual_increase = cost_above - cost_below
    assert actual_increase >= expected_increase * 0.9


def test_smoothed_t_dist_rejects_single_tick_spike():
    """§13.13 — passing t_dist_smoothed to _score_sectors makes the
    lateral cost use the smoothed value, not the raw nav reading.
    This is what would have prevented the t=20 perception spike
    (port-marker leakage) from collapsing the lateral signal.
    """
    from brain.goals.hug_shore import _score_sectors
    # Build nav with a NOISY raw reading (very close — would imply low drift)
    nav_noisy = _build_nav(target_frac=0.30, target_dist=0.22)
    # But pass a smoothed value reflecting REAL drift (much further)
    smoothed = 0.40

    costs_raw, _, _, _ = _score_sectors(
        nav_noisy, "starboard", WALL_DIST_INIT, signals=None,
    )
    costs_smoothed, _, _, _ = _score_sectors(
        nav_noisy, "starboard", WALL_DIST_INIT, signals=None,
        t_dist_smoothed=smoothed,
    )
    # Sec 0's cost should be substantially higher when we use the
    # smoothed value (reflecting true drift) rather than the spiky raw
    # reading.
    assert costs_smoothed[0] > costs_raw[0] + 0.1


def test_borderline_frac_does_not_fire_lateral_barrier():
    """t=43 of hug_debug_20260531_150415: bot in open water with target
    sector frac=0.07 (barely above LATERAL_VALID_FRAC noise floor) and
    dist=0.39 (far past wall_distance).  Previously the barrier fired
    on this noise-floor reading and triggered a spurious +90° turn.
    Post-fix the lateral_drift is gated at SHORE_VISIBLE_FRAC=0.15, so
    a 0.07 frac reading produces zero drift and zero penalty on sec0.
    """
    # Borderline frac (between LATERAL_VALID_FRAC and SHORE_VISIBLE_FRAC),
    # large dist that WOULD trip the barrier if drift fired.
    nav_borderline = _build_nav(target_frac=0.07, target_dist=0.39)
    # Same dist but with clearly-visible shore — barrier SHOULD fire.
    nav_real_drift = _build_nav(target_frac=0.30, target_dist=0.39)
    # Stable-hug reference.
    nav_at = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT)

    cost_borderline = _sec0_cost(nav_borderline)
    cost_real       = _sec0_cost(nav_real_drift)
    cost_at         = _sec0_cost(nav_at)

    # Borderline frac → no extra drift cost over the stable baseline.
    assert abs(cost_borderline - cost_at) < 0.05, (
        f"Borderline frac sec0 cost {cost_borderline:.3f} should match "
        f"stable-hug baseline {cost_at:.3f}; otherwise the noise floor "
        f"would fire spurious turns."
    )
    # Real shore at the same dist → barrier fires as expected.
    assert cost_real > cost_borderline + 0.1


def test_lateral_only_applies_to_sec0():
    """The lateral-balance term lives on sec 0 only — sec 1 (turn-
    target) and sec 7 (turn-away) get their behaviour by becoming
    relatively cheaper/more expensive as sec 0 changes, not by being
    directly modified.  This locks in the §13.7 design."""
    drift = 0.15
    nav_drifted = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT + drift)
    nav_at      = _build_nav(target_frac=0.30, target_dist=WALL_DIST_INIT)
    costs_drifted, _, _, _ = _score_sectors(
        nav_drifted, "starboard", wall_distance=WALL_DIST_INIT, signals=None,
    )
    costs_at, _, _, _ = _score_sectors(
        nav_at, "starboard", wall_distance=WALL_DIST_INIT, signals=None,
    )
    # Sec 0 should have shifted by ~K_LATERAL × drift.
    sec0_shift = costs_drifted[0] - costs_at[0]
    assert sec0_shift == pytest.approx(K_LATERAL * drift, abs=0.1)
    # Sec 7 should NOT have shifted by anything close to that (the
    # lateral term doesn't touch it).
    sec7_shift = costs_drifted[7] - costs_at[7]
    assert abs(sec7_shift) < 0.1
