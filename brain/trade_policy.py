"""Trade decision policy — pick the next action at a port from the OBSERVED state,
instead of running a blind sell→buy→sail sequence.

Motivation (user 2026-08-13): a fixed task loop sells everything at every port —
even goods just bought there — and buys even when the hold is already full of
cargo bound for the destination. The policy instead looks at what's actually true
right now and decides:

  1. SELL — if any cargo good sells at a PROFIT here (goods carried from
     elsewhere). Never sells goods that show a loss here — which also means it
     won't sell goods at the port where they were bought.
  2. BUY  — else, if the hold has room AND there are goods profitable to buy here
     for the destination (positive spread from the price KB).
  3. SAIL — else. The hold is already loaded with destination-bound cargo, or
     there's simply nothing profitable to do here → go.

Inputs are observable signals the caller gathers (cargo fill, the Sell page's
per-good profit, profitable_routes() from the market KB); the policy is pure and
testable. See project_sell_profit_and_distance, project_game_knowledge_base_vision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TradeState:
    port: str
    destination: Optional[str]
    cargo_used: int                                  # units in the hold now
    cargo_total: int
    sellable_profit_here: List[str] = field(default_factory=list)   # cargo goods that PROFIT if sold here
    buyable_for_dest: List[str] = field(default_factory=list)       # goods profitable to buy here for the destination


def decide_trade_action(s: TradeState, *, full_frac: float = 0.85) -> dict:
    """Return the next action: {op: 'sell'|'buy'|'sail'|'done', ...why}.

    full_frac — treat the hold as "no room to buy" at/above this fraction full.
    """
    # 1. Sell goods that are profitable HERE (carried in from elsewhere).
    if s.sellable_profit_here:
        return {"op": "sell", "goods": list(s.sellable_profit_here),
                "why": f"{len(s.sellable_profit_here)} cargo good(s) sell at a profit at {s.port}"}

    has_room = s.cargo_total > 0 and s.cargo_used < s.cargo_total * full_frac

    # 2. Buy for the destination if there's room and profitable goods.
    if s.destination and has_room and s.buyable_for_dest:
        return {"op": "buy", "goods": list(s.buyable_for_dest), "destination": s.destination,
                "why": f"room in hold + {len(s.buyable_for_dest)} good(s) profitable for {s.destination}"}

    # 3. Otherwise sail — the hold is loaded, or nothing here is worth trading.
    if s.destination:
        why = ("hold already loaded, nothing profitable to sell/buy here"
               if not has_room else
               "nothing profitable to sell here and no profitable buys for the destination")
        return {"op": "sail", "destination": s.destination, "why": why}

    return {"op": "done", "why": "no destination set and nothing profitable to do here"}
