"""§13.18 — Tests for the CollisionAvoider seam.

Phase 1 (regression block): legacy `_point_pursuit_pick_sector`
behaviour is still reachable by passing an explicit phase-1 config
(μ1=1, μ2=μ3=0).  Phase 2 (canonical defaults): `VFHPlusConfig()`
ships μ1=5, μ2=2, μ3=0 — the Ulrich-Borenstein 1998 ratios that BARN
Challenge 2022 winners ran.  New tests exercise the μ2 damping in
narrow-passage-like scenarios.
"""
from __future__ import annotations

from brain.goals.hug_shore import (
    CANDIDATE_SECTORS,
    POINT_PURSUIT_SAFETY_DIST,
    _point_pursuit_pick_sector,
)
from brain.steering import VFHPlusAvoider, VFHPlusConfig


_SECTOR_BEARINGS = (
    0.0, 22.5, 45.0, 67.5, 90.0, 112.5, 135.0, 157.5,
    180.0, -157.5, -135.0, -112.5, -90.0, -67.5, -45.0, -22.5,
)


class _Sec:
    __slots__ = ("land_fraction", "nearest_dist", "is_observed", "bearing_deg")
    def __init__(self, f, d, b, observed=True):
        self.land_fraction = f
        self.nearest_dist  = d
        self.is_observed   = observed
        self.bearing_deg   = b


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
    secs = []
    for i, (f, d) in enumerate(specs):
        observed = d is not None or f > 0.0
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i], observed=observed))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = heading
    return nav


def _legacy_cfg():
    """Phase-1 config: pure argmin-to-target (μ2 = μ3 = 0).
    Reachable from external callers by passing these weights
    explicitly."""
    return VFHPlusConfig(
        safety_dist=POINT_PURSUIT_SAFETY_DIST,
        target_weight=1.0, inertia_weight=0.0, smoothness_weight=0.0,
    )


def _canonical_cfg():
    """Phase-2 default: canonical Ulrich-Borenstein 1998 (μ1=5, μ2=2)."""
    return VFHPlusConfig(safety_dist=POINT_PURSUIT_SAFETY_DIST)


# ── Phase 1 invariant: avoider w/ legacy config == legacy picker ─────


def test_open_water_north_target_legacy_matches():
    """All sectors clear; desired heading north (0°)."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    legacy, _ = _point_pursuit_pick_sector(nav, CANDIDATE_SECTORS, 0.0)
    res = VFHPlusAvoider().select(nav, 0.0, CANDIDATE_SECTORS, _legacy_cfg())
    assert res.chosen_sector == legacy == 0


def test_skewed_target_legacy_matches():
    """Open water, desired 45° → sec 2 compass-match (§13.20 16-sec)."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    legacy, _ = _point_pursuit_pick_sector(nav, CANDIDATE_SECTORS, 45.0)
    res = VFHPlusAvoider().select(nav, 45.0, CANDIDATE_SECTORS, _legacy_cfg())
    assert res.chosen_sector == legacy == 2


def test_masking_imminent_collision_legacy_matches():
    """Sec 0 has wall at d < safety; both implementations mask it."""
    nav = _build_nav([
        (0.50, 0.05),   # sec 0 — wall, MASKED
        (0.10, 0.50),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.10, 0.50),
    ], heading=0.0)
    legacy, free_legacy = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, 0.0)
    res = VFHPlusAvoider().select(nav, 0.0, CANDIDATE_SECTORS, _legacy_cfg())
    assert res.chosen_sector == legacy
    assert list(res.free_sectors) == free_legacy
    assert 0 not in res.free_sectors


def test_all_candidates_masked_legacy_matches():
    # §13.20: all 16 sectors are walls (auto-expand would only fill the
    # even-index slots; spell them out for "every sector blocked").
    nav = _build_nav([(0.99, 0.05)] * 16, heading=0.0)
    legacy, free_legacy = _point_pursuit_pick_sector(
        nav, CANDIDATE_SECTORS, 0.0)
    res = VFHPlusAvoider().select(nav, 0.0, CANDIDATE_SECTORS, _legacy_cfg())
    assert res.chosen_sector is None
    assert legacy is None
    assert list(res.free_sectors) == free_legacy == []


