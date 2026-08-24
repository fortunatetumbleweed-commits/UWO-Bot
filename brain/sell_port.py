"""Sell-port selection (#22) — where to sell a good for the most ducats.

Domain model (from the season walkthrough): a good's sell value is driven mainly by
DISTANCE from where it's produced (far ports pay far more — "triple due to distance")
and multiplied by the port's per-CATEGORY seasonal preference (e.g. Food +30%). The
category preference is ALWAYS readable (Preference tab); the exact per-unit price is
only visible when the port is within trade-level range (#13 read_port_good_price) —
when we have it, it wins over the estimate.

    estimate ≈ base_per_distance · distance · (1 + preference%/100)

Pure + testable. The barter strategy (#29) uses this to pick the sell leg.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class SellOption:
    port: str
    distance: float                      # from the good's source / current position
    preference_pct: float = 0.0          # markup for THIS good's category at this port
    known_price: Optional[int] = None    # exact per-unit price when in trade-level range


@dataclass
class SellRanking:
    port: str
    value: float          # estimated (or known) per-unit sell value, comparable across ports
    known: bool           # True if `value` came from an exact in-range price


def estimate_value(opt: SellOption, base_per_distance: float = 1.0) -> float:
    """Per-unit sell value. Uses the exact price when visible; otherwise
    distance-base × (1 + preference)."""
    if opt.known_price is not None:
        return float(opt.known_price)
    return base_per_distance * opt.distance * (1.0 + opt.preference_pct / 100.0)


def rank_sell_ports(options: Sequence[SellOption],
                    base_per_distance: float = 1.0) -> list:
    """Rank sell options best→worst. `base_per_distance` sets the distance→ducat
    scale so estimates are comparable to any known in-range prices (pick it from a
    known good's price/distance when available)."""
    ranked = [SellRanking(o.port, estimate_value(o, base_per_distance),
                          o.known_price is not None)
              for o in options]
    ranked.sort(key=lambda r: r.value, reverse=True)
    return ranked


def best_sell_port(options: Sequence[SellOption],
                   base_per_distance: float = 1.0) -> Optional[SellRanking]:
    """The single best sell option, or None if there are no options."""
    ranked = rank_sell_ports(options, base_per_distance)
    return ranked[0] if ranked else None
