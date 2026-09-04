"""Pick a trade event worth selling into.

The plan (user, 2026-08-23): the fleet sails to LONDON as its hub, then checks the Trade Event
Schedule. If a **Bazaar** for a category we are carrying starts within **12 hours**, the bot
sleeps until it is nearly due and sails out to sell into the window — the spice bazaars sit
around London, so the hop is short. Folding every event into one grand plan was the other
option and it is harder: it needs a saved sailing route to each city, where the hub does not.

**Times are Korean (UTC+9) and this machine is not**, so everything here works in
timezone-aware datetimes and converts at the edges. Measured 2026-08-23: 22:28 CDT is 12:28
KST the following day, which put a 13:00 KST bazaar half an hour out — not the "few hours" a
naive read of the dialog suggests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence

from loguru import logger

from vision.trade_event_reader import TradeEvent

# How far ahead an event is still worth planning for.
DEFAULT_WITHIN_HOURS = 12.0

# Never commit to a window the fleet cannot physically reach: leave this much slack between
# arriving and the window closing.
DEFAULT_MARGIN = timedelta(minutes=10)


@dataclass(frozen=True)
class EventPlan:
    """A chosen event, and when to act on it."""

    event: TradeEvent
    wake_at: datetime            # when to start sailing (timezone-aware)
    sail_seconds: float          # the estimate the choice was made on

    @property
    def sleep_seconds(self) -> float:
        """Real seconds to wait before setting out. Never negative."""
        return max(0.0, (self.wake_at - datetime.now(timezone.utc)).total_seconds())


def sellable_events(events: Iterable[TradeEvent], carrying: Sequence[str], *,
                    now: Optional[datetime] = None,
                    within_hours: float = DEFAULT_WITHIN_HOURS) -> List[TradeEvent]:
    """Events we could sell into: right category, readable window, near enough, not over.

    `carrying` is the CATEGORIES aboard (e.g. ['Spices']) — the schedule names categories,
    not goods, so Box of Nutmeg is matched as Spices.
    """
    now = now or datetime.now(timezone.utc)
    want = {c.strip().lower() for c in carrying if c and c.strip()}
    horizon = now + timedelta(hours=within_hours)

    out = []
    for ev in events:
        if ev.goods.strip().lower() not in want:
            continue
        if ev.start is None or ev.end is None:
            # A clipped row from the scrollable list — no window, so nothing to plan on.
            logger.debug(f"[events] {ev.city} {ev.goods}: no readable window — skipped")
            continue
        if ev.end <= now:
            continue                       # already over
        if ev.start > horizon:
            continue                       # beyond the horizon
        out.append(ev)

    out.sort(key=lambda e: e.start)
    logger.info(f"[events] {len(out)} sellable event(s) within {within_hours:g}h for "
                f"{sorted(want)}: " + ", ".join(f"{e.goods}@{e.city}" for e in out))
    return out


def plan_event_sale(events: Iterable[TradeEvent], carrying: Sequence[str],
                    sail_seconds_fn, *, now: Optional[datetime] = None,
                    within_hours: float = DEFAULT_WITHIN_HOURS,
                    margin: timedelta = DEFAULT_MARGIN) -> Optional[EventPlan]:
    """The soonest event we can actually reach in time, and when to set out.

    `sail_seconds_fn(city) -> float | None` estimates the voyage in REAL seconds; None means
    "no idea", and an unknown voyage is not planned around — arriving after the window closes
    wastes the trip and the cargo stays unsold either way.
    """
    now = now or datetime.now(timezone.utc)
    for ev in sellable_events(events, carrying, now=now, within_hours=within_hours):
        secs = sail_seconds_fn(ev.city)
        if secs is None:
            logger.info(f"[events] {ev.city}: no sailing estimate — not planning for it")
            continue
        arrive_by = ev.end - margin
        latest_start = arrive_by - timedelta(seconds=secs)
        if latest_start < now:
            logger.info(f"[events] {ev.goods}@{ev.city} closes {ev.end:%H:%M %Z} and needs "
                        f"{secs / 60:.0f} min of sailing — cannot make it")
            continue
        # Set out in time to arrive as the window opens, and never before now.
        wake = max(now, ev.start - timedelta(seconds=secs))
        wake = min(wake, latest_start)
        logger.info(f"[events] chosen: {ev.kind} {ev.goods} at {ev.city}, window "
                    f"{ev.start:%m-%d %H:%M}-{ev.end:%H:%M} KST, ~{secs / 60:.0f} min sailing "
                    f"→ set out {wake:%H:%M %Z}")
        return EventPlan(event=ev, wake_at=wake, sail_seconds=secs)

    logger.info("[events] nothing sellable and reachable — no event plan")
    return None


# ── Selling INTO the window ───────────────────────────────────────────────────
#
# The sale is only worth making while the bazaar is live, and the game shows that plainly
# (user, 2026-08-23): on the Sell page a good in the bazaar's category gets a GREEN BAND on
# its tile, and its price index runs very high — usually above 150%, often more.
#
# The index is the machine-readable half and `vision.market_reader` already parses it, so it
# is what gates the sale here. The green band is the corroborating cue and is deliberately
# NOT guessed at: no frame of a live bazaar has been measured yet, and inventing a colour
# threshold without a sample is how the pale-gold Exchange button ended up being rejected.

def categories_on_hand(goods: Sequence) -> List[str]:
    """The CATEGORIES the fleet is carrying, read off the Sell page.

    The Sell tab lists exactly what is in the hold, so it is the cargo manifest and no
    separate reader is needed. This exists because the category was being passed in by hand:
    live 2026-08-24 the bot was told to sell `Spices`, planned a voyage to a spice bazaar, and
    only discovered at the market that the hold held Ebony, Coral and Textiles. An event is
    worth sailing to only for goods actually aboard, so the cargo picks the event.
    """
    seen, out = set(), []
    for g in goods:
        cat = (getattr(g, "category", "") or "").strip()
        if cat and cat.lower() not in seen:
            seen.add(cat.lower())
            out.append(cat)
    return out


BAZAAR_MIN_INDEX = 150       # % — a bazaar lifts the index well past a normal market's ~100


def bazaar_is_live(goods: Sequence, category: str,
                   min_index: int = BAZAAR_MIN_INDEX) -> bool:
    """True when the Sell page shows bazaar pricing for `category`.

    `goods` are `MarketGood`s read from the Sell tab. A normal market sits near 100%; the
    bazaar tiles run 150%+, so the index separates them without needing the colour cue.

    Returns False when nothing in the category is readable — "cannot tell" must not read as
    "the bazaar is on", or the fleet dumps its cargo at the ordinary price.
    """
    want = (category or "").strip().lower()
    matched = [g for g in goods
               if (getattr(g, "category", "") or "").strip().lower() == want]
    if not matched:
        logger.info(f"[events] nothing in category {category!r} on the Sell page — "
                    "cannot confirm the bazaar")
        return False

    indices = [g.index_pct for g in matched if getattr(g, "index_pct", None) is not None]
    if not indices:
        logger.warning(f"[events] {category} tiles carry no readable price index — refusing "
                       "to call the bazaar live")
        return False

    best = max(indices)
    live = best >= min_index
    logger.info(f"[events] {category} price index {best}% "
                f"({'BAZAAR' if live else 'ordinary'}; threshold {min_index}%)")
    return live


def should_sell_now(event: TradeEvent, goods: Sequence, *,
                    now: Optional[datetime] = None,
                    min_index: int = BAZAAR_MIN_INDEX) -> tuple:
    """(sell?, why) — commit the sale only inside the window AND at bazaar pricing.

    Both are required. The clock says the window should be open; the price index says it
    actually is. They disagree when the fleet arrives late, when the window has just shut, or
    when the schedule was read from a stale screen — and the price is the one that decides,
    because it is what the sale is worth.
    """
    now = now or datetime.now(timezone.utc)
    in_window = event.is_live(now)
    priced = bazaar_is_live(goods, event.goods, min_index=min_index)

    if priced:
        return True, (f"{event.goods} is at bazaar pricing in {event.city}"
                      + ("" if in_window else " (outside the scheduled window, but the "
                                              "price is what counts)"))
    if in_window:
        return False, (f"inside the {event.city} window but {event.goods} is not at bazaar "
                       "pricing — arrived too early, or the wrong market")
    return False, f"the {event.city} {event.goods} window is not live"
