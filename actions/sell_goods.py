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


def _name(g):
    return getattr(g, "name", None) or (g.get("name") if isinstance(g, dict) else None)


def _is_loss(g):
    v = getattr(g, "is_loss", None)
    if v is None and isinstance(g, dict):
        v = g.get("is_loss")
    return bool(v)


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
        out.append(g)
    return out


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
    return commits[0] if commits else None


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
        from vision.market_reader import read_market_page_omni
        read_profits_fn = lambda f: read_market_page_omni(f, tab="sell")   # bbox reader: name+price+profit+is_loss+tap
    if commit_fn is None:
        commit_fn = _find_sell_commit

    # ACTION: switch to the Sell tab (once; it persists across rounds).
    try:
        from actions.market_actions import MARKET_COORDS
        tap_fn(*MARKET_COORDS["sell"])
        time.sleep(settle)
    except Exception as exc:
        logger.debug(f"[sell] sell-tab tap skipped: {exc}")

    sold: list = []
    rounds: list = []
    for r in range(max_rounds):
        # PERCEIVE the Sell page → per-good profit + tap targets.
        goods = read_profits_fn(capture_fn())
        sellable = select_sellable(goods, goal, keep, only, exclude)
        if not sellable:
            break                         # goal met — nothing left to sell (only kept goods)

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

_QTY_PAIR_RE = re.compile(r"^\s*(\d[\d,]*)\s*/\s*(\d[\d,]*)\s*$")


def _find_qty_field(elements, expect_total: Optional[int] = None) -> Optional[tuple]:
    """The `n / owned` quantity field in the Trade Goods Info dialog — tapping it opens
    the keypad. Detected, not hardcoded; None when the dialog isn't up.

    `expect_total` is how many of the good we OWN, i.e. the denominator the real field must
    show. Pass it: "N / M" is not a unique shape on this screen. The **Cargo bar** reads
    `3,823/4,108` and matches the same pattern, and it sits ABOVE the goods, so a
    first-match scan finds the Cargo bar instead. Tapping that opens Set Load Ratio — not a
    keypad — and the digit detector then fails against a dialog that has no digits to find.

    Live 2026-08-21, Jakarta: `sell_down_to` computed "own 1681, keep 700 → sell 981"
    correctly, tapped what it thought was the quantity field, and aborted with
    "Keypad: digit '8' not detected". The trim never ran and the hold stayed at 3823/4108.
    """
    fallback = None
    for e in elements or []:
        lab = (getattr(e, "label", "") or "").strip()
        m = _QTY_PAIR_RE.match(lab)
        if not m:
            continue
        cx, cy = getattr(e, "cx", None), getattr(e, "cy", None)
        if cx is None or cy is None:
            continue
        total = int(m.group(2).replace(",", ""))
        if expect_total is not None and total == int(expect_total):
            return (cx, cy)                      # the good's own field — unambiguous
        if fallback is None:
            fallback = (cx, cy, total)

    if expect_total is not None:
        # Better to report "not found" than to tap the Cargo bar and open the wrong dialog.
        logger.warning(f"[sell] no quantity field showing '… / {expect_total}' — "
                       f"refusing to tap {fallback[:2] if fallback else None} "
                       f"(total {fallback[2] if fallback else '?'}), which is not this good")
        return None
    return fallback[:2] if fallback else None


def sell_down_to(port: str, keep: Mapping[str, int], *,
                 capture_fn: Optional[Callable] = None,
                 tap_fn: Optional[Callable] = None,
                 omni_fn: Optional[Callable] = None,
                 read_page_fn: Optional[Callable] = None,
                 set_bulk_fn: Optional[Callable] = None,
                 type_qty_fn: Optional[Callable] = None,
                 commit_fn: Optional[Callable] = None,
                 react_fn: Optional[Callable] = None,
                 find_button_fn: Optional[Callable] = None,
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
    if omni_fn is None:
        from vision.omniparser import parse_fast_cached
        omni_fn = parse_fast_cached
    if read_page_fn is None:
        from vision.market_reader import read_market_page_omni
        read_page_fn = lambda f: read_market_page_omni(f, tab="sell")
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

    def _abort(reason: str, skipped) -> dict:
        """Bail out with NOTHING sold — and always hand the market back with bulk ON,
        or the next buy silently breaks (live 2026-08-20)."""
        _restore_bulk(set_bulk_fn, capture_fn, settle)
        logger.warning(f"[sell_down_to] {reason}")
        return {"ok": False, "trimmed": {}, "skipped": skipped, "reason": reason}

    # DECIDE FIRST, TOUCH NOTHING YET. The Sell page's centre grid IS the cargo hold, so
    # whether anything is over-stocked is answerable from the frame already on screen. When
    # nothing is, we leave without a single tap. Live 2026-08-22 the old order toggled
    # Put In Bulk OFF and straight back ON around a trim that then reported
    # "trimmed {} (nothing to trim)" — two taps and 20 seconds to change nothing.
    goods = {_name(g).lower(): g for g in (read_page_fn(capture_fn()) or [])}
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

        logger.info(f"[{port}] trim {name}: own {owned}, keep {keep_qty} → sell {excess}")
        tap_fn(g.tap_x, g.tap_y)
        time.sleep(settle)

        # The dialog is the PROOF that bulk is really off. No dialog ⇒ either the tap
        # bulk-loaded the whole stack or it missed — both mean stop, not "tap on anyway
        # at the coordinates we remember".
        # Pass what we own so the good's field is told apart from the Cargo bar, which is
        # the same "N / M" shape and sits above it.
        qty_field = _find_qty_field(omni_fn(capture_fn()), expect_total=int(owned))
        if qty_field is None:
            return _abort(f"{name}: quantity dialog did not open (Put In Bulk still ON?) "
                          "— aborted before selling anything", skipped)
        tap_fn(*qty_field)
        time.sleep(settle)

        if not type_qty_fn(excess, capture_fn=capture_fn, tap_fn=tap_fn):
            return _abort(f"{name}: could not confirm the typed quantity {excess} "
                          "— aborted before selling anything", skipped)

        load = _find_button_fn(capture_fn(), "load") or _SELL_LOAD_BUTTON
        tap_fn(*load)
        time.sleep(settle)
        # Pass what we OWN here too. Without it this matched the Cargo bar ("3,823/4,108")
        # on the sell page behind the closed dialog and concluded the dialog was still
        # open — live 2026-08-22, right after "Keypad: 981 entered and confirmed", it
        # aborted with "quantity dialog still open after Load" when Load had worked.
        # While the dialog IS open it shows "<staged> / <owned>", so the owned count is
        # what tells the two apart (Load stages the goods; it does not change what we own).
        if _find_qty_field(omni_fn(capture_fn()), expect_total=int(owned)) is not None:
            return _abort(f"{name}: quantity dialog still open after Load — the amount "
                          "may not be in the basket; aborted before selling anything",
                          skipped)
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


def _restore_bulk(set_bulk_fn, capture_fn, settle: float) -> None:
    """Put 'Put In Bulk' back ON — the BUY flow silently breaks with it OFF (a tile tap
    opens the qty dialog instead of bulk-loading, and purchase_goods leaves goods
    uncommitted; live 2026-08-20). Never leave the market in the trim state."""
    try:
        set_bulk_fn(True, capture_fn())
        time.sleep(settle)
    except Exception as exc:
        logger.warning(f"[sell_down_to] could not restore Put In Bulk: {exc}")
