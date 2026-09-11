# actions/sell_goods.py
# Goal-aware SELL, symmetric to buy_materials.purchase_goods (project_unified_
# purchase_goal_design). Perceive-act-perceive, no subloop.
#
#   goal   : {good names} to sell. None → sell everything sellable (default).
#   exclude: {good names} to PROTECT (never sell) — e.g. the barter materials we
#            still need, and supplies. Profit-aware always: losses are skipped.
#
# Selective loading via each good's tile tap (MarketGood.tap_x/tap_y) — NOT the
# "Load All" button — so a filter/exclude actually works. Then Sell → the shared
# Confirm → Result → Negotiation dialog chain.

from __future__ import annotations

import re
import time
from typing import Callable, Mapping, Optional, Sequence

from loguru import logger
from utils.digits import SEPARATORS as _SEP


def _name(g):
    return getattr(g, "name", None) or (g.get("name") if isinstance(g, dict) else None)


def _is_loss(g):
    v = getattr(g, "is_loss", None)
    if v is None and isinstance(g, dict):
        v = g.get("is_loss")
    return bool(v)


def _field(g, *names):
    """The first of `names` this good carries a value for, or None."""
    for field in names:
        v = getattr(g, field, None)
        if v is None and isinstance(g, dict):
            v = g.get(field)
        if v is not None:
            return v
    return None


def _unpriced(g) -> bool:
    """True when nothing on this tile says what it is worth.

    Checked across the fields a Sell tile can carry it in — a good read from the grid has at
    least one; a stray label has none.
    """
    return _field(g, "profit_per_unit", "sell_price", "buy_price", "price", "unit_price") is None


def _unowned(g) -> bool:
    """True when the tile does not say we hold any of it.

    THIS is what separates a good from a stray label, not the price. A good is on the SELL
    page BECAUSE we own it, so the owned count is the one field it cannot lack — whereas the
    price is just the field OmniParser is most likely to drop.
    """
    return _field(g, "owned_qty", "owned", "qty_owned") is None


def select_sellable(goods: Sequence, goal: str = "profit",
                    keep: Optional[Sequence[str]] = None,
                    only: Optional[Sequence[str]] = None,
                    exclude: Optional[Sequence[str]] = None) -> list:
    """Filter the Sell-page goods to those we should sell THIS round, per the `goal`:
      goal="profit" (default) → sell every PROFITABLE good (skip losses).  [trade default]
      goal="clear"            → sell every good REGARDLESS of profit (free cargo for barter).
    `keep` (aka legacy `exclude`) is the PROTECT list — barter materials + supplies, never sold.
    `only` is an optional whitelist — consider only these goods.  All case-insensitive.
    Returns the surviving good objects (carrying their tap targets)."""
    prot = {e.lower() for e in list(keep or []) + list(exclude or [])}
    onlyset = {g.lower() for g in only} if only else None
    out = []
    for g in goods:
        name = _name(g)
        if not name:
            continue
        low = name.lower()
        if low in prot:
            continue                          # protected good (barter material / supply)
        if onlyset is not None and low not in onlyset:
            continue                          # not in the whitelist
        if goal == "profit" and _is_loss(g):
            continue                          # profit-aware: don't sell at a loss
        # A GOOD HAS A PRICE. A tile with none is not a good that is merely unprofitable —
        # it is something else on the screen that got read as one.
        #
        # Live 2026-08-30 at Lisboa: `load-to-sell Im glad @ (730,355) (profit/u None)`. "Im
        # glad" is another player's CHAT MESSAGE, and it went into the sell basket beside the
        # Birch Tree the mission had come to sell. `goal="clear"` deliberately sells at a
        # loss, so nothing downstream was ever going to stop it — an unpriced tile and a
        # loss-making one are different things, and only the second is a decision.
        #
        # But PRICE IS THE WRONG DISCRIMINATOR, and using it cost a whole cargo. Live
        # 2026-08-30 at Lisboa the Birch Tree the mission had sailed there to sell read
        # `owned 3,622, Wares, index 100%` with its price line missing from the parse
        # entirely — OmniParser emitted no token for `13,455 (10,463)` anywhere in the frame,
        # though a tile crop reads it plainly. It was skipped as "not a good" and the mission
        # reported success having sold nothing: roughly 48M ducats left in the hold.
        #
        # What actually separates them is the OWNED COUNT. "Im glad" has a name and nothing
        # else; a good is on the Sell page because we hold it. So junk is a tile that says
        # neither what it is worth NOR that we have any.
        if _unpriced(g) and _unowned(g):
            logger.info(f"[sell] skipping {name!r} — no price and no owned count, "
                        f"so it is not a good")
            continue
        # Owned but unpriced: real cargo, one unreadable field. Whether that matters depends
        # on what THIS pass is deciding — "clear" sells regardless of profit, so it does not
        # need the price at all; "profit" does, and must not guess.
        if _unpriced(g):
            if goal == "profit":
                logger.warning(f"[sell] skipping {name!r} — we hold "
                               f"{_field(g, 'owned_qty', 'owned', 'qty_owned')} but its price "
                               f"is unreadable, and this pass sells on profit")
                continue
            logger.warning(f"[sell] {name!r}: price unreadable, selling anyway — this pass "
                           f"clears the hold regardless of profit")
        out.append(g)
    return out


