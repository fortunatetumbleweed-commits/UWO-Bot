"""Dump / jettison planner (#21) — what to throw overboard to free cargo space.

When a barter output overflows the hold (the "Insufficient Empty Space" dialog), we
must free `overflow_units` of space. Policy:
  1. Dump the LEAST valuable trade goods first (keep the valuable barter output).
  2. Only then dump EXCESS supply — water/food ABOVE the longest-leg reserve.
  3. NEVER drop water or food below that reserve (the fleet would starve / strand).

If everything dumpable still can't clear the overflow, report the shortfall — the
caller must sacrifice some of the overflow good itself, NOT the supply reserve.

SAFETY: this is the guard behind the Discard dialog, which defaults to ALL. The
executor (#27) must dump exactly the planned quantities, never blind-OK.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Mapping, Tuple


@dataclass
class CargoItem:
    name: str
    qty: int
    unit_value: float = 0.0          # ducat value per unit (supplies ~ low/zero)
    resource: Optional[str] = None    # 'water' | 'food' for supplies; None for a trade good


@dataclass
class DumpAction:
    name: str
    qty: int
    resource: Optional[str] = None


def plan_jettison(overflow_units: int,
                  cargo: Sequence[CargoItem],
                  reserves: Mapping[str, int]) -> Tuple[list, int]:
    """Return (dump_plan, shortfall).

    overflow_units: units of space to free.
    reserves: {'water': min_units, 'food': min_units} to PRESERVE (longest-leg
              reserve, e.g. from brain.supply_planner.route_reserve_each).
    shortfall > 0 ⇒ overflow could not be cleared without touching the reserve
    (or the overflow good itself); caller decides how to sacrifice the good."""
    plan: list = []
    remaining = max(0, int(overflow_units))

    # 1. Trade goods (no reserve) — cheapest first, to preserve valuable cargo.
    goods = sorted((c for c in cargo if c.resource is None),
                   key=lambda c: c.unit_value)
    for g in goods:
        if remaining <= 0:
            break
        take = min(g.qty, remaining)
        if take > 0:
            plan.append(DumpAction(g.name, take, None))
            remaining -= take

    # 2. Excess supply only — never below the reserve for that resource.
    supplies = sorted((c for c in cargo if c.resource in reserves),
                      key=lambda c: c.unit_value)
    for s in supplies:
        if remaining <= 0:
            break
        excess = max(0, s.qty - int(reserves.get(s.resource, 0)))
        take = min(excess, remaining)
        if take > 0:
            plan.append(DumpAction(s.name, take, s.resource))
            remaining -= take

    return plan, remaining


def reserves_from_route(leg_days: Sequence[float], margin_days: float = 0.5) -> dict:
    """Convenience: water/food reserve units for a route's longest leg (unit-based,
    via brain.supply_planner). Both resources get the same reserve."""
    from brain.supply_planner import route_reserve_each
    r = route_reserve_each(leg_days, margin_days)
    return {"water": r, "food": r}