def test_no_heading_returns_none_legacy_matches():
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    nav.ship_heading_deg = None
    legacy, _ = _point_pursuit_pick_sector(nav, CANDIDATE_SECTORS, 0.0)
    res = VFHPlusAvoider().select(nav, 0.0, CANDIDATE_SECTORS, _legacy_cfg())
    assert res.chosen_sector is None
    assert legacy is None


def test_mask_dominates_alignment_legacy_matches():
    # §13.20: 16-sec grid.  Old sec 1 (+45°) is now sec 2; sec 1 (+22.5°)
    # is the new in-between.  Mask sec 2 → next-best at 22.5° offset
    # is sec 1 (free, by auto-expand).
    nav = _build_nav([
        (0.05, 0.50),   # sec 0 — clear, mediocre alignment
        (0.90, 0.04),   # 8-spec slot 1 → becomes new sec 2 (masked)
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
        (0.00, None),
    ], heading=0.0)
    legacy, _ = _point_pursuit_pick_sector(nav, CANDIDATE_SECTORS, 45.0)
    res = VFHPlusAvoider().select(nav, 45.0, CANDIDATE_SECTORS, _legacy_cfg())
    # 16-sec grid: sec 2 (best alignment) masked → sec 1 (+22.5°) wins.
    assert res.chosen_sector == legacy == 1


# ── Phase 2: canonical defaults still pick reasonably ─────────────────


def test_canonical_defaults_aligned_target():
    """Desired heading aligned with current → sec 0 (no conflict)."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    res = VFHPlusAvoider().select(nav, 0.0, CANDIDATE_SECTORS, _canonical_cfg())
    assert res.chosen_sector == 0


def test_canonical_defaults_skewed_target_still_followed():
    """μ2 > 0 doesn't prevent following a steady-state target shift.
    Desired 45° (NE), hdg 0° (N) — §13.20 16-sec: sec 2 cost =
    5·0 + 2·45 = 90; sec 0 cost = 5·45 + 2·0 = 225.  Sec 2 wins."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    res = VFHPlusAvoider().select(nav, 45.0, CANDIDATE_SECTORS, _canonical_cfg())
    assert res.chosen_sector == 2


def test_canonical_defaults_damp_target_jitter():
    """The narrow-passage case the canonical defaults are designed for.

    §13.20 16-sec grid: smallest non-zero forward sector is at
    ±22.5° (was ±45°).  Desired heading jitters ±15° — inside sec 0's
    bow zone (±11.25° at 16-sec) but past it enough that pure-argmin
    flips to neighbour sectors 1 (+22.5°) and 15 (−22.5°) each tick.

    With μ2=0 (legacy): sec 1 wins on +15° (cost 7.5 vs sec 0's 15),
    sec 15 wins on −15° → oscillation 1↔15.

    With canonical μ2=2: sec 0 cost = 5·15+0 = 75; sec 1 cost =
    5·7.5+2·22.5 = 82.5; sec 0 wins.  Same on −15° (sec 15 cost 82.5
    vs sec 0 75).  Sec 0 holds course → damping fixed."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    avoider_canonical = VFHPlusAvoider()
    avoider_legacy    = VFHPlusAvoider()
    cfg_c = _canonical_cfg()
    cfg_l = _legacy_cfg()
    picks_c = []
    picks_l = []
    for desired in (15.0, 345.0, 15.0, 345.0, 15.0):
        picks_c.append(
            avoider_canonical.select(nav, desired, CANDIDATE_SECTORS, cfg_c)
            .chosen_sector)
        picks_l.append(
            avoider_legacy.select(nav, desired, CANDIDATE_SECTORS, cfg_l)
            .chosen_sector)
    # Canonical holds course on sec 0 the whole time.
    assert picks_c == [0, 0, 0, 0, 0]
    # Legacy flips sec 1 ↔ sec 15 on every tick.
    assert picks_l == [1, 15, 1, 15, 1]


def test_canonical_defaults_commit_to_big_target_change():
    """A 90° target step IS large enough to overcome μ2.  §13.20 16-sec:
    sec 4 (compass 90°) cost = 5·0 + 2·90 = 180.  Sec 0 cost = 5·90 +
    2·0 = 450.  Sec 4 wins — the bot doesn't get stuck holding course
    in the face of a real course change."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    res = VFHPlusAvoider().select(nav, 90.0, CANDIDATE_SECTORS, _canonical_cfg())
    assert res.chosen_sector == 4