def _sell_page(frame):
    """Read the Sell grid — but ONLY once the screen agrees that is what it is.

    `tab="sell"` is an ARGUMENT, not an observation: the reader parses whatever grid is on
    screen and labels it with the caller's claim, which is why every log line said `[sell]`
    while a PURCHASE grid was being read (live 2026-08-30 at Faro). Nothing downstream can
    catch that — a correctly-read tile from the wrong page looks exactly like a correctly-read
    tile from the right one, and the numbers it carries are the SHOP's stock rather than the
    fleet's hold.

    `_on_sell_tab` was written for this on 2026-08-22 and already had a test file. It simply
    was never called from the selling side. Ask it, and read nothing until it says yes.

    THE WRONG PAGE AND AN EMPTY PAGE MUST NOT BE THE SAME ANSWER. This returned `[]` for
    both, and `[]` is what the caller reads as "nothing left to sell" — so a clear that never
    reached the Sell grid reported itself finished. Live 2026-09-01 at Luanda that is exactly
    what happened, twice in one clear, and the mission then gathered into a hold it believed
    was empty. `None` means "I was not looking at the hold"; `[]` means "I was, and it holds
    nothing sellable".
    """
    from actions.buy_materials import _on_sell_tab
    from vision.market_reader import read_market_page_omni
    if not _on_sell_tab(frame):
        logger.warning("[sell] this is not the Sell grid — not reading it as the hold")
        return None
    return read_market_page_omni(frame, tab="sell")


def _find_sell_commit(frame, elements):
    """The yellow Sell commit button (detect_commit_buttons), or None."""
    try:
        from vision.region_detectors.commit_button import detect_commit_buttons
        commits = detect_commit_buttons(elements, frame)
    except Exception as exc:
        logger.debug(f"[sell] commit detect failed: {exc}")
        return None
    for c in commits:
        if "sell" in (getattr(c, "verb", "") or "").lower():
            return c
    # NO BARE `commits[0]`. A failed lookup is a REFUSAL, not a guess
    # (`a-fallback-fires-when-guessing-is-worst`), and this is a screen we MEANT to be on, so
    # the control is anchored or it is absent — CLAUDE.md: "Expected screens are
    # multi-anchored; positive-button search is for the UNEXPECTED ... with no goal the search
    # degrades to 'tap whatever looks positive'".
    #
    # It degraded exactly that way. With the basket EMPTY the real Sell button is greyed and
    # undetectable, so the only gold thing on the Lisboa page was the `Specialties` banner on
    # the Almond tile — Almond being a Lisboa specialty. This returned it, the caller read
    # "the basket is loaded", and the tap landed inside the tile: Put In Bulk staged all 1,841
    # and the next tick sold them, during a trim whose keep list named Almond.
    #
    # An empty basket has no commit button, and that is the correct answer to give.
    return None


# How many times the sell list may be scrolled looking for more to sell. A hold deep
# enough to need more than this is a bigger problem than a clear can fix in one visit.
_MAX_SELL_SCROLLS = 6


def surplus_remains(goods, keep) -> bool:
    """True when the hold still holds something the clear was meant to sell.

    COUNT THE TILES (user, 2026-08-26). A clear intends to leave exactly the kept goods —
    the recipe's materials plus Water and Food — so ANY tile beyond that set is something it
    failed to sell. Counting is an OBSERVATION and needs no scrolling to DETECT the problem,
    only to fix it.

    What this replaces: "the visible page had nothing sellable, so we are done". That is a
    CONCLUSION, and on 2026-08-26 it ended a clear after four pages of 9 goods with the rest
    of the hold untouched below the fold — the run then gathered into a hold it believed was
    empty.
    """
    kept = {(k or "").strip().lower() for k in (keep or ())}
    leftover = [g for g in (goods or [])
                if (getattr(g, "name", "") or "").strip().lower() not in kept]
    if leftover:
        logger.info(f"[sell] {len(leftover)} good(s) still aboard that the clear should have "
                    f"sold: {[getattr(g, 'name', '?') for g in leftover][:8]}")
    return bool(leftover)


def barter_materials_exclude(good: str = "Box of Nutmeg") -> list:
    """The protect list for a barter run: the recipe's input materials + supplies."""
    protect = ["Water", "Food"]
    try:
        from memory.barter_kb import load_recipe
        r = load_recipe(good)
        if r:
            protect += [i.material for i in r.inputs]
    except Exception:
        pass
    return protect


