"""Selling, as one action per tick.

The same steps `sell_goods` takes, with the waiting given back to the dispatcher. Nothing
here is new work: `_sell_page` reads the grid, `select_sellable` chooses, `_find_sell_commit`
finds the button, `ensure_sell_tab` switches the tab. They are called from handlers instead
of from a `for r in range(max_rounds)`.

WHAT THE LOOP COST, and why this exists (FC-4, `docs/market_as_contexts.md`). At London, with
4,461 Bambara Groundnut aboard at 39,642 profit/unit:

    tap (1450,314)   "load-to-sell Bambara Groundnut"     <- the tap did not register
    sold at London: nothing
    blocked: 'no Sell button after loading basket'        <- the leg failed here

The aim was right — (1451,316) sold Argan Oil on the same screen an hour earlier. The tap
was swallowed, which the game does about once in twenty. And the proof was on the frame the
loop already held: the right-hand panel read *"Select the goods you'd like to sell."* The
loop did not look. It went hunting for a Sell button that only exists once something IS
staged, and reported the missing button as the failure — three steps from the cause.

So the shape here is: STAGE, then hand back. The next tick reads the panel and either sees a
loaded basket (commit) or an empty one (the tap was swallowed — stage again, bounded). No
step assumes its own effect.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from loguru import logger

# ONE RE-TAP, then report. That is the documented remedy for a swallowed tap
# (`memory/the-sell-tab-is-where-the-hold-lives`: "Retry once, on a re-read position"), and
# the number the village already uses (`village._MAX_SELECT_ATTEMPTS`). A second identical
# page says the control is not responding, which is a fact for the mission rather than
# something to grind at.
_MAX_STAGE_ATTEMPTS = 1
# The grid is one 3x3 page. Nothing sellable IN VIEW is not an empty hold — scroll and look.
_MAX_SELL_SCROLLS = 4

# The panel's own words when the basket holds nothing. This is the ONLY safe licence to tap a
# tile again, and the reason is in `purchase_goods`:
#
#     TAPPING A STAGED TILE UN-STAGES IT. So when the commit detector failed — it was
#     rejecting a lit Purchase button at 0.279787 against a 0.28 cut — this "retry" toggled a
#     cart that had been correctly filled on the first tap.
#
# "No commit button" has TWO causes: the tap was swallowed, or the button is there and the
# detector missed it. Re-staging on that reading destroys a correct basket in the second
# case. The empty-cart message has one cause, and it is the very evidence FC-4 recorded as
# on-screen and unread.
_CART_EMPTY = ("select the goods",)


def selection_for(goal: Any, goods: Sequence) -> list:
    """The goods this goal wants sold, from a page reading. The existing chooser."""
    from actions.sell_goods import select_sellable
    kind = type(goal).__name__
    if kind == "FreeHold":
        return list(select_sellable(goods, "clear", list(getattr(goal, "keep", ()) or ()),
                                    None, None))
    return list(select_sellable(goods, "profit", None, None,
                                list(getattr(goal, "exclude", ()) or ())))


def page_signature(goods: Sequence) -> tuple:
    """A comparable reading of the sell page.

    THE SIGNATURE IS HOW A SWALLOWED TAP IS SEEN. `barter_panel._try` compares one of these
    across a re-read inside a loop; here the comparison spans two ticks, which is the whole
    point — a dialog arriving in between is SEEN rather than tapped over.
    """
    return tuple(sorted((str(getattr(g, "name", "")), int(getattr(g, "owned_qty", 0) or 0))
                        for g in goods or ()))


def on_sell_page(state, goal, *, frame, capture_fn, tap_fn, omni_fn) -> dict:
    """One action on the sell grid. Returns a dict the activity turns into a result.

    Exactly one of: staged / committed / scrolled / finished / blocked.
    """
    from actions.sell_goods import _find_sell_commit, _sell_page

    goods = _sell_page(frame)
    if goods is None:
        # NOT AN EMPTY HOLD — we did not manage to look at one. `None` and `[]` are different
        # answers and collapsing them is what sailed a full hold to Tripoli.
        return {"do": "blocked", "why": "could not read the Sell grid"}

    commit = _find_sell_commit(frame, omni_fn(frame))
    if commit is not None:
        # SOMETHING IS STAGED — the button carries a value, which is the screen's own answer
        # to "is the basket loaded?". Nothing is remembered about it; it is read every tick.
        state.landed("stage")
        logger.info(f"[market] the basket is loaded — committing")
        # THE HOLD IS ABOUT TO CHANGE. Drop the remembered one before the tap, never after:
        # a cached hold served between the tap and the next read is a stale answer to the
        # question the whole leg turns on.
        try:
            from memory.observed_facts import forget
            forget("hold")
        except Exception as exc:                  # never fail a sale over bookkeeping
            logger.debug(f"[market] could not forget the hold: {exc}")
        tap_fn(commit.cx, commit.cy)
        state.did("tapped Sell", page_signature(goods))
        return {"do": "committed"}

    wanted = selection_for(goal, goods)
    if wanted:
        # A STAGING TAP THAT CHANGED NOTHING IS A SWALLOWED TAP — but only the panel saying
        # the cart is EMPTY licenses tapping again, because a second tap on a staged tile
        # un-stages it. A missing commit button is not enough: the detector may simply have
        # missed one that is there.
        if state.last_intent == "staged" and state.last_signature == page_signature(goods):
            if not _cart_is_empty(frame, omni_fn):
                # Staged, probably, and the commit control was not found. Do NOT tap — hand
                # back and let the next tick look with fresh eyes. One honest refusal beats
                # three destructive taps (`purchase_goods`, 2026-08-30).
                logger.info("[market] the basket does not read as empty but no Sell button "
                            "was found — looking again rather than re-tapping, which would "
                            "un-stage it")
                if state.repeated("commit_lookup", _MAX_STAGE_ATTEMPTS):
                    return {"do": "blocked",
                            "why": "goods appear staged but no Sell button can be found"}
                return {"do": "waited"}
            if state.repeated("stage", _MAX_STAGE_ATTEMPTS):
                return {"do": "blocked",
                        "why": (f"staged {len(wanted)} good(s) {_MAX_STAGE_ATTEMPTS + 1}x and "
                                "the cart stayed empty — the tile is not taking taps")}
            logger.warning("[market] the cart reads EMPTY after staging — that tap did not "
                           "register; staging again")
        for g in wanted:
            logger.info(f"[market] stage {getattr(g, 'name', '?')} @ "
                        f"({g.tap_x},{g.tap_y}) (profit/u "
                        f"{getattr(g, 'profit_per_unit', '?')})")
            tap_fn(g.tap_x, g.tap_y)
        state.did("staged", page_signature(goods))
        return {"do": "staged", "goods": [str(getattr(g, "name", "")) for g in wanted]}

    # NOTHING SELLABLE IN VIEW. One 3x3 page is not the hold.
    if state.scrolled_pages < _MAX_SELL_SCROLLS:
        state.scrolled_pages += 1
        _scroll()
        state.did("scrolled", page_signature(goods))
        return {"do": "scrolled", "page": state.scrolled_pages + 1}
    # SELLING IS WHAT EARNS THE POINTS, so this is the natural moment to claim the award —
    # and it is best-effort: a claim that fails never fails the sale.
    award = None
    try:
        from actions.market_actions import get_trade_point_award
        award = get_trade_point_award(capture_fn=capture_fn, tap_fn=tap_fn, omni_fn=omni_fn)
        if (award or {}).get("claimed"):
            logger.info(f"[market] trade-point award claimed: {award.get('reason')}")
    except Exception as exc:
        logger.debug(f"[market] trade-point award check skipped: {exc}")
    return {"do": "finished", "why": "nothing left to sell", "trade_point_award": award}


def _cart_is_empty(frame, omni_fn) -> bool:
    """Does the panel SAY the basket is empty? Unknown reads as False — not empty.

    Erring towards "something may be staged" is the safe direction: the cost of being wrong
    is one wasted tick, against un-staging a correct basket.
    """
    try:
        labels = [(getattr(e, "label", "") or "").strip().lower() for e in omni_fn(frame) or []]
        return any(any(m in l for m in _CART_EMPTY) for l in labels)
    except Exception as exc:
        logger.debug(f"[market] could not read the cart: {exc}")
        return False


def _scroll() -> None:
    """One page down, through `ui.scroll` so it carries the anti-cheat jitter."""
    from actions import ui
    from config.settings import MARKET_SCROLL_END, MARKET_SCROLL_START
    sx, sy_start = MARKET_SCROLL_START
    _, sy_end = MARKET_SCROLL_END
    ui.scroll(sx, sy_start, sy_end - sy_start, why="sell list, next page")
