"""Supply-reserve + voyage-length solver — deterministic math for supply-aware sailing.

The safety-critical numbers that must never be wrong (never strand the fleet): how much
supply a voyage/leg needs, and whether the fleet has enough. Pure arithmetic, no game/UI.

Model (observed 2026-08-14): the fleet carries WATER and FOOD as two separate cargo
resources, each consumed per game-day. Baseline: 329 water + 329 food lasts 12 days →
~27.4 of EACH per day. Days-of-supply is limited by whichever runs out first
(min(water, food)). Time scale is fast — see GAME_DAY_SECONDS below — so supply is checked
at a low frequency (days-based), not per tick.

Two sailing modes need different checks (see route-vs-free-sail branch):
  • ROUTE active: the game auto-resupplies at each PORT waypoint, so the fleet only has to
    survive the LONGEST inter-resupply leg — supply < total voyage is fine. Reserve =
    longest leg (+ margin). One pre-departure assertion, no mid-voyage management.
  • FREE-SAIL: no auto-resupply → need enough for the voyage to the destination (or to a
    planned resupply stop), else resupply at the nearest port en route.

The same "supply for the longest leg" number is also the floor the jettison planner must
preserve when dumping excess supply for cargo.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

# Per-resource consumption: 329 units lasts 12 days → 329/12 per day, for EACH of water/food.
PER_DAY_EACH: float = 329.0 / 12.0  # ≈ 27.42 units/day, per resource

# Default safety margins (game day estimates are conservative, but keep a cushion).
ROUTE_MARGIN_DAYS: float = 0.5      # route legs: small cushion over the longest leg
FREE_SAIL_MARGIN_DAYS: float = 1.0  # free-sail: larger cushion (no auto-resupply)


@dataclass(frozen=True)
class Supply:
    """Current supply aboard, in cargo units, per resource."""
    water: int
    food: int


def days_of_supply(supply: Supply, per_day_each: float = PER_DAY_EACH) -> float:
    """How many game-days the fleet can sail before EITHER resource runs out.

    Limited by whichever runs out first (both water and food are required)."""
    if per_day_each <= 0:
        return math.inf
    return min(supply.water, supply.food) / per_day_each


def supply_needed_each(days: float, per_day_each: float = PER_DAY_EACH) -> int:
    """Units of EACH resource (water and food) needed to sail `days` game-days (rounded up)."""
    return math.ceil(max(0.0, days) * per_day_each)


def route_reserve_each(
    leg_days: Sequence[float],
    margin_days: float = ROUTE_MARGIN_DAYS,
    per_day_each: float = PER_DAY_EACH,
) -> int:
    """Supply reserve (per resource) for a ROUTE = the LONGEST inter-resupply leg + margin.

    `leg_days` are the per-leg day estimates read off the route's waypoint flags. The route
    auto-resupplies at port waypoints, so only the worst single leg matters — NOT the sum.
    This same number is the floor the jettison planner must keep when dumping excess supply.
    """
    if not leg_days:
        return 0
    return supply_needed_each(max(leg_days) + margin_days, per_day_each)


def route_supply_ok(
    supply: Supply,
    leg_days: Sequence[float],
    margin_days: float = ROUTE_MARGIN_DAYS,
    per_day_each: float = PER_DAY_EACH,
) -> bool:
    """ROUTE pre-flight guard: enough supply for the LONGEST leg (not the whole voyage).

    Total supply < total voyage length is FINE — the route resupplies at port waypoints.
    Requires BOTH water and food to cover the longest leg + margin.
    """
    need = route_reserve_each(leg_days, margin_days, per_day_each)
    return supply.water >= need and supply.food >= need


def free_sail_supply_ok(
    supply: Supply,
    voyage_days: float,
    margin_days: float = FREE_SAIL_MARGIN_DAYS,
    per_day_each: float = PER_DAY_EACH,
) -> bool:
    """FREE-SAIL guard: enough supply to reach the destination (+margin), BOTH resources.

    No auto-resupply, so the whole voyage must be covered (or a resupply stop planned)."""
    return days_of_supply(supply, per_day_each) >= voyage_days + margin_days


def voyage_days_from_distance(distance: float, catalogue_units_per_day: float) -> float:
    """Estimate free-sail voyage length in game-days from a catalogue-space distance.

    `catalogue_units_per_day` is the fleet's sail speed in catalogue units per game-day —
    a calibration constant that depends on the ship + wind. TODO: calibrate live (track
    catalogue displacement over elapsed game-days on a known leg). Until then callers pass
    a measured value; for ROUTE sailing this isn't needed at all (use the route's own
    per-leg day flags via route_reserve_each).
    """
    if catalogue_units_per_day <= 0:
        return math.inf
    return distance / catalogue_units_per_day


# ── Live supply monitoring ────────────────────────────────────────────────────
# The sea HUD shows DAYS of supply left and it drops slowly, so supply is watched by
# periodic re-check rather than every tick.  Water and food are consumed at the SAME
# rate, so the per-day consumption can be calibrated live from (amount held ÷ days
# shown) — see `per_day_from_reading`, which beats the hardcoded PER_DAY_EACH baseline.
#
# TIME SCALE: a game day is **under 2 real minutes, ~1.5** (user 2026-08-20), which
# matches the independent observation in actions/route_execution.py ("~2 real-min per
# game-day, 12-day route ≈ 25 min").  We take the SHORT end: under-estimating the day
# makes every checkback fire early, which is the safe direction.
#
# `supply_checkback_seconds` is what drives the live mid-voyage watch: both sail loops
# (actions.task_runner.run_sail_to and brain.goals.sail_to.drive_sail_to) read the HUD,
# then schedule the next read from it via task_runner._next_supply_check_s.  The old fixed
# 180s poll — and the "1 game day ≈ 12 real min" figure it rested on — are gone.

GAME_DAY_SECONDS: float = 90.0          # ~1.5 real minutes per game day
VILLAGE_LEG_RESERVE_DAYS: float = 7.0   # villages cannot resupply — carry the round trip
ROUTE_LEG_DAYS: float = 6.0             # user plans routes so no leg exceeds 6 days


def supply_checkback_seconds(days_left: float, margin_days: float = 1.0,
                             min_seconds: float = 60.0) -> float:
    """How long to wait before re-reading supply, given the days currently shown.

    Waits out the banked supply MINUS a margin, so the fleet is re-read while it still
    has supply in hand — never after it would already have run dry.  With 5 days shown:
    (5 − 1) × 90s = 6 real minutes, matching the user's "check back after 6 minutes".
    Floored at `min_seconds` so a nearly-dry fleet doesn't busy-poll."""
    return max(min_seconds, (days_left - margin_days) * GAME_DAY_SECONDS)


def per_day_from_reading(amount_held: int, days_shown: float) -> Optional[float]:
    """Calibrate per-day consumption from one live reading (amount ÷ days).

    Water and food burn at the same rate, so either resource calibrates both.
    Returns None when the reading can't produce a rate."""
    if not amount_held or not days_shown or days_shown <= 0:
        return None
    return float(amount_held) / float(days_shown)
