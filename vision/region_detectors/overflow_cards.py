"""The two cards the game raises when a trade will not fit in the hold.

They are a PAIR and they stack, so telling them apart is the whole job:

    Insufficient Empty Space        "Cannot receive item due to insufficient space.
                                     Please organize your Cargo Hold.
                                     Unreceived trade goods will be discarded."
      Received Trade Goods  [360]
      Cargo  4,952/4,952 (100%)
                            [Receive]

    Notice                          "Complete the trade?
                                     400 Bambara Groundnut has not been claimed yet.
                                     Unclaimed trade goods will be discarded."
                            [Cancel] [OK]

The first says the hold is full and offers to hand over what fits. The second opens OVER it
and asks whether to finish anyway, throwing the rest away. Different questions, different
answers — the first is where materials get dumped to make room, the second is where that
chance is given up.

WHY THIS IS SHARED. It is the barter's card today, and `brain/market_context.py` describes
the same pair for the market's own cargo-full cases. One implementation per question
(CLAUDE.md), and the keypad detector next door is the precedent.

RECOGNISED BY STRUCTURE, AND THIS ONE COST A LOT. `brain/village_context._OVERFLOW_WORDS`
keyed on ("overflow", "exceeds", "cargo is full") — three phrases the game has never drawn —
so `_on_overflow`, which holds the last-round dumping policy, was UNREACHABLE for the whole
life of the project and every overflow was silently discarded. The words were imagined; the
layout was not. See FC-3 in `docs/market_as_contexts.md`.

NOT "will be discarded", WHICH BOTH CARDS SAY. Keying the notice on that phrase classified
the overflow card as the notice on the first frame it met (frame 214 of
`trace_barter_cmd_2026-09-05T21-38-09`). The QUESTION separates them: only the notice asks
one.
"""
from __future__ import annotations

from typing import Sequence

# The overflow card is the only one carrying BOTH strips — what the game is trying to hand
# over, and what the hold already holds.
_RECEIVED_STRIP = ("received trade goods",)
_CARGO_STRIP = ("cargo",)

# The notice asks. These are its question, not its consequence.
_NOTICE_QUESTION = ("has not been claimed", "complete the trade")


def is_discard_notice(text: str) -> bool:
    """The "complete the trade without them?" card.

    TESTED BEFORE the overflow card: it opens OVER it, so both sets of furniture are on
    screen at once and the innermost is the live one.
    """
    return _any(text, _NOTICE_QUESTION)


def is_overflow_card(text: str) -> bool:
    """The "Insufficient Empty Space" card, with its Received and Cargo strips."""
    return _any(text, _RECEIVED_STRIP) and _any(text, _CARGO_STRIP)


def _any(text: str, words: Sequence[str]) -> bool:
    t = (text or "").lower()
    return any(w in t for w in words)