def test_canonical_defaults_break_through_inertia_at_22deg():
    """Crossover point check.  §13.20 16-sec: sec 1 is now at +22.5°,
    not +45°.  Sec 0 cost = 5·d; sec 1 cost = 5·|d−22.5| + 2·22.5.
    Sec 1 wins when 5d > 5·(22.5−d) + 45 → 10d > 157.5 → d > 15.75°.
    At desired=15°, sec 0 holds; at desired=17°, sec 1 takes over."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    cfg = _canonical_cfg()
    r15 = VFHPlusAvoider().select(nav, 15.0, CANDIDATE_SECTORS, cfg)
    r17 = VFHPlusAvoider().select(nav, 17.0, CANDIDATE_SECTORS, cfg)
    assert r15.chosen_sector == 0
    assert r17.chosen_sector == 1


# ── μ2 and μ3 mechanics (independent of defaults) ─────────────────────


def test_inertia_pulls_toward_current_heading():
    """§13.20 16-sec: with μ2 = μ1, sec 0/1/2 all tie at cost 45 for
    desired=45° (sec 2 = perfect alignment + 45° heading change; sec 0
    = 45° target + 0 inertia; sec 1 = 22.5° each).  min() picks first
    in CANDIDATE_SECTORS iteration order — sec 0 comes first.

    Pure target (μ2=0): sec 2 wins (perfect alignment)."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    cfg_no_inertia = VFHPlusConfig(target_weight=1.0, inertia_weight=0.0,
                                    smoothness_weight=0.0)
    cfg_balanced   = VFHPlusConfig(target_weight=1.0, inertia_weight=1.0,
                                    smoothness_weight=0.0)
    res_a = VFHPlusAvoider().select(nav, 45.0, CANDIDATE_SECTORS, cfg_no_inertia)
    res_b = VFHPlusAvoider().select(nav, 45.0, CANDIDATE_SECTORS, cfg_balanced)
    assert res_a.chosen_sector == 2
    assert res_b.chosen_sector == 0


def test_smoothness_pulls_toward_prev_selection():
    """μ3 > 0 keeps the bot near its prior pick across ticks.
    Explicit inertia=0 so the test isolates the μ3 effect from μ2.
    §13.20 16-sec: sec 2 (compass 45°) is the perfect target match
    for desired=45° (was sec 1 in 8-sec)."""
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    avoider = VFHPlusAvoider()
    cfg = VFHPlusConfig(target_weight=1.0, inertia_weight=0.0,
                         smoothness_weight=10.0)
    r1 = avoider.select(nav, 45.0, CANDIDATE_SECTORS, cfg)
    assert r1.chosen_sector == 2
    r2 = avoider.select(nav, 0.0, CANDIDATE_SECTORS, cfg)
    assert r2.chosen_sector == 2
    fresh = VFHPlusAvoider().select(
        nav, 0.0, CANDIDATE_SECTORS,
        VFHPlusConfig(target_weight=1.0, inertia_weight=0.0,
                       smoothness_weight=0.0))
    assert fresh.chosen_sector == 0


def test_diagnostics_carry_cost_breakdown():
    nav = _build_nav([(0.0, None)] * 8, heading=0.0)
    res = VFHPlusAvoider().select(
        nav, 45.0, CANDIDATE_SECTORS, _canonical_cfg())
    assert "cost_breakdown" in res.diagnostics
    assert "safety_dist" in res.diagnostics
    assert "weights" in res.diagnostics
    assert set(res.diagnostics["cost_breakdown"].keys()) == set(res.free_sectors)


