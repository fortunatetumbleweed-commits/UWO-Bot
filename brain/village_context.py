"""Which VILLAGE screen is this? — the context-scoped read for the village activity.

The outer `classify_nav_state` answers "which of the forty screens is this?" and returns
`village` both before AND after the barter submenu closes. That verdict is correct and
useless: the two screens need different handling and it cannot tell them apart.

This answers the scoped question instead — *which of the village's own screens is this?* —
and returns MISS when the answer is none of them. A miss is not an error; it is the normal
signal that the context hypothesis has expired and the outer dispatcher should route
(CLAUDE.md Guiding Principles #0 and #1).

WHY THE STATES ARE READ AND NOT INFERRED (Principle #3). A dead Exchange button has three
causes and the code used to enumerate one. Here each is a separate state with its own
evidence:

    barter_panel_blocked   panel open, Exchange grey   -> rounds REMAIN, something is short
    village_top_menu       the submenu is GONE         -> the day's rounds are spent
    barter_panel_ready     panel open, Exchange live   -> commit

A grey Exchange and a closed submenu are different PIXELS, not different inferences. Live
2026-08-27 the rounds ran out, the game closed the submenu, and the missing distinction cost
the run: "the panel must be stale" led into a refresh path that had never executed.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

MISS = "miss"

VILLAGE_TOP_MENU = "village_top_menu"
BARTER_PANEL_NO_GOOD = "barter_panel_no_good"
BARTER_PANEL_READY = "barter_panel_ready"
BARTER_PANEL_BLOCKED = "barter_panel_blocked"
EXCHANGE_CONFIRM = "exchange_confirm"
BARTER_RESULT = "barter_result"
OVERFLOW_PROMPT = "overflow_prompt"

CONTEXT_STATES = (
    VILLAGE_TOP_MENU,
    BARTER_PANEL_NO_GOOD,
    BARTER_PANEL_READY,
    BARTER_PANEL_BLOCKED,
    EXCHANGE_CONFIRM,
    BARTER_RESULT,
    OVERFLOW_PROMPT,
)

# The left menu identifies the village interior. `perceive` already matches on this vocab.
_VILLAGE_MENU_WORDS = ("barter", "gifting", "loot", "recruit")

# Our own dialogs, by their titles. Both are raised BY THE BOT'S OWN TAP, so they are handled
# here and completed — never dismissed (CLAUDE.md, dialog handling).
_CONFIRM_TITLES = ("barter calculations",)
_RESULT_TITLES = ("exchange complete", "barter result", "acquired")
_OVERFLOW_WORDS = ("overflow", "exceeds", "cargo is full")
# The panel's own prompt when it is OPEN with nothing chosen. It has no `good` and no `out`
# at that moment, so the good/out test below reads it as CLOSED — which is exactly the
# assumption Guiding Principle #3 warns about: one signal treated as THE signal. The prompt
# is the panel saying "I am open and waiting", and it is the only evidence for that state.
_SELECT_PROMPT = ("select trade good",)


def classify(frame, *, elements=None, panel=None, exchange_live=None) -> str:
    """The village-scoped state, or MISS.

    Everything is INJECTABLE because the defaults capture and parse; a unit test that reaches
    for the device is a slow test, and a slow test is evidence (see VillageActivity).

    `panel` is a `BarterPanelReading` or None; `exchange_live` is a bool or None (unknown).
    """
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
    labels = [(getattr(e, "label", "") or "").strip().lower() for e in elements or []]
    text = " | ".join(l for l in labels if l)

    # OUR OWN DIALOGS FIRST. They sit ON TOP of the panel, so reading the panel underneath and
    # concluding "ready" would tap Exchange through a dialog.
    if any(t in text for t in _RESULT_TITLES):
        return BARTER_RESULT
    if any(t in text for t in _CONFIRM_TITLES):
        return EXCHANGE_CONFIRM
    if any(w in text for w in _OVERFLOW_WORDS):
        return OVERFLOW_PROMPT

    # THE RAW READING, not the derived state. `_read_panel_state()` returns a
    # `PanelBarterState` — rounds/binding/shortfall computed FROM the materials — which has
    # no notion of which good is selected or whether a panel is up at all. Reading that and
    # asking it for `good` gave None every time, so the context said "top menu" while the
    # panel was plainly open, and the activity re-opened it for ever (live 2026-08-28).
    if panel is None and frame is not None:
        from actions.barter_reader import read_barter_panel
        try:
            panel = read_barter_panel(frame)
        except Exception as exc:
            logger.debug(f"[village-context] panel unreadable: {exc}")
            panel = None

    if any(t in text for t in _SELECT_PROMPT):
        return BARTER_PANEL_NO_GOOD

    if _panel_is_up(panel):
        if exchange_live is None:
            # THE FRAME WE WERE GIVEN, not another capture. Re-shooting here asks the panel
            # a question about a screen the caller has not seen (per-frame perception
            # sharing), and costs a capture+parse on every village tick.
            exchange_live = _read_exchange_live(frame)
        if not selected_good(panel):
            # AN UNREADABLE NAME IS NOT AN EMPTY PANEL, and a live Exchange proves it.
            #
            # The game says "Select Trade Good." when nothing is chosen, and that prompt is
            # tested ABOVE — so reaching here means the game did NOT say it. All that is
            # missing is our reading of the name, and the button is the game's own verdict on
            # whether the selected good can be traded (`village._why_it_stopped`: "a greyed
            # Exchange means done... a live Exchange means there is more to do").
            #
            # Live 2026-09-05 at Berber this looped without end. Argan Oil's name would not
            # OCR on any frame, so `_select_trade_good` selected it, answered "selected Argan
            # Oil" — via the same Exchange rule, one layer down — and then the very next tick
            # classified the panel as NO_GOOD again and threw the selection away. Six taps a
            # minute, no rounds, for as long as it was left running.
            return BARTER_PANEL_READY if exchange_live else BARTER_PANEL_NO_GOOD
        return BARTER_PANEL_READY if exchange_live else BARTER_PANEL_BLOCKED

    # No panel. Are we still in the village at all? The left menu says so — and this is the
    # screen the game drops us on when the day's rounds run out.
    if sum(1 for w in _VILLAGE_MENU_WORDS if w in text) >= 2:
        return VILLAGE_TOP_MENU

    return MISS


def selected_good(panel) -> Optional[str]:
    """The good the panel is showing. `BarterPanelReading.selected_good` is the real field —
    `good` and `out` were taken from the LOG'S formatting rather than the dataclass, and the
    stubs enshrined the mistake so the tests agreed with it."""
    if panel is None:
        return None
    return getattr(panel, "selected_good", None) or getattr(panel, "good", None)


def _panel_is_up(panel) -> bool:
    """The panel names a good and an output while it is open; both go when the game closes
    it. Amity does NOT count — it is drawn on the village screen behind the submenu."""
    if panel is None:
        return False
    out = getattr(panel, "output_quantity", None)
    if out is None:
        out = getattr(panel, "out", None)
    return bool(selected_good(panel)) or out is not None


def _read_exchange_live(frame=None) -> Optional[bool]:
    try:
        from actions.barter_panel import _exchange_still_live
        return bool(_exchange_still_live(frame))
    except Exception as exc:
        logger.debug(f"[village-context] could not read the Exchange button: {exc}")
        return None
