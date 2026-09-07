"""Material-gathering solver (#20) — which source ports to visit, in what order.

Barter recipes need several materials, each sold at several ports, and some ports
carry more than one needed material. The job: cover all needed materials with a
SHORT sailing route from the current position. This is set-cover (which ports) +
routing (what order) together — the deterministic solver that beat an LLM by ~20%
on this exact task (the LLM ignored the exit-direction gradient, e.g. picking
Carrot@Kerch over the far-closer Montpellier — see
project_llm_vs_solver_routing_2026-08-14).

Heuristic: greedily extend the route with the port maximizing
(newly-covered materials) / (marginal distance from the route's current end) —
which jointly rewards multi-material ports AND geographic proximity. Capacity is
checked (total units vs hold) and flagged; multi-trip splitting is left to the
caller. Pure + testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence


@dataclass
class GatheringPlan:
    route: list = field(default_factory=list)      # ordered ports to visit
    covered: set = field(default_factory=set)      # materials sourced by the route
    unsourced: set = field(default_factory=set)    # materials with no known source port
    total_distance: float = 0.0
    over_capacity: bool = False                    # total units needed exceed the hold
    # Materials whose EVERY known source port is scarce this season. Not a failure to plan —
    # a fact about the world, and the one case where giving up early is the right answer
    # (user: "if all ports have low stock, then just abandon the task as non-profitable for
    # the season"). The caller decides; the solver only reports.
    low_everywhere: set = field(default_factory=set)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


# What a material is worth at a port where it is SCARCE this season, against 1.0 for an
# ordinary shelf. Measured, not chosen: Faro returned ~457 Pig per blue-gem refresh and
# Madeira ~110 Raisin on 2026-09-06, and 110/457 is 0.24.
_LOW_SEASON_WEIGHT = 0.24


def _season(season_fn, port: str, material: str) -> Optional[str]:
    """What the KB remembers about this good's season here. Unknown on any trouble —
    a planner must not fail because a record could not be read."""
    if season_fn is None:
        return None
    try:
        return season_fn(port, material)
    except Exception:                         # noqa: BLE001 — no season is not an error
        return None


def plan_gathering(needed: Sequence[str],
                   material_sources: Mapping[str, Sequence[str]],
                   port_coords: Mapping[str, tuple],
                   start: tuple,
                   quantities: Optional[Mapping[str, int]] = None,
                   cargo_capacity: Optional[int] = None,
                   season_fn: Optional[Callable[[str, str], Optional[str]]] = None) -> GatheringPlan:
    """Plan a gathering route covering `needed` materials from their source ports.

    needed:            materials to gather.
    material_sources:  {material: [ports that sell it]}.
    port_coords:       {port: (x, y)} for the sourceable ports.
    start:             (x, y) current position, or None when it cannot be read — the legs
                       then run unordered (by coverage) rather than the plan failing.
    """
    needed = set(needed)
    # Materials with no known/reachable source can't be covered.
    unsourced = {m for m in needed
                 if not any(p in port_coords for p in material_sources.get(m, ()))}
    uncovered = needed - unsourced

    route: list = []
    covered: set = set()
    total = 0.0
    cur = start
    while uncovered:
        best, best_score, best_new, best_d = None, -1.0, None, 0.0
        for port, coords in port_coords.items():
            if port in route:
                continue
            new = {m for m in uncovered if port in material_sources.get(m, ())}
            if not new:
                continue
            # NO ORIGIN? ORDER BY COVERAGE ALONE. `start` is what makes this a ROUTE rather
            # than a set: without it we cannot say which port is nearer, so we choose the one
            # that covers the most materials and accept the extra sailing.
            #
            # A missing origin is normal, not exceptional: the remote check that reads the
            # recipe leaves the fleet on the WORLD MAP, which paints no port name (user,
            # 2026-08-31: "from port is only for good logging, for sailing it is really not
            # important"). Refusing instead cost two runs on consecutive days.
            #
            # What is NOT allowed is inventing an origin — on 2026-08-21 a made-up one sent
            # the fleet to Atuona at 5,948 instead of Masulipatnam at 294. Dropping the
            # distance term is not the same as guessing at it.
            d = _dist(cur, coords) if cur is not None else 0.0
            # A MATERIAL THAT IS SCARCE HERE THIS SEASON IS WORTH LESS THAN ONE.
            #
            # Coverage alone treats every shelf as equal, so the plan will happily send the
            # fleet to a drained port and refresh it — the refresh always "works", it just
            # pays a quarter rate. Live 2026-09-06: Faro returned ~457 Pig per refresh and
            # Madeira ~110 Raisin for the same 11 gems, and Raisin still finished 655 short,
            # capping the barter at 6 rounds instead of 7.
            #
            # The weight IS that ratio (110/457 ~ 0.24), so a low-season port has to cover
            # about four times as many materials to beat an ordinary one — which is the
            # trade-off the numbers actually describe, not a knob.
            worth = sum(_LOW_SEASON_WEIGHT if _season(season_fn, port, m) == "low" else 1.0
                        for m in new)
            score = worth if cur is None else worth / (d + 1.0)
            if score > best_score:
                best, best_score, best_new, best_d = port, score, new, d
        if best is None:
            break
        route.append(best)
        covered |= best_new
        uncovered -= best_new
        total += best_d
        cur = port_coords[best]

    # EVERY SOURCE SCARCE IS A DIFFERENT ANSWER FROM "NO SOURCE". Both leave the mission
    # unable to gather, but one is worth waiting out and the other never will be.
    low_everywhere = set()
    for m in needed - unsourced:
        ports = [p for p in material_sources.get(m, ()) if p in port_coords]
        if ports and all(_season(season_fn, p, m) == "low" for p in ports):
            low_everywhere.add(m)

    over_cap = False
    if cargo_capacity is not None and quantities is not None:
        over_cap = sum(quantities.get(m, 0) for m in covered) > cargo_capacity

    return GatheringPlan(route=route, covered=covered,
                         unsourced=unsourced | uncovered,
                         total_distance=total, over_capacity=over_cap, low_everywhere=low_everywhere)


def assign_purchases(route: Sequence[str],
                     material_sources: Mapping[str, Sequence[str]],
                     quantities: Mapping[str, int]) -> dict:
    """Assign each needed material to the FIRST route port that sources it →
    {port: {material: qty}}. Feeds the buy-materials executor (#23)."""
    assignment: dict = {}
    covered: set = set()
    for port in route:
        for material, qty in quantities.items():
            if material in covered or qty <= 0:
                continue
            if port in material_sources.get(material, ()):
                assignment.setdefault(port, {})[material] = qty
                covered.add(material)
    return assignment
