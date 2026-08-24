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
from typing import Mapping, Optional, Sequence


@dataclass
class GatheringPlan:
    route: list = field(default_factory=list)      # ordered ports to visit
    covered: set = field(default_factory=set)      # materials sourced by the route
    unsourced: set = field(default_factory=set)    # materials with no known source port
    total_distance: float = 0.0
    over_capacity: bool = False                    # total units needed exceed the hold


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def plan_gathering(needed: Sequence[str],
                   material_sources: Mapping[str, Sequence[str]],
                   port_coords: Mapping[str, tuple],
                   start: tuple,
                   quantities: Optional[Mapping[str, int]] = None,
                   cargo_capacity: Optional[int] = None) -> GatheringPlan:
    """Plan a gathering route covering `needed` materials from their source ports.

    needed:            materials to gather.
    material_sources:  {material: [ports that sell it]}.
    port_coords:       {port: (x, y)} for the sourceable ports.
    start:             (x, y) current position.
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
            d = _dist(cur, coords)
            score = len(new) / (d + 1.0)          # coverage per marginal distance
            if score > best_score:
                best, best_score, best_new, best_d = port, score, new, d
        if best is None:
            break
        route.append(best)
        covered |= best_new
        uncovered -= best_new
        total += best_d
        cur = port_coords[best]

    over_cap = False
    if cargo_capacity is not None and quantities is not None:
        over_cap = sum(quantities.get(m, 0) for m in covered) > cargo_capacity

    return GatheringPlan(route=route, covered=covered,
                         unsourced=unsourced | uncovered,
                         total_distance=total, over_capacity=over_cap)


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
