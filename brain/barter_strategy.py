"""Barter strategy (#29) — which good, at which village, sold at which port.

The "AI + solvers READ the KB" side of the two-brain design. The ranking core is
DETERMINISTIC (net-profit estimate composed from the #18/#19/#22 solvers) — the
empirically-better half (LLM was 20% worse on the routing sub-problem,
project_llm_vs_solver_routing_2026-08-14). AI belongs on top for the fuzzy calls
(amity investment worth it?, season alpha, tie-breaks) — it re-ranks close plays,
it doesn't do the arithmetic.

Net profit per play:
    revenue        = barter output units × best sell value/unit  (#22)
    material_cost  = Σ material unit price × units consumed        (from market KB)
    net            = revenue − material_cost
Rounds/output come from the quantity solver (#19); eligibility gates candidates (#18).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from brain.eligibility import evaluate_eligibility
from brain.barter_quantity import solve_barter_quantity, output_for_amity
from brain.sell_port import best_sell_port


@dataclass
class BarterPlay:
    good: str
    village: str
    sell_port: Optional[str]
    rounds: int
    output_qty: int
    est_revenue: float
    est_material_cost: float
    est_net_profit: float
    limited_by: str = ""
    notes: list = field(default_factory=list)


def _material_cost(consumed: Mapping[str, int],
                   material_prices: Optional[Mapping[str, float]]) -> float:
    if not material_prices:
        return 0.0
    return float(sum(qty * material_prices.get(m, 0) for m, qty in consumed.items()))


def rank_barter_plays(
    recipes: Sequence,
    villages_by_name: Mapping[str, object],
    fleet,
    sell_options_by_good: Mapping[str, Sequence],
    materials_on_hand: Mapping[str, int],
    cargo_free: int,
    *,
    base_per_distance: float = 1.0,
    material_prices: Optional[Mapping[str, float]] = None,
    active_events: Sequence[str] = (),
) -> list:
    """Rank every eligible (recipe × village) barter play by estimated net profit.

    Each recipe is evaluated at each of its villages we have a Village record for and
    that passes eligibility (#18). Returns BarterPlays best→worst."""
    plays: list = []
    for recipe in recipes:
        for vname in (recipe.villages or []):
            village = villages_by_name.get(vname)
            if village is None:
                continue
            elig = evaluate_eligibility(recipe, village, fleet, active_events)
            if not elig.eligible:
                continue
            out_per_round = output_for_amity(recipe, village.amity)
            if not out_per_round:
                continue
            rounds_remaining = (village.barter_rounds_remaining
                                if village.barter_rounds_remaining is not None else 999)
            qp = solve_barter_quantity(out_per_round, recipe.inputs, materials_on_hand,
                                       rounds_remaining, cargo_free)
            if qp.rounds <= 0:
                continue
            sell = best_sell_port(sell_options_by_good.get(recipe.good, []),
                                  base_per_distance)
            sell_value = sell.value if sell else 0.0
            revenue = qp.output_qty * sell_value
            cost = _material_cost(qp.materials_consumed, material_prices)
            net = revenue - cost
            notes = []
            if sell is None:
                notes.append("no sell port known — revenue unscored")
            if qp.overflow_units:
                notes.append(f"overflow {qp.overflow_units} → jettison")
            plays.append(BarterPlay(
                good=recipe.good, village=vname,
                sell_port=(sell.port if sell else None),
                rounds=qp.rounds, output_qty=qp.output_qty,
                est_revenue=revenue, est_material_cost=cost, est_net_profit=net,
                limited_by=qp.limited_by, notes=notes))
    plays.sort(key=lambda p: p.est_net_profit, reverse=True)
    return plays


def best_barter_play(*args, **kwargs) -> Optional[BarterPlay]:
    """The single most profitable eligible play, or None."""
    ranked = rank_barter_plays(*args, **kwargs)
    return ranked[0] if ranked else None