def test_phase2_default_weights_are_canonical():
    """Lock the canonical (5, 2, 0) ratios + §13.25 clearance default
    in via the constructor so a future drift is loud."""
    cfg = VFHPlusConfig()
    assert cfg.target_weight     == 5.0
    assert cfg.inertia_weight    == 2.0
    assert cfg.smoothness_weight == 0.0
    assert cfg.clearance_weight    == 1.5
    assert cfg.clearance_pref_dist == 0.20


def test_clearance_penalty_flips_t163_pick():
    """§13.25 — Replay phase_a_dest8_20260602_162537 t163.  Two
    sectors survive the safety+density mask:
      - sec 12 (port beam) at dist=0.06 (right at safety boundary)
      - sec 1  (right-fwd) at dist=0.17 (well clear)
    Pure VFH+ picks sec 12 because it's angularly closer to the wp
    bearing 220° (NW vs ENE), even though it's hugging shore.  With
    the clearance term the cost ranking flips and sec 1 wins.
    """
    # 16-sector nav: only sec 12 and sec 1 are passable; everything
    # else is dense land in the (frac > 0.6) ceiling band.
    specs = [(0.95, 0.10)] * 16
    specs[12] = (0.38, 0.06)   # port beam, near safety boundary
    specs[1]  = (0.31, 0.17)   # right-fwd, comfortable clearance
    nav = _build_nav(specs, heading=45.0)
    # Without clearance term — sec 12 wins (replays the bug).
    cfg_no_clearance = VFHPlusConfig(safety_dist=0.06,
                                       clearance_weight=0.0)
    res_off = VFHPlusAvoider().select(nav, 220.0, CANDIDATE_SECTORS,
                                        cfg_no_clearance)
    assert set(res_off.free_sectors) == {12, 1}
    assert res_off.chosen_sector == 12
    # With clearance term (default) — sec 1 wins.
    res_on = VFHPlusAvoider().select(nav, 220.0, CANDIDATE_SECTORS,
                                       VFHPlusConfig(safety_dist=0.06))
    assert set(res_on.free_sectors) == {12, 1}
    assert res_on.chosen_sector == 1


def test_clearance_penalty_zero_when_dist_above_pref():
    """A sector with dist >= clearance_pref_dist contributes zero
    clearance cost — open water shouldn't penalise the picker."""
    # Two free sectors, both comfortably clear.  Pure angular pick.
    specs = [(0.95, 0.10)] * 16
    specs[0] = (0.0, 0.50)   # ahead, far
    specs[1] = (0.0, 0.50)   # right-fwd, far
    nav = _build_nav(specs, heading=0.0)
    # Target points at sec 1 (22.5° right).  No clearance influence
    # should change this.
    res = VFHPlusAvoider().select(nav, 22.5, CANDIDATE_SECTORS,
                                    VFHPlusConfig(safety_dist=0.06))
    assert res.chosen_sector == 1


