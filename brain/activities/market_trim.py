"""Trimming the hold, one tick at a time.

`sell_down_to` did the whole trim inside a single call: switch to the Sell tab, read the
grid, turn Put In Bulk off, then for every over-stocked good tap its tile, wait for the
quantity dialog, tap the field, type on the keypad, tap Load, check the dialog closed — and
finally tap Sell. Fifteen or more captures for a three-good trim, every one of them a screen
the dispatcher never saw, and any game popup arriving mid-walk landed on code that was not
looking for one.

WHAT IT COST: at Madeira on 2026-09-06 (frame 400 of trace_barter_cmd_2026-09-06T21-45-01)
the Load tap at (1313,943) was dead on the button and the card simply did not close. The
walk had no way to ask anyone about that, so it aborted, and 1,828 Pig sailed on unsold.

THE BASKET SURVIVES THE TICKS, AND THE TILES ARE WHAT SAY SO. Staging moves a good OUT of
its tile and into the cart — measured live: Iron 999 read 822 once its 177 surplus was
staged — so a good that is still over its keep level is a good still to stage, and one that
is not has already been staged. That is the whole recovery rule. Nothing needs to remember
where the walk was when it was interrupted, because the grid is showing it: whatever the
dispatcher did in between, the next SELL_PAGE tick re-reads the tiles and carries on from
what they say. `staged` below is kept for the REPORT, never consulted to decide.

`sell_down_to` remains for the callers that are not on the tick path; this is the same
logic, differently driven.
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

# One re-tap is the answer to a dropped tap, which the game does about once in twenty times.
# More than that is not a dropped tap, and repeating it will not find out what it is.
_MAX_TILE_TAPS = 2


def _over_stocked(goods, keep, giving_up=()) -> list:
    """Goods whose tile still shows more than we mean to keep, with the surplus.

    THIS IS THE TILE NOW, not a plan made earlier — which is exactly what lets the basket
    span ticks. A staged good has already left its tile, so it does not appear here and
    cannot be staged twice.
    """
    out = []
    for name, keep_qty in (keep or {}).items():
        if name.strip().lower() in (giving_up or ()):
            continue                          # its tile will not respond; reported, not retried
        g = next((x for x in goods
                  if str(getattr(x, "name", "")).strip().lower() == name.strip().lower()),
                 None)
        if g is None or getattr(g, "owned_qty", None) is None:
            continue                          # never guess at what we are about to SELL
        owned = int(g.owned_qty)
        if owned > int(keep_qty):
            out.append((name, g, owned, owned - int(keep_qty)))
    return out


def _why_nothing_to_stage(goods, keep) -> list:
    """The goods `_over_stocked` passed over, and why — a skip is a reading failure and it
    must not be silent. Dropping these made a trim that never had a number for Candle look
    like one that had decided Candle was fine."""
    said = []
    for name, keep_qty in (keep or {}).items():
        g = next((x for x in goods
                  if str(getattr(x, "name", "")).strip().lower() == name.strip().lower()),
                 None)
        if g is None:
            said.append(f"{name}: not on the sell page")
        elif getattr(g, "owned_qty", None) is None:
            said.append(f"{name}: owned quantity unreadable")
        elif int(g.owned_qty) <= int(keep_qty):
            said.append(f"{name}: {g.owned_qty} ≤ keep {keep_qty}")
    return said


def on_sell_page(state, goal, *, frame, tap_fn, omni_fn, set_bulk_fn=None) -> dict:
    """One move on the Sell grid: turn bulk off, stage the next good, commit, or finish."""
    from actions.sell_goods import _find_sell_commit, _sell_page

    keep = dict(getattr(goal, "keep_qty", {}) or {})
    goods = _sell_page(frame)
    if goods is None:
        # NONE IS NOT AN EMPTY HOLD. `_sell_page` says None for "I was not looking at the
        # hold" and [] for "I was, and it holds nothing"; collapsing them is what once
        # reported a full hold as trimmed and sailed it to Tripoli.
        return {"do": "blocked", "why": "the Sell grid could not be read"}

    if set_bulk_fn is None:
        from actions.market_actions import _ensure_bulk_mode as set_bulk_fn
    todo = _over_stocked(goods, keep, state.trim_giving_up)

    if todo:
        # BULK OFF FIRST, or a tile tap loads the whole stack instead of opening the dialog
        # — and selling that stack dumps the materials the barter needs.
        #
        # DECIDED AFTER `todo`, NEVER BEFORE. Toggling it for a trim that turns out to have
        # nothing to do is two taps and twenty seconds to change nothing (live 2026-08-22),
        # and the frame on screen already answers whether there is anything to do.
        if not state.trim_bulk_off:
            set_bulk_fn(False, frame)
            state.trim_bulk_off = True
            state.did("turned Put In Bulk off")
            return {"do": "waited", "why": "turned Put In Bulk off before staging"}

        name, g, owned, excess = todo[0]
        # A DROPPED TAP COSTS A REPEAT; A TILE THAT NEVER RESPONDS COSTS A REPORT. Coming
        # back here with the same good still over its keep level IS the retry — the tile did
        # not move, so nothing was staged — but it must not be forever.
        if state.repeated(f"stage:{name}", _MAX_TILE_TAPS):
            state.trim_skipped.append(f"{name}: its tile would not open a quantity dialog")
            keep.pop(name, None)
            state.trim_giving_up.add(name.strip().lower())
            return {"do": "waited", "why": f"{name}'s tile will not respond — leaving it"}
        logger.info(f"[trim] {name}: own {owned}, keep {keep[name]} → sell {excess}")
        tap_fn(g.tap_x, g.tap_y)
        # WHAT WE EXPECT TO SEE, not what we conclude happened. The next tick either finds
        # the good's card — in which case the card, not this, says how many we hold — or
        # finds this same grid, which means the tap was dropped.
        state.trim_good, state.trim_owned = name, owned
        state.did("tapped a tile to stage", (name, owned))
        return {"do": "staged", "why": f"opening {name}'s quantity dialog"}

    # NOTHING IS OVER ITS KEEP LEVEL. Either everything meant for the basket is in it, or
    # there was never anything to do.
    if state.trim_staged and not state.trim_committed:
        commit = _find_sell_commit(frame, omni_fn(frame))
        if commit is None:
            return {"do": "blocked", "why": "no Sell button with the basket loaded"}
        from memory.observed_facts import forget
        forget("hold")                        # about to change — never serve a stale one
        logger.info(f"[trim] tap Sell @ ({commit.cx},{commit.cy}) for {state.trim_staged}")
        tap_fn(commit.cx, commit.cy)
        state.trim_committed = True
        state.did("committed the trim", tuple(sorted(state.trim_staged.items())))
        return {"do": "waited", "why": f"committing {state.trim_staged}"}

    # PUT IN BULK GOES BACK ON, ALWAYS. The BUY flow silently breaks with it off — a tile tap
    # opens the quantity dialog instead of bulk-loading, and the purchase leaves goods
    # uncommitted (live 2026-08-20). Never leave the market in the trim's state.
    if state.trim_bulk_off:
        set_bulk_fn(True, frame)
        state.trim_bulk_off = False
        state.did("put Put In Bulk back on")
        return {"do": "waited", "why": "restoring Put In Bulk"}

    return {"do": "finished",
            "why": f"trimmed {state.trim_staged}" if state.trim_staged else "nothing to trim",
            "skipped": _why_nothing_to_stage(goods, keep),
            "owned": {str(getattr(g, "name", "")).strip().lower(): int(g.owned_qty)
                      for g in goods if getattr(g, "owned_qty", None) is not None}}


def on_goods_info(state, goal, *, frame, tap_fn, omni_fn) -> dict:
    """The good's card is up. Tap its quantity field to bring up the keypad.

    THE DIALOG IS THE AUTHORITY on how many we hold. The grid badge was our reading of a
    40px overlay, and on 2026-08-27 that overlay's melted-wax artwork OCR'd as a leading
    digit — Candle 148 became 2148, at confidence 0.65 against OmniParser's 0.9987. So the
    surplus is recomputed here rather than carried in from the tile.
    """
    from actions.sell_goods import _find_qty_field
    from vision.overlay import detect_overlay

    if not state.trim_good:
        return {"do": "unclaimed", "why": "a goods card we did not open"}

    keep = dict(getattr(goal, "keep_qty", {}) or {})
    field = _find_qty_field(omni_fn(frame), dialog_bbox=detect_overlay(frame).bbox)
    if field is None:
        return {"do": "unclaimed", "why": f"no quantity field on {state.trim_good}'s card"}

    qx, qy, dialog_owned = field
    if state.trim_owned is not None and dialog_owned != state.trim_owned:
        logger.warning(f"[trim] {state.trim_good}: the grid read {state.trim_owned} but the "
                       f"dialog shows {dialog_owned} — trusting the dialog")
    excess = int(dialog_owned) - int(keep.get(state.trim_good, 0))
    if excess <= 0:
        # Nothing to sell after all. Close the card through its own control and move on —
        # an outside tap is the same gesture as a misfire and would be unreadable in the log.
        state.trim_skipped.append(f"{state.trim_good}: {dialog_owned} ≤ "
                                  f"keep {keep.get(state.trim_good)} (per the dialog)")
        state.trim_good = state.trim_owned = state.trim_excess = None
        return {"do": "dismiss", "why": "the dialog says there is no surplus"}

    state.trim_excess = excess
    tap_fn(qx, qy)
    state.did("opened the keypad", (state.trim_good, excess))
    return {"do": "staged", "why": f"typing {excess} for {state.trim_good}"}


def on_keypad(state, goal, *, frame, capture_fn, tap_fn, omni_fn, type_qty_fn=None) -> dict:
    """The keypad is up. Type the surplus and tap Load.

    TYPING KEEPS ITS OWN LOOP, and that is not the sub-loop this refactor removes. It types
    a value and reads the DISPLAY back before pressing ↵ — one dialog, one screen, waiting
    on nothing but its own effect, which is the one loop a primitive may have. The failure
    it exists for is a dropped digit ('21' typed for '218', live 2026-08-20), which on a sell
    dumps the wrong amount of cargo.
    """
    from actions.market_actions import _SELL_LOAD_BUTTON

    if not state.trim_good or state.trim_excess is None:
        return {"do": "unclaimed", "why": "a keypad we did not open"}

    if type_qty_fn is None:
        from actions.market_actions import type_quantity_on_keypad as type_qty_fn
    if not type_qty_fn(state.trim_excess, capture_fn=capture_fn, tap_fn=tap_fn):
        # ↵ WAS NEVER PRESSED — nothing is in the basket for this good, so this loses only
        # the good, not the goods already staged. Per-good confirmation is the model: at
        # Tripoli on 2026-09-06 a whole confirmed basket (Candle 211, Iron 177, one tap from
        # Sell, 205,013 ducats) was thrown away because a later good's dialog misbehaved.
        state.trim_skipped.append(f"{state.trim_good}: could not confirm the typed "
                                  f"quantity {state.trim_excess}")
        state.trim_good = state.trim_owned = state.trim_excess = None
        return {"do": "dismiss", "why": "the typed quantity would not confirm"}

    from actions.sail_actions import _find_button
    load = _find_button(frame, "load") or _SELL_LOAD_BUTTON
    tap_fn(*load)
    # STAGED, AS FAR AS THIS TICK CAN SAY. The next SELL_PAGE tick reads the tile: if it has
    # dropped, the good is in the basket; if it has not, the good is still over its keep
    # level and comes round again. The count below is for the report, not for that decision.
    state.trim_staged[state.trim_good] = state.trim_excess
    state.did("tapped Load", (state.trim_good, state.trim_excess))
    state.trim_good = state.trim_owned = state.trim_excess = None
    return {"do": "staged", "why": "loaded into the basket"}
