"""What the market activity carries between ticks — and nothing else.

The flows used to hold this in locals for the length of one call: `sold`, `scrolled_pages`,
the ledger, the tracked tile. That was fine while a call WAS the whole flow. Once the flow
becomes a tick per action the locals have to live somewhere, and this is the somewhere.

KEYED TO THE GOAL, so nothing outlives its owner (CLAUDE.md Guiding Principle #4, and the
village's `_goal_key`). A new goal or a new port is a new visit: the cart, the staged basket
and the scroll position are `PANEL`/`BUILDING` data and die with the building; the ledger is
what this visit bought and sold, and dies with the visit.

WHAT IT MUST NOT HOLD is anything a fresh look could answer. "Is something staged?" is
readable — the Sell button carries a value when the basket has one — so it is NOT here.
Storing it would be a conclusion that outlives its evidence, and the whole point of a tick is
that the evidence is re-taken.

`last_intent` and `last_signature` ARE here, and they are the exception that proves the rule:
they do not describe the world, they record what THIS bot did and what the screen looked like
when it did it. That is how the next tick tells "my tap landed" from "nothing happened"
without holding control to find out — the judgement `barter_panel._try` makes inside a loop,
made across ticks instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class MarketState:
    """One visit to one market, for one goal."""

    key: Optional[Tuple] = None           # (goal, port) — a change resets everything

    # What this visit has done. `sold` is what a RESULT dialog confirmed, never what was
    # staged: reporting the staged list is a defect this doc-set already records.
    sold: List[str] = field(default_factory=list)
    # WHAT WAS STAGED, WAITING FOR THE CARD TO AUTHORISE IT. A SELL result card reports
    # MONEY — "Total Amount 122,637,216" — and names no goods at all, so `sold` could never
    # be filled from it and stayed `[]` through a 122.6M sale (live 2026-09-07 at London).
    #
    # This does not weaken the rule above. The CARD is still the only thing that authorises
    # an entry; staging merely supplies the NAME the card omits, and nothing moves across
    # without a card. Same shape as the buy side's `awaiting_credit`.
    sold_pending: List[str] = field(default_factory=list)
    ledger: Any = None

    # THE MOST THIS PORT WAS SEEN TO HOLD, per material, this visit. Read on sight beside
    # the season, and kept because the decision that needs it comes LATER — by the time the
    # shelf has been emptied it reads 0, and 0 says nothing about whether the port was worth
    # working. See `_a_port_worth_working` in `market_buy`.
    shelf_seen: Dict[str, int] = field(default_factory=dict)

    # Where we are in a grid that does not fit on one page.
    scrolled_pages: int = 0

    # THE TRIM, WHICH NOW SPANS TICKS. `trim_staged` is the REPORT of what went into the
    # basket, never the thing consulted to decide what to stage next — staging moves a good
    # OUT of its tile (measured live: Iron 999 read 822 once its 177 surplus was staged), so
    # the grid itself says what is left to do and survives any interruption the dispatcher
    # routes through. `trim_good`/`trim_owned`/`trim_excess` are the good whose dialog THIS
    # tick expects, and they are cleared the moment it is answered.
    trim_staged: Dict[str, int] = field(default_factory=dict)
    trim_skipped: List[str] = field(default_factory=list)
    trim_bulk_off: bool = False
    trim_committed: bool = False
    trim_good: Optional[str] = None
    trim_owned: Optional[int] = None
    trim_excess: Optional[int] = None
    trim_giving_up: set = field(default_factory=set)

    # The Trade Point award is claimed at most once per visit — tapping the chest opens a
    # dialog that hides the very counter a second attempt would consult.
    award_claimed: bool = False

    # THE RECORDED INTENT. What the previous tick did, and the screen it did it to.
    last_intent: Optional[str] = None
    last_signature: Optional[Tuple] = None

    # THE SHELF AS IT STOOD WHEN `Purchase` WAS TAPPED, waiting to be compared against the
    # shelf now — that difference is what was actually bought, and it is the ONLY way to
    # learn it, because the result card reports money rather than units.
    #
    # ITS OWN SLOT, and that is the point. It used to ride on `last_intent`, which is a
    # single slot describing the PREVIOUS TICK — and between tapping Purchase and coming back
    # to the grid the game shows a confirm and a result, each answered by a handler that
    # records what IT did. The purchase intent was overwritten every time, so the credit
    # never ran.
    #
    # Live 2026-09-07 at Faro: the ledger seeded `pig: 729`, ~914 Pig were bought over the
    # next four minutes, and the hold panel read 1,643 while the ledger still said 729 —
    # already past its 1,505 target and still buying. It had been masked until now by a
    # separate bug that re-seeded the ledger on almost every tick.
    awaiting_credit: Optional[Tuple] = None

    # Per-control attempt counts — the retry bound, replacing every `for attempt in range`.
    # The bound lives here because the GOAL's lifetime is the right lifetime for it.
    attempts: dict = field(default_factory=dict)

    def for_goal(self, key: Tuple) -> "MarketState":
        """This state if it belongs to `key`, else a fresh one. Never returns another
        visit's cart."""
        if self.key == key:
            return self
        return MarketState(key=key)

    def did(self, intent: str, signature=None) -> None:
        """Record what this tick attempted, so the next one can tell whether it landed."""
        self.last_intent = intent
        self.last_signature = signature

    def repeated(self, control: str, limit: int) -> bool:
        """Count an attempt at `control` and say whether it has now been tried too often.

        A swallowed tap costs a repeat; a control that never responds costs a report. The
        difference is a number, and it lives with the goal rather than in a loop.
        """
        self.attempts[control] = self.attempts.get(control, 0) + 1
        return self.attempts[control] > limit

    def landed(self, control: str) -> None:
        """The control responded — forget the attempts against it."""
        self.attempts.pop(control, None)
