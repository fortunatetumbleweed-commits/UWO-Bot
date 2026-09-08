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
# How many times to look again at a price this pass could not read. Perception
# varies frame to frame — the same card that parsed as a 725x364 phantom parsed
# correctly on the very next frame — so a re-read is worth more than a refusal.
_MAX_PRICE_READS = 2

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
    from vision.market_reader import sell_page_can_have_more_below

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
        names = [str(getattr(g, "name", "")) for g in wanted]
        # THE NAMES THE RESULT CARD WILL NOT CARRY. Held pending until a card confirms the
        # sale actually happened; see `MarketState.sold_pending`.
        for n in names:
            if n and n not in state.sold_pending:
                state.sold_pending.append(n)
        return {"do": "staged", "goods": names}

    # NOTHING SELLABLE IN VIEW IS NOT AN EMPTY HOLD. The grid shows one 3x3 page, so before
    # believing it: SCROLL, and look again.
    #
    # THE RULE IS THE OLD ONE, UNCHANGED — only WHO looks has moved. The sub-loop scrolled,
    # captured and compared; now the handler scrolls and hands back, the DISPATCHER captures,
    # and the next tick compares. Same test, same outcome, one tick later:
    #
    #     scroll -> look -> still nothing sellable -> the clear is finished
    #
    # And the test is SELLABILITY, not whether the page changed. An empty hold needs no
    # special case: it yields nothing sellable, scrolls once, still yields nothing, finishes.
    # NOTHING SELLABLE IS NOT THE SAME AS NOTHING ABOARD. A tile is on the Sell page because
    # we hold it, so cargo we own that this pass declined means the pass could not price it —
    # not that the hold is empty.
    #
    # Live 2026-09-06 at Lisboa, the end of an otherwise clean mission:
    #
    #     [sell] skipping 'Birch Tree' — we hold 3668 but its price is unreadable,
    #            and this pass sells on profit
    #     nothing sellable after scrolling to page 2 — the clear is finished
    #     sell done / every leg is done / status done
    #
    # The mission reported SUCCESS holding the 3,668 units it had sailed to Lisboa to sell.
    # Skipping an unpriced good is right on its own — a profit pass must not guess at a price
    # — but calling the leg finished converted one bad read into a lost cargo, and threw away
    # the retry that would have re-read it.
    #
    # CLAUDE.md, flow completeness: "a flow is complete only when it (1) ends at a recognised
    # state AND (2) contains at least one positive transaction."
    held = _cargo_this_pass_declined(goal, goods)
    if held and not state.sold:
        if not state.repeated("price_read", _MAX_PRICE_READS):
            logger.warning(f"[market] {held[0]!r} is aboard but this pass could not price it "
                           "— looking again rather than reporting the hold as empty")
            state.did("re-read the prices")
            return {"do": "waited", "why": f"{held[0]!r} aboard but unpriced"}
        return {"do": "blocked",
                "why": (f"holding {', '.join(held)} that this pass could not price — the "
                        "hold is not empty and nothing was sold")}

    # THE CLEAR IS OVER — but claim the day's award before saying so, on whichever of the
    # two paths gets here.
    #
    # THE CLAIM USED TO SIT BELOW THE SCROLL BRANCH, so it needed `scrolled_pages` to be
    # spent AND the previous tick not to have been a scroll — and the normal clear ends
    # exactly the other way round: scroll, look, nothing sellable, `last_intent == "scrolled"`,
    # finished. It was unreachable. Live 2026-09-07 at London the counter read 5,441/1,000
    # with FIVE awards pending and the chest was never tapped; the run before it, 13,894 with
    # thirteen.
    # A PAGE THAT IS NOT FULL IS THE END OF THE LIST, so there is nothing to scroll to and
    # the scroll-then-look-again round trip below buys nothing. Live 2026-09-08 at Jakarta
    # the Sell grid held two tiles and seven empty cells, and the clear still spent a swipe,
    # a capture and a tick to be told what the grid already showed.
    #
    # It is the same rule `read_market_all_pages` has always applied to a partial page; the
    # clear simply never asked. Asked of the grid's SHAPE, so one undetected tile on a full
    # shelf cannot end a clear early — see `sell_page_can_have_more_below`.
    ends_here = not sell_page_can_have_more_below(frame, omni_fn(frame))
    finished = (state.last_intent == "scrolled"
                or state.scrolled_pages >= _MAX_SELL_SCROLLS
                or ends_here)
    if finished:
        # SELLING IS WHAT EARNS THE POINTS, so this is the natural moment — and it is
        # best-effort: a claim that fails never fails the sale.
        #
        # ONE TAP AND HAND BACK. `get_trade_point_award` wrapped this in its own perceive
        # loop — up to six captures and twelve seconds, re-tapping the chest if the counter
        # had not dropped — and the counter is exactly what the reward dialog covers, so a
        # SUCCESSFUL claim looked to it like a failed one and earned a second tap through the
        # dialog. The dispatcher sees that dialog on the next tick and knows how to answer it.
        if not state.award_claimed:
            state.award_claimed = True     # one attempt per visit, whatever comes of it
            try:
                from actions.market_actions import tap_the_award_chest
                points = tap_the_award_chest(frame, tap_fn, omni_fn)
            except Exception as exc:       # noqa: BLE001 — never fail a sale over an award
                logger.debug(f"[market] trade-point award check skipped: {exc}")
                points = None
            if points is not None:
                state.did("claimed the trade-point award")
                return {"do": "waited",
                        "why": f"claiming the trade-point award ({points}/1,000)"}
        if state.last_intent == "scrolled":
            logger.info(f"[market] nothing sellable after scrolling to page "
                        f"{state.scrolled_pages + 1} — the clear is finished")
        elif ends_here:
            logger.info("[market] nothing sellable and the grid ends on this page "
                        "— the clear is finished without a scroll")
        return {"do": "finished", "why": "nothing left to sell"}

    state.scrolled_pages += 1
    _scroll()
    state.did("scrolled", page_signature(goods))
    return {"do": "scrolled", "page": state.scrolled_pages + 1}


def _cargo_this_pass_declined(goal, goods) -> list:
    """Goods we OWN that this pass did not choose — the hold's own answer to "is it empty?".

    Named goods a goal deliberately keeps (Water, Food, the barter's materials) are not
    declined, they are kept, so they must not hold the leg open forever.
    """
    keep = {str(n).strip().lower()
            for n in tuple(getattr(goal, "keep", ()) or ())
            + tuple(getattr(goal, "exclude", ()) or ())}
    out = []
    for g in goods or ():
        name = str(getattr(g, "name", "")).strip()
        if not name or name.lower() in keep:
            continue
        try:
            if int(getattr(g, "owned_qty", 0) or 0) > 0:
                out.append(name)
        except (TypeError, ValueError):
            continue
    return out


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
