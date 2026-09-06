"""Which MARKET screen is this? — the context-scoped read for the market activity.

The outer `classify_nav_state` answers "which of the forty screens is this?" and returns
`building:market` for every one of them: the purchase grid, the sell grid, the quantity
prompt, the result dialog. That verdict is correct and useless — each needs different
handling and it cannot tell them apart. This answers the scoped question instead, and
returns MISS when the answer is none of the market's own screens.

WHY THIS EXISTS AT ALL. The market is the last activity still driving whole flows in private
loops, and every failure recorded in `docs/market_as_contexts.md` (FC-1..FC-4) traces to
control sitting inside one of them while the game showed something it did not expect. A
context table is how control gets handed back: classify, do ONE thing, return.

CLASSIFY BY STRUCTURE AND POSITION, NEVER BY WORDING — the lesson FC-3 paid for. The
village's `OVERFLOW_PROMPT` keys on ("overflow", "exceeds", "cargo is full"); the game says
"Insufficient Empty Space", so that handler has never once been reached and every overflow
this project has met was discarded in silence. The words were guessed. The LAYOUT was not: a
card carrying a `Received Trade Goods` strip above a `Cargo` strip is that dialog whatever it
is called.

So the shape of every test here is:

    is a bounded card up?           -> `detect_dialog`, which owns "is there a dialog"
    what does the card CONTAIN?     -> strips, action labels, a gem cost, a numeric field
    only then, what does it SAY?    -> to RANK candidates, never as the gate

IT DOES NOT ASK WHETHER WE ARE STILL IN THE MARKET, and does not need to (user, 2026-09-06):
*"when in a building, unless the bot explicitly tap back or home, it won't exit the building,
so we can assume it is still market unless it hit home or back at the building top menu."*
A building is a place the bot OCCUPIES until it leaves, and leaving is an action it takes —
so occupancy is known, not observed, and re-deriving it here would be a second opinion on a
question the dispatcher's routing already answers. When this cannot name a screen it returns
MISS and hands back; if neither layer recognises something, the fix is to teach the right one
to recognise it.

`elements`, `dialog` and `tab` are injectable because the defaults capture and parse, and a
unit test that reaches for the device is a slow test.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from loguru import logger

MISS = "miss"

MARKET_LANDING = "market_landing"
PURCHASE_PAGE = "purchase_page"
SELL_PAGE = "sell_page"
QUANTITY_DIALOG = "quantity_dialog"
CONFIRM_DIALOG = "confirm_dialog"
RESULT_DIALOG = "result_dialog"
NEGOTIATION = "negotiation"
TRADE_GOODS_INFO = "trade_goods_info"
RESTOCK_PROMPT = "restock_prompt"
OVERFLOW_PROMPT = "overflow_prompt"
DISCARD_NOTICE = "discard_notice"
CARGO_FULL_NOTICE = "cargo_full_notice"

CONTEXT_STATES = (
    MARKET_LANDING,
    PURCHASE_PAGE,
    SELL_PAGE,
    QUANTITY_DIALOG,
    CONFIRM_DIALOG,
    RESULT_DIALOG,
    NEGOTIATION,
    TRADE_GOODS_INFO,
    RESTOCK_PROMPT,
    OVERFLOW_PROMPT,
    DISCARD_NOTICE,
    CARGO_FULL_NOTICE,
    MISS,
)

# ── structural signatures ────────────────────────────────────────────────────
#
# Each is a thing the card CONTAINS, not a phrase it uses. Where a phrase appears below it
# names a STRIP or a COLUMN HEADING — furniture the game draws to structure the card — and
# it is required alongside another such heading, never alone.

# The overflow card and the discard notice that opens over it are asked of
# `vision.region_detectors.overflow_cards`, which owns the pair — the same two cards appear
# in the barter flow, and one implementation per question is the rule.

# The hold cannot take the purchase — a NOTICE, not a dialog with a choice worth making.
_SLOT_OVERRUN = ("will be exceeded by", "exceeded by")

# The restock card prices itself in gems and says what it does not affect.
_RESTOCK_MARKS = ("replenish stock", "does not affect market price", "remaining:")

# The result card is the PROOF a transaction happened (CLAUDE.md), and the ONLY context
# allowed to write the ledger — which is what makes FC-2 unreachable, so telling it from the
# confirm card matters more than most.
#
# THE DIFFERENCE IS A CHOICE VERSUS AN ACKNOWLEDGEMENT, not a phrase. Frames 93 and 97 of
# trace_barter_cmd_2026-09-05T21-38-09 are the pair:
#
#   Confirm Sales   Cancel + OK   "Total Sales  95,727"
#   Result          OK alone      "Total Amount 95,727"  + "Balance 68,380,908,293"
#
# One word apart. Keying on the word worked and would have broken the moment the game said
# "Total Sales" on a result card — the same shape of mistake as `_OVERFLOW_WORDS`. So the
# test is the SHAPE: nothing to decide (no Cancel) and the company's money AFTER the fact.
# `Balance` is the strongest single marker, because only a completed transaction has one.
_RESULT_ROWS = ("balance", "mate trade exp", "obtained trade point")
_RESULT_MONEY = ("total amount", "profit/loss", "profit / loss")

_NEGOTIATION_MARKS = ("negotiation", "negotiate", "nego. chance")

# The card a tile tap opens: the good's description, its price history, and a stepper.
_INFO_ROWS = ("description", "max price", "min price")

# The amount keypad opens OVER the Trade Goods Info card, and is recognised BY ITS KEYS —
# see `vision.region_detectors.keypad`, which owns the question because the same card is used
# throughout the game. Not by its "Enter Number" title: a caption is the weakest evidence
# here, and the keys are language-independent.


def classify(frame, *, elements=None, dialog=None, tab=None) -> str:
    """The market-scoped state, or MISS.

    `dialog` is a `DialogModel` or None; `tab` is 'purchase' | 'sell' | None (unknown).
    """
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
    labels = [(getattr(e, "label", "") or "").strip().lower() for e in elements or []]
    text = " | ".join(l for l in labels if l)

    # ── OUR OWN CARDS FIRST ──────────────────────────────────────────────────
    #
    # They sit ON TOP of the grid, so reading the grid underneath and calling it a page is
    # how a dialog row became a tile ("Im glad" staged as a good, 2026-08-30) and how a
    # positive button got pressed with nobody knowing which card it was on (FC-3).
    if dialog is None and frame is not None:
        dialog = _detect_dialog(frame, elements)

    card = _card_text(dialog, text)
    if dialog is not None or _looks_like_a_card(text):
        found = _which_card(card, dialog, elements)
        if found is not None:
            return found

    # ── THE PAGES ────────────────────────────────────────────────────────────
    if tab is None:
        tab = _active_tab(frame, text)
    if tab == "sell":
        return SELL_PAGE
    if tab == "purchase":
        return PURCHASE_PAGE

    # The market's own left menu, with neither grid up.
    if _has_market_menu(text):
        return MARKET_LANDING
    return MISS


def _which_card(card: str, dialog, elements=()) -> Optional[str]:
    """Which of the market's cards this is, by what it CONTAINS. None = not one of ours.

    Order is by specificity, not by likelihood: the discard notice opens OVER the overflow
    card, so both sets of furniture are on screen at once and the innermost must win.
    """
    # The discard notice sits over the overflow card — test it FIRST or the overflow's own
    # strips claim the frame and the wrong handler answers a live choice.
    from vision.region_detectors.overflow_cards import is_discard_notice, is_overflow_card
    if is_discard_notice(card):
        return DISCARD_NOTICE
    if is_overflow_card(card):
        return OVERFLOW_PROMPT
    if _any(card, _SLOT_OVERRUN):
        return CARGO_FULL_NOTICE
    if _any(card, _RESTOCK_MARKS):
        return RESTOCK_PROMPT
    if _is_result(card, dialog):
        return RESULT_DIALOG
    if _any(card, _NEGOTIATION_MARKS):
        return NEGOTIATION
    if _has_keypad(elements):
        # INNERMOST WINS. The keypad opens OVER the Trade Goods Info card, so both are on
        # screen and only the keypad is live.
        return QUANTITY_DIALOG
    if _any(card, _INFO_ROWS):
        return TRADE_GOODS_INFO
    if _has_choice(dialog):
        # A card with a real choice and none of the above furniture. Deliberately last: it is
        # the shape every one of the others also has, so reaching it means nothing more
        # specific matched.
        return CONFIRM_DIALOG
    return None


# ── the readings the tests above are made of ─────────────────────────────────

def _detect_dialog(frame, elements):
    try:
        from vision.region_detectors.dialog import detect_dialog
        return detect_dialog(elements or [], frame.width, frame.height, frame=frame)
    except Exception as exc:                      # a classifier must never raise into a tick
        logger.debug(f"[market-context] dialog detection skipped: {exc}")
        return None


def _card_text(dialog, whole_frame_text: str) -> str:
    """The card's own text where we have it, else the frame's.

    THE CARD, NOT THE FRAME, when a card is known: the grid behind is full of goods names and
    numbers, and a whole-frame search answers yes almost anywhere — the same reason
    `location_panel_is_for` crops the panel rather than searching the map.
    """
    body = " | ".join(t.strip().lower() for t in (getattr(dialog, "body_text", None) or ()))
    title = ((getattr(getattr(dialog, "title_bar", None), "text", None) or "").strip().lower())
    actions = " | ".join((getattr(a, "label", "") or "").strip().lower()
                         for a in (getattr(dialog, "actions", None) or ()))
    card = " | ".join(p for p in (title, body, actions) if p)
    return card or whole_frame_text


def _looks_like_a_card(text: str) -> bool:
    """A card is up even though `detect_dialog` did not model it.

    Detection is stricter than recognition — it wants a title bar, a body and a bounded card
    — and the overflow card nests a second card inside itself, which is exactly the shape it
    is worst at. The furniture below is unambiguous enough to say "a card is up"; WHICH card
    is then decided the same way as always.
    """
    from vision.region_detectors.overflow_cards import is_discard_notice, is_overflow_card
    return is_discard_notice(text) or is_overflow_card(text)


def _is_result(card: str, dialog) -> bool:
    """The card reporting a FINISHED transaction, as opposed to one proposing it.

    Structure first: a result offers nothing to decide, so a `Cancel` rules it out however
    the rows are worded. Then its own rows — `Balance` above all, which is the company's
    money AFTER and exists on no card that has not yet happened.
    """
    if _offers_a_choice(dialog):
        return False                              # a choice means it has NOT happened yet
    if _any(card, _RESULT_ROWS):
        return True
    # A money total on a card with nothing to cancel. Weaker, and last, because the confirm
    # card carries a total too — it just also carries a Cancel.
    return _any(card, _RESULT_MONEY)


def _offers_a_choice(dialog) -> bool:
    """Cancel (or No) alongside a positive — the card is asking, not reporting."""
    labels = {(getattr(a, "label", "") or "").strip().lower()
              for a in (getattr(dialog, "actions", None) or ())}
    return bool(labels & {"cancel", "no", "decline"})


def _has_choice(dialog) -> bool:
    labels = {(getattr(a, "label", "") or "").strip().lower()
              for a in (getattr(dialog, "actions", None) or ())}
    return bool(labels & {"ok", "okay", "yes", "confirm", "cancel", "no"})


def _has_keypad(elements) -> bool:
    """The amount keypad is up — asked of the detector that owns it, game-wide.

    Read from the FRAME's elements, not the card's text: `detect_dialog` models one card, and
    the keypad opens OVER the Trade Goods Info card, so the modelled one may be the other.
    """
    try:
        from vision.region_detectors.keypad import keypad_is_up
        return keypad_is_up(elements)
    except Exception as exc:                      # a classifier must never raise into a tick
        logger.debug(f"[market-context] keypad check skipped: {exc}")
        return False


def _active_tab(frame, text: str) -> Optional[str]:
    """Purchase or Sell, or None. Asked of the reader that already owns the question.

    `buy_materials._on_sell_tab` decides on the COMMIT BUTTON — "Sell" on one grid,
    "Purchase" on the other from the same slot — because the left menu paints BOTH words on
    every market screen and the title flips a beat before the grid does. Live 2026-08-30 at
    Faro a title-first reader answered "Sell" over a Purchase grid, and the caller loaded
    goods the fleet had never bought into a sell basket.

    Asking it here rather than re-deriving keeps one implementation of the question, so the
    context and the action cannot disagree about which grid is up.
    """
    try:
        from actions.buy_materials import _on_sell_tab, _submenu_says
        if _on_sell_tab(frame):
            return "sell"
        # NOT-SELL IS NOT PURCHASE. `_on_sell_tab` answers one question and returns False for
        # "no" and for "could not tell" alike; treating that as Purchase would classify an
        # unreadable market as the buy grid and let a handler act on it.
        if _submenu_says(frame) == "purchase":
            return "purchase"
    except Exception as exc:
        logger.debug(f"[market-context] tab unreadable: {exc}")
    return None


def _has_market_menu(text: str) -> bool:
    return "purchase" in text and "sell" in text


def _any(text: str, words: Sequence[str]) -> bool:
    return any(w in text for w in words)
