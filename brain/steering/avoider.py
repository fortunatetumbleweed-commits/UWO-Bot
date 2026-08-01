"""CollisionAvoider — the §13.18 seam.

Phase 1 of the steering refactor (see docs/steering_architecture.md).
Extracts the §13.17 picker (`_point_pursuit_pick_sector` in
`brain/goals/hug_shore.py`) into a configurable Protocol-backed
Avoider class.

Phase 1 shipped behaviour-identical to today's picker via explicit
phase-1 configs in the tests.  Phase 2 bumps `VFHPlusConfig` defaults
to the canonical (μ1=5, μ2=2, μ3=0) from Ulrich & Borenstein 1998 —
the μ2 inertia term is what the paper credits with eliminating
narrow-corridor oscillation, and `HugShoreGoal` picks up the new
defaults automatically.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Protocol


# Sector relative-bearing convention.  Bow = 0; +ve clockwise (stbd).
# 16-sector grid (§13.20): forward arc covers indices 12-15 + 0-4
# (9 sectors at 22.5° granularity).  Astern sectors (5-11) are admitted
# as candidates by hug_shore §13.26 when the waypoint sits more than
# 90° from the bow — needed for genuine U-turns.
_SECTOR_REL_ANGLE: dict[int, float] = {
    0:    0.0,    # ahead
    1:   22.5,    # +22.5° stbd of bow
    2:   45.0,    # bow-starboard
    3:   67.5,    # +67.5° stbd
    4:   90.0,    # starboard beam
    5:  112.5,    # astern arc (stbd half)
    6:  135.0,
    7:  157.5,
    8:  180.0,    # dead astern
    9: -157.5,    # astern arc (port half)
    10: -135.0,
    11: -112.5,
    12: -90.0,    # port beam
    13: -67.5,    # -67.5° port
    14: -45.0,    # bow-port
    15: -22.5,    # -22.5° port of bow
}


@dataclass(frozen=True)
class VFHPlusConfig:
    """Configuration for `VFHPlusAvoider`.

    Defaults follow Ulrich & Borenstein 1998: μ1=5 (target alignment),
    μ2=2 (current-heading inertia), μ3=0 (previous-selection
    smoothness — requires cross-tick state, deferred).  The constraint
    μ1 > μ2 + μ3 still holds; raising μ3 should not raise the sum past
    μ1 or the picker stops chasing the target.

    Phase 1 of the §13.18 refactor used (μ1=1, μ2=0) to reproduce the
    legacy `_point_pursuit_pick_sector` exactly.  Phase 2 (this file)
    moves to canonical defaults; legacy behaviour is still reachable
    by passing `VFHPlusConfig(target_weight=1.0, inertia_weight=0.0)`
    explicitly.
    """
    safety_dist:       float = 0.12
    target_weight:     float = 5.0    # μ1
    inertia_weight:    float = 2.0    # μ2 — canonical (Ulrich-Borenstein
                                       # 1998); BARN Challenge 2022 winners
                                       # ran this ratio.  Eliminates the
                                       # ±45° per-tick flip-flop our
                                       # 8-sector grid is prone to.
    smoothness_weight: float = 0.0    # μ3 — deferred (cross-tick state)
    # §13.22 — Density gate.  At 22.5° wedges (16-sector grid) a sector
    # can have nearest_dist just above safety_dist while its land
    # fraction is ~90%: a thin water lane through dense shore.  At the
    # 8-sector 45° grid the wider wedge captured the worst case, but at
    # 16 sectors a frac-only check is required to keep the picker from
    # committing to those thin-lane sectors.  See `project_density_gate_validated.md`
    # for the hug_debug_20260601_164805 t161-t165 counterfactual.
    #
    # Ceiling tuned 0.5 → 0.6 on 2026-06-02 after live voyages showed
    # the 0.5 ceiling trapping the bot at narrow river passes (Nubia
    # bend in particular).  Forward sectors at typical narrow channels
    # read frac≈0.53 — masking those left the avoider no choice but to
    # turn sideways, kicking off a multi-tick U-turn pattern that
    # erased southward progress.  0.6 admits the typical narrow pass
    # while still catching the t161-class catastrophic picks
    # (frac=0.86-0.97 — well above 0.6).
    land_fraction_ceiling: float = 0.6
    # §13.30 — Graduated density penalty (opt-in via positive weight).
    #
    # When `density_penalty_weight > 0`, sectors in
    # (density_soft_floor, land_fraction_ceiling] are admitted with a
    # quadratic penalty `(frac − soft_floor)² × weight` instead of
    # being hard-masked.  Callers who want graduated behaviour also
    # raise `land_fraction_ceiling` (e.g. to 0.85) so the binary mask
    # only fires on genuinely dense walls and the soft band is wider.
    #
    # Default 0.0 = legacy binary behaviour (preserves the stoprej_dest8
    # 386-tick baseline).  Live-validated at weight=10000.0,
    # ceiling=0.85 on explore_port_20260602_235018 — destination
    # reached but voyage ran 471 ticks (22% slower than baseline) with
    # 8.9% rejection rate (vs 5.4% baseline); kept opt-in pending
    # more A/B data.
    density_soft_floor:     float = 0.6
    density_penalty_weight: float = 0.0
    # §13.25 — Clearance penalty.  Canonical VFH+ uses `nearest_dist`
    # only as a binary safety mask: once a sector is "free" (dist >=
    # safety_dist), it's treated as equally safe regardless of how much
    # margin it has.  In practice this lets the picker commit to a
    # sector that's *just barely* above the safety threshold (dist =
    # safety_dist + ε) when an angularly-worse alternative has 3× the
    # clearance.  Observed at phase_a_dest8_20260602_162537 t163: free
    # sectors were 12 (dist=0.06, at safety boundary) and 1 (dist=0.17,
    # 3× safer); the picker chose 12 because it was angularly closer to
    # the south-facing waypoint, and the bot scraped along the shore.
    #
    # The clearance term penalises sectors in the (safety_dist,
    # clearance_pref_dist) band with a linear ramp: 180° equivalent
    # cost at the safety boundary, 0 at clearance_pref_dist and beyond.
    # Multiplied by `clearance_weight` (default 1.5 — strong enough to
    # flip the t163 pick, weak enough not to dominate clearly-correct
    # angular choices in open water).  Set `clearance_weight=0.0` to
    # restore pure-mask behaviour.
    clearance_weight:     float = 1.5
    clearance_pref_dist:  float = 0.20


@dataclass(frozen=True)
class AvoiderResult:
    """Output of `CollisionAvoider.select()`.

    `chosen_sector` is None when no candidate survived the safety mask
    (caller falls back to escape behaviour).  `diagnostics` is an
    untyped dict for trace logging; shape varies by Avoider.
    """
    chosen_sector:       Optional[int]
    chosen_heading_deg:  Optional[float]
    free_sectors:        tuple[int, ...]
    diagnostics:         dict


class CollisionAvoider(Protocol):
    """Pick a sector that respects collision constraints while pursuing
    the desired heading.

    Pure-ish: no perception calls, no command emission.  Per-instance
    state allowed for cross-tick memory (e.g. previous-selection for
    the μ3 smoothness term).
    """
    def select(
        self,
        nav,
        desired_heading_deg: float,
        candidate_sectors: tuple[int, ...],
        config: VFHPlusConfig,
    ) -> AvoiderResult:
        ...


class VFHPlusAvoider:
    """Canonical VFH+ candidate selection (Ulrich & Borenstein 1998).

    Algorithm:
      1. Mask sectors with `nearest_dist < safety_dist` (imminent
         collision).
      2. Among surviving free sectors, pick the one minimising
            c(σ) = μ1·Δ(σ, desired_heading)
                 + μ2·Δ(σ, current_heading)
                 + μ3·Δ(σ, previous_selection)
         where Δ is absolute angular difference in [0°, 180°].

    With phase-2 defaults (μ1=5, μ2=2, μ3=0) the picker damps toward
    the current heading: a single-tick waypoint flip won't yank the
    bot 90° unless the target term overcomes the inertia term.  Pass
    `VFHPlusConfig(target_weight=1.0, inertia_weight=0.0)` to reach
    the legacy pure-argmin behaviour.
    """

    def __init__(self):
        self._prev_chosen: Optional[int] = None

    def select(
        self,
        nav,
        desired_heading_deg: float,
        candidate_sectors: tuple[int, ...],
        config: VFHPlusConfig,
    ) -> AvoiderResult:
        if nav.ship_heading_deg is None:
            return AvoiderResult(
                chosen_sector=None, chosen_heading_deg=None,
                free_sectors=(), diagnostics={"reason": "no_heading"},
            )

        free = []
        for idx in candidate_sectors:
            if idx >= len(nav.sectors):
                continue
            sec = nav.sectors[idx]
            if (sec.is_observed and sec.nearest_dist is not None
                    and sec.nearest_dist < config.safety_dist):
                continue
            # §13.22 density gate — see VFHPlusConfig.land_fraction_ceiling.
            if (sec.is_observed
                    and sec.land_fraction > config.land_fraction_ceiling):
                continue
            free.append(idx)
        if not free:
            return AvoiderResult(
                chosen_sector=None, chosen_heading_deg=None,
                free_sectors=(),
                diagnostics={
                    "reason": "all_masked",
                    "safety_dist": config.safety_dist,
                    "land_fraction_ceiling": config.land_fraction_ceiling,
                },
            )

        ship_hdg = nav.ship_heading_deg

        def sector_compass(idx: int) -> float:
            return (ship_hdg + _SECTOR_REL_ANGLE.get(idx, 0.0)) % 360.0

        def clearance_penalty(idx: int) -> float:
            """§13.25 — penalty for sectors close to the safety mask.
            Linear ramp: 180 at safety boundary, 0 at clearance_pref."""
            sec = nav.sectors[idx]
            if (sec.nearest_dist is None
                    or sec.nearest_dist >= config.clearance_pref_dist):
                return 0.0
            span = max(
                config.clearance_pref_dist - config.safety_dist, 1e-6)
            proximity = (config.clearance_pref_dist
                         - sec.nearest_dist) / span
            return 180.0 * max(0.0, min(1.0, proximity))

        def density_penalty(idx: int) -> float:
            """§13.30 — quadratic penalty for high land-fraction sectors
            in the soft band (soft_floor, hard_ceiling].  Returns 0 below
            soft_floor.  Sectors above hard_ceiling are mask-excluded
            upstream and never reach this function."""
            sec = nav.sectors[idx]
            if not sec.is_observed:
                return 0.0
            excess = sec.land_fraction - config.density_soft_floor
            if excess <= 0.0:
                return 0.0
            return excess * excess  # caller multiplies by penalty_weight

        def cost(idx: int) -> float:
            c_bear = sector_compass(idx)
            total = config.target_weight * _abs_angle_diff(
                c_bear, desired_heading_deg)
            if config.inertia_weight > 0.0:
                total += config.inertia_weight * _abs_angle_diff(
                    c_bear, ship_hdg)
            if (config.smoothness_weight > 0.0
                    and self._prev_chosen is not None):
                prev_compass = sector_compass(self._prev_chosen)
                total += config.smoothness_weight * _abs_angle_diff(
                    c_bear, prev_compass)
            if config.clearance_weight > 0.0:
                total += config.clearance_weight * clearance_penalty(idx)
            if config.density_penalty_weight > 0.0:
                total += (config.density_penalty_weight
                          * density_penalty(idx))
            return total

        chosen = min(free, key=cost)
        self._prev_chosen = chosen
        return AvoiderResult(
            chosen_sector=chosen,
            chosen_heading_deg=sector_compass(chosen),
            free_sectors=tuple(free),
            diagnostics={
                "weights": (config.target_weight,
                            config.inertia_weight,
                            config.smoothness_weight,
                            config.clearance_weight,
                            config.density_penalty_weight),
                "safety_dist": config.safety_dist,
                "land_fraction_ceiling": config.land_fraction_ceiling,
                "density_soft_floor":   config.density_soft_floor,
                "clearance_pref_dist": config.clearance_pref_dist,
                "cost_breakdown": {i: cost(i) for i in free},
            },
        )


def _abs_angle_diff(a: float, b: float) -> float:
    """Smallest absolute compass difference between two bearings, in
    degrees.  Result is in [0, 180]."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)
