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
from typing import Any, List, Optional, Tuple


@dataclass
class MarketState:
    """One visit to one market, for one goal."""

    key: Optional[Tuple] = None           # (goal, port) — a change resets everything

    # What this visit has done. `sold` is what a RESULT dialog confirmed, never what was
    # staged: reporting the staged list is a defect this doc-set already records.
    sold: List[str] = field(default_factory=list)
    ledger: Any = None

    # Where we are in a grid that does not fit on one page.
    scrolled_pages: int = 0

    # THE RECORDED INTENT. What the previous tick did, and the screen it did it to.
    last_intent: Optional[str] = None
    last_signature: Optional[Tuple] = None

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
