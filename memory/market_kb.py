# memory/market_kb.py
# Market price knowledge base.
#
# Each port's market gets one JSON file:
#   memory/knowledge/markets/<port_slug>__market.json
#
# Structure:
#   { "port": "Socotra", "snapshots": [ { timestamp, tab, goods: [...] }, ... ] }
#
# Each goods entry:
#   { "name", "buy_price", "sell_price", "index_pct", "trend" }
#
# The latest snapshot is authoritative for current prices.
# All snapshots are kept so trends can be computed over time.

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

_MARKETS_DIR = Path("memory/knowledge/markets")


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class MarketGood:
    """A single trade good seen in the market."""
    name: str
    buy_price: Optional[int] = None      # None if not on Purchase tab or sold out
    sell_price: Optional[int] = None     # None if not on Sell tab
    index_pct: Optional[int] = None      # price index as integer percent (e.g. 87)
    trend: str = "unknown"               # "rising", "falling", "stable", "unknown"
    category: str = ""                   # e.g. "Food", "Textile", "Metal"
    available_qty: Optional[int] = None  # units in stock at market (purchase tab only; None if unknown)
    sold_out: bool = False               # True when restocking timer shown instead of price
    # GATED, NOT EMPTY. Some goods are conditional — purchasable only when a condition holds
    # (a time window, or membership of the city's monopolising guild). They are NOT a sold-out
    # shelf and NO blue-gem refresh will ever restock them, so they must be skipped rather
    # than refreshed (user, 2026-08-24). Marked by a coloured ribbon in the tile's TOP-LEFT
    # corner; measured at Bordeaux, Hungary Water's corner is 62% magenta against 0% on every
    # other tile on the grid.
    conditional: bool = False
    profit_per_unit: Optional[int] = None  # sell tab: per-unit profit baked with distance (negative = loss)
    is_loss: bool = False                # sell tab: selling here loses money (profit < 0)
    owned_qty: Optional[int] = None      # sell tab: units of this good currently in cargo
    tap_x: Optional[int] = None         # tile centre x in full-frame pixels
    tap_y: Optional[int] = None         # tile centre y in full-frame pixels

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketGood":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class MarketSnapshot:
    """
    A single visit's worth of market price data.

    purchase_goods: what the market sells (available to buy), with buy_price.
    sell_goods:     what the player was carrying that could be sold here,
                    with sell_price. This is cargo-dependent — not a complete
                    list of all goods the port accepts.
    """
    port: str
    timestamp: str
    purchase_goods: list[MarketGood] = field(default_factory=list)
    sell_goods: list[MarketGood] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["purchase_goods"] = [g.to_dict() for g in self.purchase_goods]
        d["sell_goods"]     = [g.to_dict() for g in self.sell_goods]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MarketSnapshot":
        purchase = [MarketGood.from_dict(g) for g in d.pop("purchase_goods", [])]
        sell     = [MarketGood.from_dict(g) for g in d.pop("sell_goods", [])]
        # back-compat: old snapshots had a flat "goods" list
        legacy   = [MarketGood.from_dict(g) for g in d.pop("goods", [])]
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        obj.purchase_goods = purchase or legacy
        obj.sell_goods     = sell
        return obj

    def summary(self) -> str:
        buy_names  = [g.name for g in self.purchase_goods]
        sell_names = [g.name for g in self.sell_goods]
        return (
            f"{self.port} market @ {self.timestamp[:16]}: "
            f"{len(self.purchase_goods)} buyable {buy_names}, "
            f"{len(self.sell_goods)} sellable {sell_names}"
        )


# ── Storage helpers ────────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    return s.lower().replace(" ", "_").replace("/", "_")


def _market_path(port: str) -> Path:
    return _MARKETS_DIR / f"{_slug(port)}__market.json"


def load_market(port: str) -> dict:
    """Load the full market record for a port. Returns empty dict if not found."""
    path = _market_path(port)
    if not path.exists():
        return {"port": port, "snapshots": []}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        logger.warning(f"Failed to load market KB {path}: {exc}")
        return {"port": port, "snapshots": []}


def save_snapshot(snapshot: MarketSnapshot) -> None:
    """Append a new price snapshot to the market record."""
    _MARKETS_DIR.mkdir(parents=True, exist_ok=True)
    record = load_market(snapshot.port)
    record["port"] = snapshot.port
    record.setdefault("snapshots", []).append(snapshot.to_dict())
    path = _market_path(snapshot.port)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    logger.info(f"Market snapshot saved: {snapshot.summary()}")


def latest_snapshot(port: str) -> Optional[MarketSnapshot]:
    """Return the most recent snapshot for a port, or None."""
    record = load_market(port)
    snapshots = record.get("snapshots", [])
    if not snapshots:
        return None
    return MarketSnapshot.from_dict(snapshots[-1])


def all_buy_prices(port: str) -> dict[str, int]:
    """Return {good_name: buy_price} from the latest snapshot's Purchase tab."""
    snap = latest_snapshot(port)
    if snap is None:
        return {}
    return {g.name: g.buy_price for g in snap.purchase_goods if g.buy_price is not None}


def all_sell_prices(port: str) -> dict[str, int]:
    """
    Return {good_name: sell_price} from the latest snapshot's Sell tab.
    Note: only covers goods the player was carrying during that visit.
    """
    snap = latest_snapshot(port)
    if snap is None:
        return {}
    return {g.name: g.sell_price for g in snap.sell_goods if g.sell_price is not None}


def profitable_routes(
    buy_port: str,
    sell_port: str,
    min_margin_pct: float = 10.0,
) -> list[dict]:
    """
    Compare prices between two ports.
    Returns goods where sell_price at sell_port > buy_price at buy_port
    by at least min_margin_pct%, sorted by margin descending.
    """
    buy_prices  = all_buy_prices(buy_port)
    sell_prices = all_sell_prices(sell_port)

    routes = []
    for good, buy in buy_prices.items():
        sell = sell_prices.get(good)
        if sell is None or buy <= 0:
            continue
        margin_pct = (sell - buy) / buy * 100
        if margin_pct >= min_margin_pct:
            routes.append({
                "good":        good,
                "buy_port":    buy_port,
                "buy_price":   buy,
                "sell_port":   sell_port,
                "sell_price":  sell,
                "margin_pct":  round(margin_pct, 1),
            })

    routes.sort(key=lambda r: r["margin_pct"], reverse=True)
    return routes
