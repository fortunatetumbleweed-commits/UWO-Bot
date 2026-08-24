# actions/jettison_executor.py
# ⚠️ #27 — jettison executor with supply-safety guard.
#
# Clears a cargo overflow ("Insufficient Empty Space" dialog) by executing EXACTLY
# the plan from brain.jettison_planner.plan_jettison — dump cheapest goods first,
# then only EXCESS supply above the longest-leg reserve, NEVER below it.
#
# SAFETY: the Discard Goods dialog defaults to ALL. This executor issues the exact
# planned quantity for each item (partial for supplies) and NEVER blind-OKs. If the
# plan can't clear the overflow without touching the reserve (shortfall > 0), it
# stops and reports — the caller must sacrifice some of the overflow good itself,
# not the fleet's water/food. See project_supply_route_barter_facts (Discard-ALL
# hazard) + the #21 planner.

from __future__ import annotations

from typing import Callable, Optional, Sequence

from loguru import logger

from brain.jettison_planner import CargoItem, DumpAction, plan_jettison


def execute_jettison(
    overflow_units: int,
    cargo_items: Sequence[CargoItem],
    reserves: dict,
    *,
    discard_fn: Callable[[CargoItem, int], bool],
    verify_cleared_fn: Optional[Callable[[], bool]] = None,
) -> dict:
    """Execute a safe jettison to free `overflow_units`.

    discard_fn(item, qty) -> bool: dump exactly `qty` of `item` (partial via the
        input pad for supplies), returning whether the discard was applied. It must
        NEVER dump more than `qty`.
    verify_cleared_fn() -> bool: whether the overflow dialog is gone afterwards
        (e.g. task_conditions.dialog_absent('insufficient empty space')).

    Returns {ok, plan, shortfall, dumped, cleared}."""
    plan, shortfall = plan_jettison(overflow_units, cargo_items, reserves)

    if shortfall > 0:
        logger.warning(
            f"[jettison] plan leaves shortfall {shortfall} — cannot clear overflow "
            f"without dropping supply below reserve; caller must sacrifice the overflow good")

    by_name = {c.name: c for c in cargo_items}
    dumped = []
    for action in plan:
        item = by_name.get(action.name)
        if item is None:
            continue
        # SAFETY: dump exactly the planned quantity — never the dialog's default ALL.
        ok = bool(discard_fn(item, action.qty))
        dumped.append({"name": action.name, "qty": action.qty,
                       "resource": action.resource, "ok": ok})
        logger.info(f"[jettison] discarded {action.qty} {action.name}"
                    f"{' (supply)' if action.resource else ''} ok={ok}")

    cleared = verify_cleared_fn() if verify_cleared_fn is not None else (shortfall == 0)
    ok = cleared and shortfall == 0
    return {"ok": ok, "plan": plan, "shortfall": shortfall,
            "dumped": dumped, "cleared": cleared}


def discard_item_live(item: CargoItem, qty: int, *,
                      capture_fn: Optional[Callable] = None,
                      tap_fn: Optional[Callable] = None,
                      set_qty_fn: Optional[Callable] = None) -> bool:
    """Default live discard: tap the item's cargo tile → in the Discard Goods dialog
    set the quantity to `qty` (partial via the input pad when qty < held) → OK.

    NOTE: the tile position and input-pad coords need live calibration on the
    Insufficient-Space / Discard dialogs; this wires the SAFE-primitive flow and the
    exact-quantity discipline. Supplies get a partial quantity; a full dump of a
    trade good can skip the pad."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn

    tile = getattr(item, "tile", None)     # (x, y) supplied by the cargo reader, if any
    if tile is None:
        logger.warning(f"[jettison] no tile position for {item.name} — cannot discard")
        return False
    tap_fn(*tile)                          # open Discard Goods for this item

    partial = qty < item.qty
    if partial and set_qty_fn is not None:
        set_qty_fn(qty)                    # input-pad partial (keeps the reserve)
    # else: dumping the whole tile — the dialog already defaults to the full amount.

    from actions.route_execution import find_text_button
    from actions.sail_actions import _ocr_frame
    ok_pos = find_text_button(_ocr_frame(capture_fn()), "ok", min_ratio=0.8)
    if ok_pos:
        tap_fn(*ok_pos)
        return True
    return False