def sell_goods(port: str, goal: str = "profit", keep: Optional[Sequence[str]] = None,
               only: Optional[Sequence[str]] = None,
               exclude: Optional[Sequence[str]] = None, *,
               max_rounds: int = 6,
               capture_fn: Optional[Callable] = None,
               tap_fn: Optional[Callable] = None,
               omni_fn: Optional[Callable] = None,
               read_profits_fn: Optional[Callable] = None,
               commit_fn: Optional[Callable] = None,
               scroll_fn: Optional[Callable] = None,
               settle: float = 1.2) -> dict:
    """Goal-driven SELL — a bounded perceive→act→perceive loop (symmetric to buy_to_goal):

      goal="profit" (default) → sell every PROFITABLE good; skip losses.  [trade runs]
      goal="clear"            → sell every non-kept good regardless of profit — free cargo
                                space before a barter run.
      keep / exclude          → PROTECT list (barter materials + supplies), never sold.
      only                    → optional whitelist.

    Each round loads the selected goods, taps Sell, and handles the Confirm→Result→Negotiation
    chain.  The Sell page shows only ≤9 tiles and does NOT reflow while LOADING (a loaded tile
    grays in place) — but AFTER a commit the sold goods are removed and the rest scroll up, so the
    next round's perceive sees them.  This is why we commit per round rather than swipe: goods
    below the fold surface naturally as the ones above are sold (user 2026-08-19).  Terminates
    when a round finds nothing left to sell (goal met) or max_rounds is hit.
    Returns {ok, sold, rounds, reason}."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn
    if omni_fn is None:
        from vision.omniparser import parse_fast_cached
        omni_fn = parse_fast_cached
    if read_profits_fn is None:
        read_profits_fn = _sell_page          # confirms the grid before believing it
    if commit_fn is None:
        commit_fn = _find_sell_commit
    if scroll_fn is None:
        def scroll_fn():
            """One page down the goods grid. Goes through `ui.scroll`, which carries the
            anti-cheat jitter — a fixed-cadence swipe loop terminated the game on
            2026-08-21 (CLAUDE.md, anti-cheat tap discipline)."""
            from actions import ui
            from config.settings import MARKET_SCROLL_START, MARKET_SCROLL_END
            sx, sy_start = MARKET_SCROLL_START
            _, sy_end = MARKET_SCROLL_END
            ui.scroll(sx, sy_start, sy_end - sy_start, why="sell list, next page")

    # ACTION: switch to the Sell tab (once; it persists across rounds), and CONFIRM it.
    # `ensure_sell_tab` is the one implementation — see its docstring for what tapping the
    # calibrated point blind cost at Luanda.
    try:
        from actions.buy_materials import ensure_sell_tab
        on_sell = ensure_sell_tab(capture_fn, tap_fn, settle)
    except Exception as exc:
        logger.debug(f"[sell] sell-tab switch raised: {exc}")
        on_sell = False
    if not on_sell:
        # NOT AN EMPTY HOLD — we never got to look at it. Saying "nothing to sell" here is
        # the conclusion that sailed a full hold to Tripoli.
        logger.error(f"[{port}] could not reach the Sell grid — refusing to report the hold "
                     "as clear")
        return {"ok": False, "sold": [], "port": port,
                "reason": "could not reach the Sell grid"}

    sold: list = []
    rounds: list = []
    scrolled_pages = 0
    for r in range(max_rounds):
        # PERCEIVE the Sell page → per-good profit + tap targets.
        goods = read_profits_fn(capture_fn())
        if goods is None:
            # ONE UNREADABLE LOOK IS NOT A LOST GRID. The frame right after a sale is
            # expected to be unsettled — the result dialog has just been answered and the
            # grid is re-flowing — and capturing into that window is the mid-animation read
            # the settle waits exist for. Live 2026-09-01 at Tripoli this refused nine
            # seconds after a SUCCESSFUL sale of 4,448 Bambara Groundnut (+130M ducats) on a
            # page that reads perfectly a moment later, turning a completed clear into a
            # failed leg. Waiting for our own effect is allowed; declaring defeat on the
            # first blink is not.
            time.sleep(settle)
            goods = read_profits_fn(capture_fn())
        if goods is None:
            # STILL unreadable. A dialog, a stray tap, a tab that flipped back — the cause
            # does not matter here; what matters is that the hold is unread, and an unread
            # hold is not an empty one. Hand back rather than declare it clear.
            logger.error(f"[{port}] lost the Sell grid after selling {sold or 'nothing'} — "
                         "refusing to report the hold as clear")
            return {"ok": False, "sold": sold, "port": port,
                    "reason": "lost the Sell grid mid-clear"}
        sellable = select_sellable(goods, goal, keep, only, exclude)
        if not sellable:
            # NOTHING SELLABLE *IN VIEW* IS NOT AN EMPTY HOLD.
            #
            # The grid shows one 3x3 page. Live 2026-08-26 this break ended a clear after four
            # pages with the rest of the hold still aboard below the fold, and the run then
            # gathered into a hold it believed was empty. So before believing it: SCROLL, and
            # look again. Only a page that yields nothing new AND nothing sellable is the end.
            if scrolled_pages < _MAX_SELL_SCROLLS:
                scrolled_pages += 1
                scroll_fn()
                goods = read_profits_fn(capture_fn())
                if goods is None:
                    logger.error(f"[{port}] lost the Sell grid while scrolling — refusing to "
                                 "report the hold as clear")
                    return {"ok": False, "sold": sold, "port": port,
                            "reason": "lost the Sell grid mid-clear"}
                sellable = select_sellable(goods, goal, keep, only, exclude)
                if not sellable:
                    logger.info(f"[{port}] nothing sellable after scrolling to page "
                                f"{scrolled_pages + 1} — the clear is finished")
                    break
                logger.info(f"[{port}] scrolled to page {scrolled_pages + 1}: "
                            f"{len(sellable)} more to sell")
            else:
                break

        # ACTION: load each selected good's tile into the sell basket (selective).
        for g in sellable:
            logger.info(f"[{port}] load-to-sell {_name(g)} @ ({g.tap_x},{g.tap_y}) "
                        f"(profit/u {getattr(g, 'profit_per_unit', '?')})")
            tap_fn(g.tap_x, g.tap_y)
            time.sleep(settle)

        # PERCEIVE: the Sell commit now carries a value.
        time.sleep(settle)
        frame2 = capture_fn()
        commit = commit_fn(frame2, omni_fn(frame2))
        if commit is None:
            rounds.append({"round": r, "loaded": [_name(g) for g in sellable], "commit": None})
            return {"ok": bool(sold), "sold": sold, "rounds": rounds,
                    "reason": "no Sell button after loading basket"}

        # ACTION: Sell → PERCEIVE + REACT to the Confirm → Result → Negotiation chain.
        logger.info(f"[{port}] tap Sell @ ({commit.cx},{commit.cy})")
        from memory.observed_facts import forget
        forget("hold")   # the hold is about to change — never serve a stale one
        tap_fn(commit.cx, commit.cy)
        time.sleep(settle + 1.0)
        try:
            from actions.buy_materials import react_after_commit
            react_after_commit(capture_fn, tap_fn)
        except Exception as exc:
            logger.debug(f"[sell] post-sell react skipped: {exc}")

        batch = [_name(g) for g in sellable]
        sold += batch
        rounds.append({"round": r, "sold": batch})
        # next round re-perceives: sold goods are gone, the rest scroll up into view.

    # After selling: claim any pending Trade Point award (1 per 1,000 points — the chest badge on
    # the left-panel Trade Points widget).  Best-effort: selling is what EARNS the points, so this
    # is the natural moment; a claim failure never fails the sell.
    award = None
    try:
        from actions.market_actions import get_trade_point_award
        award = get_trade_point_award(capture_fn=capture_fn, tap_fn=tap_fn,
                                      omni_fn=omni_fn, settle=max(settle, 1.0))
        if award.get("claimed"):
            logger.info(f"[{port}] trade-point award claimed: {award.get('reason')}")
    except Exception as exc:
        logger.debug(f"[sell] trade-point award check skipped: {exc}")

    return {"ok": True, "sold": sold, "rounds": rounds, "trade_point_award": award,
            "reason": f"sold {sold}" if sold else "nothing to sell"}


# ── Sell-down-to-N (partial-quantity sell) ────────────────────────────────────
# `sell_goods` is all-or-nothing per good: it sells a good entirely or protects it via
# `keep`. That cannot free the space a barter run needs, because the surplus is INSIDE
# materials we still need — the 2026-08-20 Jakarta gather held 1,644 Textiles against 900
# needed and 1,238 Coral against 1,020, with no room for the remaining Ebony. Trimming
# each material down to its exact need freed 962 units (+6.45M ducats).
#
# Mechanics (validated live 2026-08-20): Put In Bulk must be OFF — with it ON a tile tap
# loads the WHOLE stack — then tile → Trade Goods Info dialog (`− [n / owned] +`,
# Min/Max, Cancel/Load) → tap the qty field → Enter-Number keypad → type → ↵ → Load.
#
# SAFETY MODEL: loading a basket is reversible, committing is not. Every good must pass
# through a CONFIRMED quantity dialog (see market_actions.type_quantity_on_keypad, which
# reads the typed value back before pressing ↵). If any good can't be confirmed, we abort
# WITHOUT tapping Sell — nothing has left the hold at that point. This is what stops the
# bulk-still-ON case (tile tap silently loads the full stack) from selling everything.

_QTY_PAIR_RE = re.compile(r"^\s*(\d[\d,.\']*)?\s*/\s*(\d[\d,.\']*)\s*$")


def _find_qty_field(elements, dialog_bbox=None) -> Optional[tuple]:
    """The `n / owned` quantity field in the Trade Goods Info dialog, as
    `(cx, cy, owned)` — where `owned` IS the game telling us how many we hold.
    None when the dialog isn't up.

    Scoped by `dialog_bbox`, not by a number we expect. "N / M" is not a unique shape
    here: the **Cargo bar** reads `3,040/4,952`, matches the same pattern, and sits ABOVE
    the goods, so a first-match scan finds it, and tapping it opens Set Load Ratio rather
    than a keypad (live 2026-08-21, Jakarta). The old defence was to require the
    denominator to equal what the SELL GRID said we owned — which made a grid misread able
    to veto the truth. Live 2026-08-27 at Barcelona the grid read Candle as 2148 (truly
    148), so this refused the real `1/148` field and reported "quantity dialog did not
    open" while it was plainly open. The bbox settles it without an expectation: the Cargo
    bar is OUTSIDE the dialog, so the only match inside is the good's own field.

    The DENOMINATOR alone carries the fact: it is the field's upper limit, and on the SELL
    dialog the most one can sell is the whole holding. So `/148` means we hold 148 — the
    numerator is merely how many are currently dialled in and is irrelevant here (and it is
    routinely missing: OmniParser reads the live `1/148` spinner as `/148`, the green slider
    splitting the leading digit off, which is why the numerator is optional in the pattern).

    That makes the denominator the AUTHORITY on how many we own — an observation, where the
    grid badge was only a belief. Callers should recompute from it, not check it.
    """
    if dialog_bbox is None:
        logger.warning("[sell] no dialog bbox — refusing to look for a quantity field. "
                       "Unscoped, the first 'N / M' on this page is the CARGO BAR, and "
                       "tapping it opens Set Load Ratio instead of the keypad.")
        return None
    best = None
    for e in elements or []:
        lab = (getattr(e, "label", "") or "").strip()
        m = _QTY_PAIR_RE.match(lab)
        if not m:
            continue
        cx, cy = getattr(e, "cx", None), getattr(e, "cy", None)
        if cx is None or cy is None:
            continue
        x0, y0, x1, y1 = dialog_bbox
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue              # the Cargo bar and anything else behind the scrim
        total = int(m.group(2).translate(_SEP))
        if best is None:
            best = (cx, cy, total)
    if best is None:
        logger.warning("[sell] no quantity field inside the dialog "
                       f"{dialog_bbox} — not tapping anything outside it")
    return best


def sell_down_to(port: str, keep: Mapping[str, int], *,
                 capture_fn: Optional[Callable] = None,
                 tap_fn: Optional[Callable] = None,
                 omni_fn: Optional[Callable] = None,
                 read_page_fn: Optional[Callable] = None,
                 set_bulk_fn: Optional[Callable] = None,
                 type_qty_fn: Optional[Callable] = None,
                 commit_fn: Optional[Callable] = None,
                 overlay_fn: Optional[Callable] = None,
                 react_fn: Optional[Callable] = None,
                 find_button_fn: Optional[Callable] = None,
                 ensure_sell_tab_fn: Optional[Callable] = None,
                 settle: float = 1.2) -> dict:
    """Trim each good in `keep` down to that many units, selling the excess.

    `keep` = {good name: units to KEEP} (e.g. {"Textiles": 900, "Coral": 1020}).
    Goods at or below their keep level are left alone; goods absent from `keep` are not
    touched at all — use `sell_goods(goal="clear")` for whole-good disposal.

    Returns {ok, trimmed: {good: units sold}, skipped, reason}. `ok=False` always means
    NOTHING was sold — the abort happens before the Sell commit."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn
    if overlay_fn is None:
        from vision.overlay import detect_overlay
        overlay_fn = detect_overlay
    if omni_fn is None:
        from vision.omniparser import parse_fast_cached
        omni_fn = parse_fast_cached
    if read_page_fn is None:
        read_page_fn = _sell_page             # confirms the grid before believing it
    if set_bulk_fn is None:
        from actions.market_actions import _ensure_bulk_mode
        set_bulk_fn = _ensure_bulk_mode
    if type_qty_fn is None:
        from actions.market_actions import type_quantity_on_keypad
        type_qty_fn = type_quantity_on_keypad
    if commit_fn is None:
        commit_fn = _find_sell_commit
    if react_fn is None:
        from actions.buy_materials import react_after_commit
        react_fn = react_after_commit
    if find_button_fn is None:
        from actions.sail_actions import _find_button
        find_button_fn = _find_button
    _find_button_fn = find_button_fn

    from actions.market_actions import _SELL_LOAD_BUTTON

    def _abort(reason: str, skipped, overlay=None) -> dict:
        """Bail out with NOTHING sold — and always hand the market back with bulk ON,
        or the next buy silently breaks (live 2026-08-20).

        Clear the dialog FIRST. Restoring bulk taps a checkbox on the page behind, and
        while a modal is up that point is not the checkbox — it is the modal's backdrop,
        where a tap DISMISSES rather than toggles. Live 2026-08-27 this cleanup ran under
        an open Trade Goods Info dialog, threw it away, and still logged "'Put in Bulk'
        still not ON", leaving the market in exactly the state it exists to prevent."""
        if overlay is not None and overlay.is_modal:
            _dismiss_dialog(overlay, capture_fn, tap_fn, find_button_fn, settle)
        _restore_bulk(set_bulk_fn, capture_fn, settle)
        logger.warning(f"[sell_down_to] {reason}")
        return {"ok": False, "trimmed": {}, "skipped": skipped, "reason": reason}

    def _tile_did_not_move(name: str, was: int, overlay=None) -> bool:
        """Is this good's tile still showing what it showed before we tapped it?

        THE PROOF THAT THE BASKET IS CLEAN. Staging moves a good OUT of the tile and into
        the cart — measured live: Iron 999 became 822 once its 177 surplus was staged — so a
        tile that has not moved is a tile whose tap did nothing. Unknown reads as False:
        this licenses a sale, so "cannot tell" must not mean "go ahead".

        The dialog is cleared first. Reading the page behind a modal is reading the wrong
        window, and the tap that clears it must not be aimed through one either.
        """
        if overlay is not None and getattr(overlay, "is_modal", False):
            _dismiss_dialog(overlay, capture_fn, tap_fn, find_button_fn, settle)
        try:
            page = read_page_fn(capture_fn()) or []
        except Exception as exc:              # noqa: BLE001 — a poorer read, not a broken one
            logger.debug(f"[sell_down_to] could not re-read the grid: {exc}")
            return False
        for other in page:
            if str(getattr(other, "name", "")).strip().lower() == name.strip().lower():
                now = getattr(other, "owned_qty", None)
                return now is not None and int(now) == int(was)
        return False                          # not on the page — nothing is proven

    # DECIDE FIRST, TOUCH NOTHING YET. The Sell page's centre grid IS the cargo hold, so
    # whether anything is over-stocked is answerable from the frame already on screen. When
    # nothing is, we leave without a single tap. Live 2026-08-22 the old order toggled
    # Put In Bulk OFF and straight back ON around a trim that then reported
    # "trimmed {} (nothing to trim)" — two taps and 20 seconds to change nothing.
    # SWITCH TO THE SELL TAB, AND CONFIRM IT. `sell_goods` has always done this and
    # `_read_owned_via_sell` refuses without it; the TRIM path alone just read whatever grid
    # was on screen. Live 2026-09-04 at Madeira it read the PURCHASE page (frame 277 of
    # trace_barter_cmd_2026-09-04T17-35-01 — title "Purchase", the shop's stock in the middle,
    # the fleet's 3,237 Pig sitting in the panel on the right):
    #
    #     [Madeira] nothing over-stocked — leaving the market untouched (hold {})
    #     trim skipped at Madeira: Raisin: not on the sell page
    #     trim skipped at Madeira: Pig: not on the sell page
    #
    # It then sailed to the village with the whole 1,476-unit Pig surplus aboard.
    _ensure_tab = ensure_sell_tab_fn
    if _ensure_tab is None:
        from actions.buy_materials import ensure_sell_tab as _ensure_tab
    try:
        on_sell = _ensure_tab(capture_fn, tap_fn, settle)
    except Exception as exc:
        logger.debug(f"[sell_down_to] sell-tab switch raised: {exc}")
        on_sell = False
    if not on_sell:
        return _abort("could not reach the Sell grid — refusing to report the hold as "
                      "trimmed", [f"{n}: the Sell grid was never reached" for n in keep])

    # None IS NOT AN EMPTY HOLD. `_sell_page` returns None for "I was not looking at the
    # hold" and [] for "I was, and it holds nothing" — a distinction it documents at length,
    # having been given it on 2026-09-01 for exactly this failure. `or []` threw it away one
    # call later, so a wrong-page read became "nothing to trim" and the leg reported ok.
    page = read_page_fn(capture_fn())
    if page is None:
        return _abort("the Sell grid could not be read — refusing to report the hold as "
                      "trimmed", [f"{n}: the Sell grid could not be read" for n in keep])
    goods = {_name(g).lower(): g for g in page}
    owned_now = {n: int(g.owned_qty) for n, g in goods.items()
                 if getattr(g, "owned_qty", None) is not None}
    over_stocked, skipped = [], []
    for name, keep_qty in keep.items():
        g = goods.get(name.lower())
        if g is None:
            skipped.append(f"{name}: not on the sell page")
        elif getattr(g, "owned_qty", None) is None:
            skipped.append(f"{name}: owned quantity unreadable")   # never guess what we SELL
        elif int(g.owned_qty) <= int(keep_qty):
            skipped.append(f"{name}: {g.owned_qty} ≤ keep {keep_qty}")
        else:
            over_stocked.append(name)
    if not over_stocked:
        logger.info(f"[{port}] nothing over-stocked — leaving the market untouched "
                    f"(hold {owned_now})")
        return {"ok": True, "trimmed": {}, "skipped": skipped, "owned": owned_now,
                "reason": "nothing to trim"}

    # Bulk OFF, or a tile tap loads the whole stack instead of opening the dialog.
    set_bulk_fn(False, capture_fn())
    time.sleep(settle)

    trimmed = {}
    for name in over_stocked:
        g = goods[name.lower()]
        owned = int(g.owned_qty)
        excess = owned - int(keep[name])

        # keep[name], NOT keep_qty: that name is the loop variable from the decide-first
        # pass above and still holds the LAST good's figure. The arithmetic was always
        # right; only the message lied — live 2026-08-27 it printed "trim Candle: own 2148,
        # keep 61 → sell 2046" when Candle's keep was 102 (61 was Matchlock Gun's), which
        # sent a debugging session chasing a trim target that was never used.
        logger.info(f"[{port}] trim {name}: own {owned}, keep {keep[name]} → sell {excess}")
        tap_fn(g.tap_x, g.tap_y)
        time.sleep(settle)

        # The dialog is the PROOF that bulk is really off. No dialog ⇒ either the tap
        # bulk-loaded the whole stack or it missed — both mean stop, not "tap on anyway
        # at the coordinates we remember".
        # Pass what we own so the good's field is told apart from the Cargo bar, which is
        # the same "N / M" shape and sits above it.
        dlg_frame = capture_fn()
        overlay = overlay_fn(dlg_frame)
        qty_field = _find_qty_field(omni_fn(dlg_frame), dialog_bbox=overlay.bbox)
        if qty_field is None:
            # A DROPPED TAP IS THE LIKELIEST CAUSE, and the game drops about one in twenty.
            # Re-tap the SAME tile once — this waits for our own effect on the same screen,
            # which is the one thing a primitive's loop may do.
            logger.info(f"[{port}] {name}: no quantity dialog — re-tapping the tile once")
            tap_fn(g.tap_x, g.tap_y)
            time.sleep(settle)
            dlg_frame = capture_fn()
            overlay = overlay_fn(dlg_frame)
            qty_field = _find_qty_field(omni_fn(dlg_frame), dialog_bbox=overlay.bbox)

        if qty_field is None:
            # COMMIT WHAT WAS CONFIRMED RATHER THAN THROWING IT AWAY.
            #
            # The all-or-nothing abort exists for a real danger: with Put In Bulk still ON a
            # tile tap loads the WHOLE stack, and selling that dumps materials the barter
            # needs. But that danger is about THIS good, and it is observable — a tap that
            # loaded the stack MOVES the tile's count, while a dropped tap leaves it exactly
            # where it was. Every good already in the basket passed a confirmed quantity
            # dialog, so committing them is precisely as safe as the full commit would be.
            #
            # Live 2026-09-06 at Tripoli: Candle 211 and Iron 177 staged correctly — the
            # exact surpluses, tiles reading 709 and 822 — then Matchlock Gun's dialog did
            # not open and the whole basket was abandoned, 205,013 ducats and one tap from
            # Sell. That is the 2026-08-22 loss repeating ("981 Ebony loaded, Sell one tap
            # away, thrown away"), and this file's own safety note is what licenses the
            # narrower rule: the model is per-good confirmation, not per-basket.
            if trimmed and _tile_did_not_move(name, owned, overlay):
                logger.warning(f"[{port}] {name}: quantity dialog would not open and its "
                               f"tile is untouched — skipping it and selling the "
                               f"{len(trimmed)} good(s) already confirmed")
                skipped.append(f"{name}: quantity dialog would not open")
                continue
            return _abort(f"{name}: quantity dialog did not open (Put In Bulk still ON?) "
                          "— aborted before selling anything", skipped, overlay=overlay)

        # THE DIALOG IS THE AUTHORITY. Its denominator is the game stating how many we
        # hold; the grid badge was our reading of a 40px overlay, and on 2026-08-27 that
        # overlay's melted-wax artwork OCR'd as a leading digit — 148 became 2148, at
        # confidence 0.65 against OmniParser's 0.9987. Recompute rather than verify: an
        # observation is available, so no belief should survive here.
        qx, qy, dialog_owned = qty_field
        if dialog_owned != owned:
            logger.warning(f"[{port}] {name}: the grid read {owned} but the dialog shows "
                           f"{dialog_owned} — trusting the dialog and re-deriving the trim")
            owned = dialog_owned
            excess = owned - int(keep[name])
            if excess <= 0:
                _dismiss_dialog(overlay, capture_fn, tap_fn, find_button_fn, settle)
                skipped.append(f"{name}: {owned} ≤ keep {keep[name]} (per the dialog)")
                continue
        tap_fn(qx, qy)
        time.sleep(settle)

        if not type_qty_fn(excess, capture_fn=capture_fn, tap_fn=tap_fn):
            return _abort(f"{name}: could not confirm the typed quantity {excess} "
                          "— aborted before selling anything", skipped,
                          overlay=overlay_fn(capture_fn()))

        load = _find_button_fn(capture_fn(), "load") or _SELL_LOAD_BUTTON
        tap_fn(*load)
        time.sleep(settle)
        # DID THE DIALOG CLOSE? Ask the scrim, which lifts with it — do not infer it from
        # an "N / M" match on the page behind. Unscoped, that match was the Cargo bar
        # ("3,823/4,108"), and live 2026-08-22, right after "Keypad: 981 entered and
        # confirmed", this aborted with "quantity dialog still open after Load" when Load
        # had in fact worked. Guarding it with the owned count only moved the failure: the
        # count itself can be misread (Candle 2148 for 148, live 2026-08-27). With no modal
        # up there is nothing to search, so the Cargo bar cannot be mistaken for the field.
        after = capture_fn()
        overlay_after = overlay_fn(after)
        if (overlay_after.is_modal
                and _find_qty_field(omni_fn(after), dialog_bbox=overlay_after.bbox)):
            # A DIALOG STILL OPEN IS PROOF THE LOAD DID NOT HAPPEN, which is what makes ONE
            # re-tap safe: a Load that had worked would have closed it, so this cannot load
            # the amount twice. The usual cause is the tap the game drops about once in
            # twenty, and re-tapping the same control on the same screen is waiting for our
            # own effect — the one loop a primitive may have.
            #
            # Live 2026-09-06 at Madeira, frame 400 of trace_barter_cmd_2026-09-06T21-45-01:
            # the Trade Goods Info card open for Pig, the field reading a correct `73 / 1,828`
            # (1,828 aboard less 1,755 kept), and the tap at (1313,943) dead on Load. Four
            # seconds later the card was unchanged. The trim aborted with "the amount may not
            # be in the basket" and the whole surplus sailed on unsold.
            logger.info(f"[{port}] {name}: the quantity dialog is still open — re-tapping "
                        "Load once; it cannot have loaded and stayed open")
            tap_fn(*load)
            time.sleep(settle)
            after = capture_fn()
            overlay_after = overlay_fn(after)
            if (overlay_after.is_modal
                    and _find_qty_field(omni_fn(after), dialog_bbox=overlay_after.bbox)):
                return _abort(f"{name}: quantity dialog still open after Load — the amount "
                              "may not be in the basket; aborted before selling anything",
                              skipped, overlay=overlay_after)
        trimmed[name] = excess

    if not trimmed:
        _restore_bulk(set_bulk_fn, capture_fn, settle)
        return {"ok": True, "trimmed": {}, "skipped": skipped, "owned": owned_now,
                "reason": "nothing to trim"}

    frame = capture_fn()
    commit = commit_fn(frame, omni_fn(frame))
    if commit is None:
        return _abort("no Sell button after loading the basket — nothing committed",
                      skipped)
    logger.info(f"[{port}] tap Sell @ ({commit.cx},{commit.cy}) for {trimmed}")
    from memory.observed_facts import forget
    forget("hold")   # the hold is about to change — never serve a stale one
    tap_fn(commit.cx, commit.cy)
    time.sleep(settle + 1.0)
    try:
        react_fn(capture_fn, tap_fn)
    except Exception as exc:
        logger.debug(f"[sell_down_to] post-sell react skipped: {exc}")

    _restore_bulk(set_bulk_fn, capture_fn, settle)
    # What we owned BEFORE the trim, less what we just sold — the caller needs the hold,
    # and re-reading it would mean navigating back to a page we are about to leave.
    sold = {k.lower(): v for k, v in trimmed.items()}     # grid keys are lower-cased
    after = {n: q - int(sold.get(n, 0)) for n, q in owned_now.items()}
    return {"ok": True, "trimmed": trimmed, "skipped": skipped, "owned": after,
            "reason": f"trimmed {trimmed}"}