def test_graduated_density_lets_borderline_left_sector_win_at_y_tip():
    """§13.30 — Replay stoprej_dest8_20260602_221508 t307.
    Bot facing NW (hdg=318°), goal south (wp=166°).  Forward-left
    sectors 13-15 had frac=0.62-0.67 (just above the OLD 0.6 ceiling)
    and were hard-masked, forcing the bot to commit to the LONGER
    rotation (right turn via sec 3).  With the graduated penalty,
    sec 15 (closer to goal direction) wins on cost despite its
    borderline frac, and the bot turns LEFT.
    """
    specs = [
        (0.64, 0.30),  # 0  — bow (NW), borderline
        (0.50, 0.42),  # 1
        (0.28, 0.41),  # 2
        (0.02, 0.44),  # 3  — clean (the OLD pick, longer rotation)
        (0.24, 0.18),  # 4
        (0.86, 0.11),  # 5
        (0.93, 0.11),  # 6
        (0.90, 0.16),  # 7
        (0.71, 0.31),  # 8  — over 0.6 too but astern (not candidate)
        (0.42, 0.13),  # 9
        (0.95, 0.09),  # 10
        (0.96, 0.08),  # 11
        (0.97, 0.07),  # 12 — port beam
        (0.64, 0.07),  # 13 — borderline left, close
        (0.62, 0.08),  # 14 — borderline left, close
        (0.67, 0.11),  # 15 — borderline left  — the NEW pick (shorter rotation)
    ]
    secs = []
    for i, (f, d) in enumerate(specs):
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i]))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = 318.0

    # Graduated density is opt-in: enable it (raised hard ceiling +
    # quadratic penalty in the soft band).
    cfg = VFHPlusConfig(safety_dist=0.06,
                         land_fraction_ceiling=0.85,
                         density_penalty_weight=10000.0)
    res = VFHPlusAvoider().select(nav, 166.0, CANDIDATE_SECTORS, cfg)
    # Sec 14/15 were masked under the old binary gate at ceiling=0.6.
    # With graduated density they're admitted with a small penalty.
    assert any(s in res.free_sectors for s in (13, 14, 15))
    # The picker no longer chooses sec 3 (the OLD forced-right pick
    # that moves the bot away from the goal).  Acceptable picks: any
    # left-of-bow sector (12-15), or bow itself (0) — bow is "hold
    # course while perception catches up," which is the conservative
    # outcome.  The pathological behaviour we're guarding against is
    # turning right when left is the shorter rotation toward goal.
    assert res.chosen_sector not in (1, 2, 3, 4)


def test_clearance_weight_zero_restores_pure_mask_behavior():
    """Setting `clearance_weight=0.0` reverts to canonical VFH+:
    distance is mask-only, all free sectors equally safe."""
    specs = [(0.95, 0.10)] * 16
    specs[12] = (0.38, 0.06)
    specs[1]  = (0.31, 0.17)
    nav = _build_nav(specs, heading=45.0)
    cfg = VFHPlusConfig(safety_dist=0.06, clearance_weight=0.0)
    res = VFHPlusAvoider().select(nav, 220.0, CANDIDATE_SECTORS, cfg)
    # Without clearance, sec 12's better angular alignment wins
    # despite hugging the shore.
    assert res.chosen_sector == 12


def test_phase2_default_density_gates():
    """§13.30 — Default density gate is LEGACY binary (weight=0,
    ceiling=0.6).  Graduated behaviour is opt-in by callers via
    `VFHPlusConfig(land_fraction_ceiling=0.85,
                    density_penalty_weight=10000.0)` (or the equivalent
    `graduated_density: true` flag in an explore-task YAML).
    Keeping the default as legacy preserves the stoprej_dest8
    386-tick baseline; turning on graduated gave 471 ticks in one
    A/B (22% slower, more rejections — pending more data).
    """
    cfg = VFHPlusConfig()
    assert cfg.density_soft_floor      == 0.6
    assert cfg.land_fraction_ceiling   == 0.6     # legacy binary mask
    assert cfg.density_penalty_weight  == 0.0     # off by default


