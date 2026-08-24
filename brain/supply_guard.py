"""Route-vs-free-sail supply branch — the pre-departure / in-flight supply decision.

Sits between the readers (vision.hud_readers.read_supply) and the solver
(brain.supply_planner) and turns "how much supply do I have + where am I going" into
ONE decision the sail executor acts on. The branch is load-bearing:

  • ROUTE mode: the game auto-resupplies at each PORT waypoint, so we only need to clear
    the LONGEST inter-resupply leg. Supply below the *total* voyage is fine.
  • FREE-SAIL mode: no auto-resupply, so we need enough for the whole voyage (or to a
    planned resupply stop).

Per the "escalate, don't absorb" rule: when supply can't be read, we do NOT assume it's
fine (could strand the fleet) nor assume it's empty — we return UNKNOWN so the caller
decides (re-read, resupply defensively, or escalate). Requires BOTH water and food to be
readable to give a confident PROCEED/RESUPPLY.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from brain.supply_planner import (
    Supply, route_supply_ok, free_sail_supply_ok,
    ROUTE_MARGIN_DAYS, FREE_SAIL_MARGIN_DAYS, PER_DAY_EACH,
)


class SailMode(Enum):
    ROUTE = "route"          # following a saved route with port waypoints (auto-resupply)
    FREE_SAIL = "free_sail"  # steering to a destination directly (no auto-resupply)


class SupplyVerdict(Enum):
    PROCEED = "proceed"              # enough supply for this mode → sail
    RESUPPLY_FIRST = "resupply"     # top up at a port before committing
    UNKNOWN = "unknown"             # supply not fully readable → caller must decide


@dataclass(frozen=True)
class SupplyDecision:
    verdict: SupplyVerdict
    reason: str


def decide_supply(
    supply: Optional[Supply],
    mode: SailMode,
    *,
    leg_days: Optional[Sequence[float]] = None,
    voyage_days: Optional[float] = None,
    route_margin_days: float = ROUTE_MARGIN_DAYS,
    free_sail_margin_days: float = FREE_SAIL_MARGIN_DAYS,
    per_day_each: float = PER_DAY_EACH,
) -> SupplyDecision:
    """Decide whether to sail, resupply first, or escalate.

    ROUTE mode needs `leg_days` (per-leg day estimates from the route's waypoint flags);
    FREE_SAIL needs `voyage_days` (estimated days to the destination). Returns UNKNOWN if
    supply is unreadable (either resource None) so the caller can re-read or resupply
    defensively rather than gambling the fleet on an assumed value.
    """
    if supply is None or supply.water is None or supply.food is None:
        return SupplyDecision(SupplyVerdict.UNKNOWN, "supply not fully readable (water/food)")

    if mode is SailMode.ROUTE:
        if not leg_days:
            return SupplyDecision(SupplyVerdict.UNKNOWN, "route mode but no leg days provided")
        ok = route_supply_ok(supply, leg_days, route_margin_days, per_day_each)
        longest = max(leg_days)
        if ok:
            return SupplyDecision(
                SupplyVerdict.PROCEED,
                f"route: supply covers longest leg {longest:g}d (+{route_margin_days:g}d)",
            )
        return SupplyDecision(
            SupplyVerdict.RESUPPLY_FIRST,
            f"route: supply short of longest leg {longest:g}d (+{route_margin_days:g}d)",
        )

    # FREE_SAIL
    if voyage_days is None:
        return SupplyDecision(SupplyVerdict.UNKNOWN, "free-sail mode but no voyage days provided")
    ok = free_sail_supply_ok(supply, voyage_days, free_sail_margin_days, per_day_each)
    if ok:
        return SupplyDecision(
            SupplyVerdict.PROCEED,
            f"free-sail: supply covers voyage {voyage_days:g}d (+{free_sail_margin_days:g}d)",
        )
    return SupplyDecision(
        SupplyVerdict.RESUPPLY_FIRST,
        f"free-sail: supply short of voyage {voyage_days:g}d (+{free_sail_margin_days:g}d)",
    )