def _dismiss_dialog(overlay, capture_fn, tap_fn, find_button_fn, settle: float) -> None:
    """Close an open dialog through its OWN control (Cancel / Close), never by tapping
    outside it. An outside tap does dismiss a dialog the bot opened — but it is the same
    gesture as a misfire, so using it deliberately makes the two indistinguishable in the
    log, and it does nothing at all for a game-pushed popup."""
    try:
        frame = capture_fn()
        for label in ("cancel", "close"):
            pos = find_button_fn(frame, label)
            if pos and not overlay.blocks(*pos):
                logger.info(f"[sell_down_to] closing the dialog via {label!r} @ {pos}")
                tap_fn(*pos)
                time.sleep(settle)
                return
        logger.warning("[sell_down_to] dialog is up but no Cancel/Close found inside it — "
                       "leaving it open rather than tapping outside")
    except Exception as exc:
        logger.warning(f"[sell_down_to] could not close the dialog: {exc}")


def _restore_bulk(set_bulk_fn, capture_fn, settle: float) -> None:
    """Put 'Put In Bulk' back ON — the BUY flow silently breaks with it OFF (a tile tap
    opens the qty dialog instead of bulk-loading, and purchase_goods leaves goods
    uncommitted; live 2026-08-20). Never leave the market in the trim state."""
    try:
        set_bulk_fn(True, capture_fn())
        time.sleep(settle)
    except Exception as exc:
        logger.warning(f"[sell_down_to] could not restore Put In Bulk: {exc}")