def test_density_gate_masks_thin_water_lane_sector():
    """§13.22 — A sector with nearest_dist just above safety_dist but
    land_fraction well above the ceiling represents a thin water lane
    through dense shore.  The picker must NOT commit to it.

    Replays hug_debug_20260601_164805 t161: bot facing ENE, ideal
    bearing south, sec 2 (bow-stbd) has frac=0.90 dist=0.067 and would
    have been picked by the safety-only mask.  With the density gate
    at 0.5, sec 2 is excluded and the picker falls through to sec 15
    (bow-port, frac=0.15 dist=0.349) — the only true water sector in
    the forward arc.
    """
    # Sector specs ordered 0..15.  16-sec grid, not auto-expanded.
    specs = [
        (0.74, 0.108),   # 0  — bow, dense
        (0.95, 0.094),   # 1  — bow-stbd intermediate, dense
        (0.90, 0.067),   # 2  — bow-stbd, the trap sector
        (0.99, 0.050),   # 3  — close, safety-masked
        (0.97, 0.055),   # 4  — safety-masked
        (0.98, 0.045),   # 5
        (0.99, 0.048),   # 6
        (0.99, 0.067),   # 7
        (0.98, 0.091),   # 8
        (0.90, 0.221),   # 9
        (0.74, 0.329),   # 10
        (0.13, 0.226),   # 11
        (0.72, 0.182),   # 12 — port beam, dense
        (0.74, 0.182),   # 13 — port-aft, dense
        (0.67, 0.206),   # 14 — bow-port intermediate, dense
        (0.15, 0.349),   # 15 — bow-port, the only safe forward water
    ]
    secs = []
    for i, (f, d) in enumerate(specs):
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i]))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = 74.0

    # Narrow-regime safety_dist=0.06 (matches live ConfigSelector); at
    # that threshold sec 2 is NOT safety-masked (dist=0.067) and the
    # density gate is the only thing keeping the picker away from it.
    cfg = VFHPlusConfig(safety_dist=0.06)
    res = VFHPlusAvoider().select(nav, 202.0, CANDIDATE_SECTORS, cfg)
    # Sec 2 must NOT be free under the density gate.
    assert 2 not in res.free_sectors
    # Sec 15 IS free and gets picked.
    assert res.chosen_sector == 15


def test_density_fully_disabled_admits_and_picks_thin_lane():
    """§13.30 — Inverse: with BOTH the hard mask raised to 1.0 AND the
    quadratic penalty zeroed, the picker reverts to legacy behaviour
    and picks the dense sector.  This documents that the density
    mechanism (mask + penalty together) is what keeps the t161 case
    safe — turning off either alone is not enough."""
    specs = [
        (0.74, 0.108), (0.95, 0.094), (0.90, 0.067), (0.99, 0.050),
        (0.97, 0.055), (0.98, 0.045), (0.99, 0.048), (0.99, 0.067),
        (0.98, 0.091), (0.90, 0.221), (0.74, 0.329), (0.13, 0.226),
        (0.72, 0.182), (0.74, 0.182), (0.67, 0.206), (0.15, 0.349),
    ]
    secs = []
    for i, (f, d) in enumerate(specs):
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i]))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = 74.0

    cfg = VFHPlusConfig(safety_dist=0.06,
                         land_fraction_ceiling=1.0,
                         density_penalty_weight=0.0)
    res = VFHPlusAvoider().select(nav, 202.0, CANDIDATE_SECTORS, cfg)
    # Without either guard, sec 2 is admitted and (per t161 trace) chosen.
    assert 2 in res.free_sectors
    assert res.chosen_sector == 2


def test_density_penalty_alone_blocks_thin_lane():
    """§13.30 — Even with the hard mask disabled, the quadratic penalty
    alone is enough to keep the picker away from a frac=0.90 thin lane.
    """
    specs = [
        (0.74, 0.108), (0.95, 0.094), (0.90, 0.067), (0.99, 0.050),
        (0.97, 0.055), (0.98, 0.045), (0.99, 0.048), (0.99, 0.067),
        (0.98, 0.091), (0.90, 0.221), (0.74, 0.329), (0.13, 0.226),
        (0.72, 0.182), (0.74, 0.182), (0.67, 0.206), (0.15, 0.349),
    ]
    secs = []
    for i, (f, d) in enumerate(specs):
        secs.append(_Sec(f, d, _SECTOR_BEARINGS[i]))
    nav = _Nav()
    nav.sectors = tuple(secs)
    nav.ship_heading_deg = 74.0

    # Hard mask off, penalty on — sec 2 admitted but not picked.
    # Graduated density is opt-in (default weight=0); enable it here.
    cfg = VFHPlusConfig(safety_dist=0.06,
                         land_fraction_ceiling=1.0,
                         density_penalty_weight=10000.0)
    res = VFHPlusAvoider().select(nav, 202.0, CANDIDATE_SECTORS, cfg)
    assert 2 in res.free_sectors           # mask off → admitted
    assert res.chosen_sector != 2          # penalty wins
    assert res.chosen_sector == 15         # the genuine water sector
