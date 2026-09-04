"""Remembered values, each with an OWNER — and each dying when its owner leaves play.

Ask of any remembered value not "where did I read it?" but "WHO DOES IT BELONG TO?". Only
the second predicts when it goes bad (user, 2026-08-26).

    COMPANY   ducats, gems, the mission and its plan       always valid
    FLEET     cargo, capacity, supply, crew, ship life     valid in EVERY world — it moves
    PLACE     this port/village: buildings, prices, amity  dies on leaving
    BUILDING  what this market stocks, which tab is open   dies on leaving the building
    PANEL     the cart, the selected tile, the open dialog dies when the panel closes

THE CART IS WHY OWNERSHIP AND NOT LOCATION. The market's right panel shows the cart and the
hold in ONE rectangle — active tiles staged for purchase, greyed tiles already owned
(`memory/market-right-panel-is-cart-and-cargo`). Same glance, two owners, two lifetimes:
leaving the market destroys the cart, while the cargo sails with the ship. A remembered cart
is an instruction to buy things nobody chose. Keyed by where it was read, both would share a
fate, and one of them would be wrong.

TWO WAYS DATA GOES BAD, NOT ONE:

    LEAVING   `observe()` drops PLACE / BUILDING / PANEL when the world moves out from
              under them. Automatic — the dispatcher calls it every tick.
    ACTING    `changed()` drops what an action changed, wherever it happened. Buying
              changes the hold; bartering changes the hold AND the amity AND today's rounds.

FLEET data is immune to the first and fully exposed to the second, which is exactly why a
place-based model gets it wrong: it would drop the hold's contents on departure — the one
fact the next leg needs.

WHAT THIS IS NOT. It is not a substitute for looking. THE SCREEN OUTRANKS THE RECORD: this is
a cache that makes re-reading cheap to skip, never a record to trust over a fresh look. And
it stores OBSERVATIONS ONLY — never a conclusion (CLAUDE.md), because a conclusion cannot be
checked by looking and so has no honest expiry at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from loguru import logger

COMPANY = "company"
FLEET = "fleet"
PLACE = "place"
BUILDING = "building"
PANEL = "panel"

# Ordered widest-lived first. Leaving a place also ends every building in it and every panel
# in those — so dropping an owner drops everything after it in this list.
_NESTED = (PLACE, BUILDING, PANEL)
_ALL = (COMPANY, FLEET) + _NESTED


@dataclass
class _Store:
    values: Dict[str, Tuple[str, Any]] = field(default_factory=dict)   # key -> (owner, value)
    where: Tuple[Optional[str], Optional[str], Optional[str]] = (None, None, None)


_store = _Store()


# ── reading and writing ──────────────────────────────────────────────────────

def remember(key: str, value: Any, *, owner: str) -> None:
    """Record an OBSERVATION under its owner."""
    if owner not in _ALL:
        raise ValueError(f"unknown owner {owner!r} — one of {_ALL}")
    _store.values[key] = (owner, value)


def recall(key: str, default: Any = None) -> Any:
    """What was remembered, or `default` if its owner has since left play."""
    got = _store.values.get(key)
    return default if got is None else got[1]


def owner_of(key: str) -> Optional[str]:
    got = _store.values.get(key)
    return None if got is None else got[0]


def forget(*owners: str) -> list:
    """Drop everything belonging to these owners. Returns the keys dropped."""
    doomed = [k for k, (o, _v) in _store.values.items() if o in owners]
    for k in doomed:
        del _store.values[k]
    return doomed


def reset() -> None:
    """Drop everything, including COMPANY. For tests and for a fresh session."""
    _store.values.clear()
    _store.where = (None, None, None)


# ── the two ways data goes bad ───────────────────────────────────────────────

def observe(state: Any) -> list:
    """Note where the bot is now, and drop what the move invalidated.

    Called by the dispatcher every tick, because the dispatcher is the only layer that sees
    both the previous world and this one. Returns the owners dropped, for the log.
    """
    now = _identity(state)
    was = _store.where
    _store.where = now

    # THE PLACE ITSELF IS A PLACE-OWNED FACT, and the most asked-for one: a building has no
    # port name on screen, so a caller standing in a market cannot answer "which port is
    # this?" from the frame. It survives entering a building and dies when the fleet puts to
    # sea, which is exactly the lifetime PLACE already describes.
    #
    # Recorded BEFORE the first-look return below, or the very first observation — usually
    # the only one taken on the port overworld, where the name IS legible — is the one look
    # that never gets stored.
    if now[0]:
        remember("port", now[0], owner=PLACE)

    if was == (None, None, None):
        return []                       # first look — nothing to have left

    dead = []
    for i, owner in enumerate(_NESTED):
        if was[i] != now[i]:
            # Everything from here down goes: leaving a place ends its buildings, and
            # leaving a building closes its panels. Nobody has to enumerate them.
            dead = list(_NESTED[i:])
            break

    if dead:
        keys = forget(*dead)
        logger.info(f"[owned] {was} -> {now}: dropped {dead}"
                    + (f" ({len(keys)} value(s))" if keys else " (nothing was held)"))
    return dead


def changed(*owners: str) -> list:
    """An action changed something. Drop what it changed, wherever the bot is.

    Buying is `changed(FLEET, BUILDING)` — the hold moved and so did the shelf. Bartering is
    `changed(FLEET, PLACE)` — the hold moved, and so did the amity and today's rounds.
    """
    keys = forget(*owners)
    if keys:
        logger.info(f"[owned] an action changed {list(owners)}: dropped {keys}")
    return keys


# ── where the bot is, as an identity ─────────────────────────────────────────

def _identity(state: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(place, building, panel) — the three that can be left.

    A place is a port or a village BY NAME, so sailing from Seville to Barcelona changes it
    even though both are `port_overworld`. That is the case a state-string comparison misses
    entirely, and it is the common one.
    """
    where = getattr(state, "state", None) or getattr(state, "location", None) or ""
    port = getattr(state, "port", None)

    if where in ("sea", "sea_cinematic", "world_map", "loading", "transient", "idle_lock"):
        # Not anywhere — but NOT a reason to forget the place either. `transient` and
        # `idle_lock` cover a world rather than replacing it, and `world_map` is opened from
        # on top of one. Treating them as "left the port" would drop a market's contents
        # because a notice appeared over it. Only the SEA means the port is genuinely behind
        # us, and the sea reports no port name, so the place becomes None by itself.
        if where in ("sea", "sea_cinematic"):
            return (None, None, None)
        return _store.where

    head, _, tail = where.partition(":")
    name = tail.strip().lower() or None

    if head == "building":
        return (port or _store.where[0], name, None)
    if head == "sub_menu":
        # A sub-menu is INSIDE a building, and the state string does not say which. Inherit
        # it: treating the building as unknown here would drop the market's contents every
        # time the bot opened the Purchase tab.
        return (port or _store.where[0], _store.where[1], name)
    # port_overworld, village: a place, no building, no panel.
    return (port or _store.where[0], None, None)
