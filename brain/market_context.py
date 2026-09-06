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

# The overflow card is the only one with BOTH strips: what the game is trying to hand over,
# and what the hold already has. Frame 214 of trace_barter_cmd_2026-09-05T21-38-09.
_RECEIVED_STRIP = ("received trade goods",)
_CARGO_STRIP = ("cargo",)

# The discard notice is a second, smaller card that opens OVER the overflow one and asks
# whether to complete the trade without the unclaimed goods.
#
# NOT "will be discarded" — BOTH cards say it, and using it here classified the overflow card
# as the notice on the very first frame it was tested against (frame 214):
#
#   overflow: "Cannot receive item ... Unreceived trade goods will be discarded."
#   notice:   "Complete the trade? N X has not been claimed yet. Unclaimed trade goods
#              will be discarded."
#
# The QUESTION is what separates them, and only the notice asks one. Which is the module's
# own rule turned on itself: a shared phrase is not a discriminator, and the structure is —
# the overflow card offers `Receive`, the notice offers a choice.
_UNCLAIMED = ("has not been claimed", "complete the trade")

# The hold cannot take the purchase — a NOTICE, not a dialog with a choice worth making.
_SLOT_OVERRUN = ("will be exceeded by", "exceeded by")

# The restock card prices itself in gems and says what it does not affect.
_RESTOCK_MARKS = ("replenish stock", "does not affect market price", "remaining:")

# The result card is the PROOF a transaction happened (CLAUDE.md). It is the only market card
# carrying a money total.
_RESULT_ROWS = ("total amount", "profit/loss", "profit / loss")

_NEGOTIATION_MARKS = ("negotiation", "negotiate", "nego. chance")


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
        found = _which_card(card, dialog)
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


def _which_card(card: str, dialog) -> Optional[str]:
    """Which of the market's cards this is, by what it CONTAINS. None = not one of ours.

    Order is by specificity, not by likelihood: the discard notice opens OVER the overflow
    card, so both sets of furniture are on screen at once and the innermost must win.
    """
    # The discard notice sits over the overflow card — test it FIRST or the overflow's own
    # strips claim the frame and the wrong handler answers a live choice.
    if _any(card, _UNCLAIMED):
        return DISCARD_NOTICE
    if _any(card, _RECEIVED_STRIP) and _any(card, _CARGO_STRIP):
        return OVERFLOW_PROMPT
    if _any(card, _SLOT_OVERRUN):
        return CARGO_FULL_NOTICE
    if _any(card, _RESTOCK_MARKS):
        return RESTOCK_PROMPT
    if _any(card, _RESULT_ROWS):
        return RESULT_DIALOG
    if _any(card, _NEGOTIATION_MARKS):
        return NEGOTIATION
    if _has_quantity_field(card, dialog):
        return QUANTITY_DIALOG
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
    return _any(text, _UNCLAIMED) or (_any(text, _RECEIVED_STRIP) and _any(text, _CARGO_STRIP))


def _has_choice(dialog) -> bool:
    labels = {(getattr(a, "label", "") or "").strip().lower()
              for a in (getattr(dialog, "actions", None) or ())}
    return bool(labels & {"ok", "okay", "yes", "confirm", "cancel", "no"})


def _has_quantity_field(card: str, dialog) -> bool:
    """The amount prompt: a card whose body carries an `n / owned` pair.

    Asked of `sell_goods._QTY_PAIR_RE`, which is also what READS the field — so the state and
    the action cannot disagree about whether there is one.
    """
    try:
        from actions.sell_goods import _QTY_PAIR_RE
    except Exception:                             # pragma: no cover - import guard
        return False
    for part in card.split("|"):
        if _QTY_PAIR_RE.match(part.strip()):
            return True
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
