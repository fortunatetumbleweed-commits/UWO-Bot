"""The 'Insufficient Empty Space' overflow flow — read it, decide, clear it.

When a barter Exchange yields more than the hold can take, the game holds the output
PENDING in an overflow dialog and warns that "Unreceived trade goods will be discarded".
Dismissing it LOSES those goods. Freeing space lets them auto-receive incrementally
(observed live: 143 → 131 → 100 → 0 as three discards landed), and Receive closes it.

The awkward part is that the cargo tiles are icons with a quantity badge and no name —
OmniParser reports "226" and nothing else, so the bot cannot tell water from the barter
output by looking. The Discard Goods dialog DOES name the item, and it has a Cancel. So
the flow here is probe-then-decide: tap a tile, read what it actually is, and either
discard an exact quantity or back out untouched. No icon recognition, no guessing from
quantity, and nothing irreversible until the name has been read.

Safety, in order of importance:
  1. **Never dismiss the dialog with goods still pending** — that discards them.
  2. **Never drop water/food below the leg's reserve** — brain.jettison_planner owns that
     rule; this module only supplies it with what is actually aboard.
  3. **Never dump the barter output** we just came to collect.
  4. The Discard dialog defaults to ALL — always set the quantity explicitly.

All geometry is derived from detected elements (see actions/ui), because this dialog
moves with the camera-cutout offset like everything else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from loguru import logger

_INT_RE = re.compile(r"^[\d,]+$")
_PAIR_RE = re.compile(r"^\s*([\d,]+)\s*/\s*([\d,]+)")
_USED_CAP_RE = re.compile(r"([\d,]+)\s*/\s*([\d,]+)\s*\((\d+)%\)")

OVERFLOW_TITLE = "insufficient empty space"
DISCARD_TITLE = "discard goods"


def _int(s: str) -> Optional[int]:
    s = (s or "").strip().replace(",", "")
    return int(s) if s.isdigit() else None


def _label(e) -> str:
    return (getattr(e, "label", "") or "").strip()


@dataclass
class CargoTile:
    """One tappable tile in the dialog's Cargo row — quantity is all it shows."""
    qty: int
    element: object = None

    @property
    def pos(self) -> Optional[Tuple[int, int]]:
        e = self.element
        return (e.cx, e.cy) if e is not None else None


@dataclass
class OverflowState:
    pending: Optional[int] = None            # units waiting to be received
    cargo_used: Optional[int] = None
    cargo_capacity: Optional[int] = None
    tiles: List[CargoTile] = field(default_factory=list)
    receive: object = None                   # the Receive button element

    @property
    def free_space(self) -> Optional[int]:
        if self.cargo_used is None or self.cargo_capacity is None:
            return None
        return max(0, self.cargo_capacity - self.cargo_used)


@dataclass
class DiscardState:
    name: Optional[str] = None               # what the tile actually IS
    selected: Optional[int] = None           # quantity currently set (defaults to ALL)
    held: Optional[int] = None
    qty_field: object = None                 # tap to open the keypad
    ok: object = None
    cancel: object = None


def is_overflow_dialog(elements) -> bool:
    return any(OVERFLOW_TITLE in _label(e).lower() for e in elements or [])


def is_discard_dialog(elements) -> bool:
    return any(DISCARD_TITLE in _label(e).lower() for e in elements or [])


def _header(elements, text: str):
    text = text.lower()
    for e in elements or []:
        if _label(e).lower().startswith(text):
            return e
    return None


def read_overflow(elements) -> Optional[OverflowState]:
    """Parse the overflow dialog. Sections are located by their HEADERS, and the tiles
    by which header they sit under — the dialog's absolute position is never assumed."""
    if not is_overflow_dialog(elements):
        return None
    st = OverflowState()

    numeric = [e for e in elements
               if getattr(e, "element_type", "") == "button" and _INT_RE.match(_label(e))]

    pending_hdr = _header(elements, "received trade")
    cargo_hdr = _header(elements, "cargo")

    # 'Received Trade Goods' holds ONE tile: the pending output.
    if pending_hdr is not None:
        below = [e for e in numeric if pending_hdr.y1 <= e.y1 <= pending_hdr.y1 + 220]
        if below:
            st.pending = _int(_label(min(below, key=lambda e: e.x1)))

    # The 'N/M (P%)' readout shares the Cargo header's row.
    for e in elements:
        m = _USED_CAP_RE.search(_label(e))
        if m and (cargo_hdr is None or abs(e.y1 - cargo_hdr.y1) <= 60):
            st.cargo_used, st.cargo_capacity = _int(m.group(1)), _int(m.group(2))
            break

    # Cargo tiles: the numeric buttons on the row(s) under the Cargo header.
    if cargo_hdr is not None:
        tiles = [e for e in numeric if e.y1 > cargo_hdr.y1 - 10]
        if pending_hdr is not None:
            tiles = [e for e in tiles if e.y1 > pending_hdr.y1 + 220]
        st.tiles = [CargoTile(qty=_int(_label(e)), element=e)
                    for e in sorted(tiles, key=lambda e: (e.y1, e.x1))
                    if _int(_label(e)) is not None]

    for e in elements:
        if _label(e).lower() == "receive":
            st.receive = e
            break
    return st


def read_discard(elements) -> Optional[DiscardState]:
    """Parse the Discard Goods dialog — crucially the item's NAME, which the cargo tile
    itself never showed. The `selected/held` pair anchors the layout: the name sits just
    above it, the keypad opens from it."""
    if not is_discard_dialog(elements):
        return None
    st = DiscardState()

    pair = None
    for e in elements:
        m = _PAIR_RE.match(_label(e))
        if m:
            sel, held = _int(m.group(1)), _int(m.group(2))
            if sel is not None and held is not None and sel <= held:
                if pair is None or e.y1 > pair[0].y1:
                    pair = (e, sel, held)
    if pair is not None:
        st.qty_field, st.selected, st.held = pair[0], pair[1], pair[2]
        # The name is the nearest text ABOVE the quantity pair, in its column.
        above = [e for e in elements
                 if getattr(e, "element_type", "") == "text"
                 and e.y2 <= pair[0].y1 + 6
                 and abs(e.cx - pair[0].cx) <= 220
                 and _label(e) and not _INT_RE.match(_label(e))
                 and "/" not in _label(e)]
        if above:
            st.name = _label(max(above, key=lambda e: e.y1))

    for e in elements:
        low = _label(e).lower()
        if low == "ok" and st.ok is None:
            st.ok = e
        elif low == "cancel" and st.cancel is None:
            st.cancel = e
    return st


# ── Driver: probe what is aboard, plan with the canonical policy, execute ──────

def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def probe_tiles(capture_fn, tap_fn, state: OverflowState, *, omni_fn, ui,
                max_tiles: int = 12) -> list:
    """Tap each cargo tile to learn WHAT it is, cancelling out of every one.

    The tiles show a quantity and nothing else, so this is the only way to tell the
    barter output from the water we must not dump. Cancel makes the probe free: nothing
    is discarded until a plan says so."""
    found = []
    for tile in state.tiles[:max_tiles]:
        if tile.element is None:
            continue
        ui.tap_element(tile.element, dwell="dialog", why=f"identify the {tile.qty}-unit tile")
        dc = read_discard(omni_fn(capture_fn()))
        if dc is None or not dc.name:
            logger.warning(f"[overflow] tile {tile.qty} did not identify — leaving it alone")
            if dc is not None and dc.cancel is not None:
                ui.tap_element(dc.cancel, dwell="dialog", why="cancel an unidentified tile")
            continue
        held = dc.held if dc.held is not None else tile.qty
        found.append({"name": dc.name, "qty": held, "tile": tile})
        logger.info(f"[overflow] tile {tile.qty} is {dc.name!r} (held {held})")
        if dc.cancel is not None:
            ui.tap_element(dc.cancel, dwell="dialog", why=f"done identifying {dc.name}")
    return found


def build_cargo(found: list, *, output_good: str, reserves: dict) -> list:
    """Turn probed tiles into `jettison_planner.CargoItem`s.

    Two classifications carry all the safety: supplies get a `resource` so the planner
    keeps their reserve, and the barter OUTPUT is **left out of the candidate list
    entirely**.

    Pricing the output high is NOT enough — `plan_jettison` sorts by value but still
    walks every trade good before it touches a supply, so an expensive output is dumped
    ahead of genuinely spare water. Simulated against the live Camas overflow it planned
    to throw away 100 of the 3,613 Camas while 122 units of spare supply sat untouched;
    the human dumped 50 food + 50 water instead. Excluding it is also what makes the
    planner's `shortfall` mean the right thing — "even after everything dumpable, the
    output itself must be sacrificed" — rather than silently sacrificing it first."""
    from brain.jettison_planner import CargoItem
    out = []
    for f in found:
        name, low = f["name"], _norm(f["name"])
        if _norm(output_good) and low == _norm(output_good):
            logger.info(f"[overflow] {name} is the barter output — not a dump candidate")
            continue
        resource = low if low in reserves else None
        out.append(CargoItem(name=name, qty=f["qty"], unit_value=0.0, resource=resource))
    return out


def plan_for_overflow(state: OverflowState, found: list, *, output_good: str,
                      reserves: dict):
    """(dump_plan, shortfall) for this dialog, via the canonical jettison policy."""
    from brain.jettison_planner import plan_jettison
    cargo = build_cargo(found, output_good=output_good, reserves=reserves)
    need = state.pending or 0
    return plan_jettison(need, cargo, reserves)


def clear_overflow(*, output_good: str, reserves: dict,
                   capture_fn=None, tap_fn=None, omni_fn=None, ui_mod=None,
                   type_qty_fn=None, max_discards: int = 8) -> dict:
    """Clear an open overflow dialog: probe → plan → discard exactly → Receive.

    Returns {ok, pending_before, discarded, sacrificed, reason}. `sacrificed` > 0 means
    the reserve could not be preserved AND the overflow cleared, so that many units of
    the output were knowingly given up — reported, never silent."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen as capture_fn
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn
    if omni_fn is None:
        from vision.omniparser import parse_fast_cached as omni_fn
    if ui_mod is None:
        from actions import ui as ui_mod
    if type_qty_fn is None:
        from actions.market_actions import type_quantity_on_keypad as type_qty_fn

    state = read_overflow(omni_fn(capture_fn()))
    if state is None:
        return {"ok": True, "pending_before": 0, "discarded": [], "sacrificed": 0,
                "reason": "no overflow dialog on screen"}
    pending_before = state.pending or 0
    if not pending_before:
        ok = _receive(state, ui_mod)
        return {"ok": ok, "pending_before": 0, "discarded": [], "sacrificed": 0,
                "reason": "nothing pending — received"}

    found = probe_tiles(capture_fn, tap_fn, state, omni_fn=omni_fn, ui=ui_mod)
    plan, shortfall = plan_for_overflow(state, found, output_good=output_good,
                                        reserves=reserves)
    logger.info(f"[overflow] pending {pending_before} → plan "
                f"{[(d.name, d.qty) for d in plan]} shortfall={shortfall}")
    by_name = {_norm(f["name"]): f["tile"] for f in found}

    discarded = []
    for action in plan[:max_discards]:
        tile = by_name.get(_norm(action.name))
        if tile is None or tile.element is None:
            logger.warning(f"[overflow] no tile for {action.name} — skipping")
            continue
        ui_mod.tap_element(tile.element, dwell="dialog", why=f"discard {action.name}")
        dc = read_discard(omni_fn(capture_fn()))
        if dc is None or _norm(dc.name or "") != _norm(action.name):
            # The dialog that opened is not the item we planned for — back out rather
            # than discard something unidentified.
            logger.warning(f"[overflow] expected {action.name}, got {dc.name if dc else None}"
                           " — cancelling")
            if dc is not None and dc.cancel is not None:
                ui_mod.tap_element(dc.cancel, dwell="dialog", why="wrong item")
            continue
        # The dialog DEFAULTS TO ALL — set the exact quantity whenever we want less.
        if dc.held is not None and action.qty < dc.held:
            if dc.qty_field is None:
                logger.warning(f"[overflow] cannot set a partial {action.name} — cancelling")
                if dc.cancel is not None:
                    ui_mod.tap_element(dc.cancel, dwell="dialog", why="no qty field")
                continue
            ui_mod.tap_element(dc.qty_field, dwell="dialog", why="open the keypad")
            if not type_qty_fn(action.qty, capture_fn=capture_fn, tap_fn=tap_fn):
                logger.warning(f"[overflow] could not confirm {action.qty} — cancelling")
                dc2 = read_discard(omni_fn(capture_fn()))
                if dc2 is not None and dc2.cancel is not None:
                    ui_mod.tap_element(dc2.cancel, dwell="dialog", why="unconfirmed qty")
                continue
            dc = read_discard(omni_fn(capture_fn())) or dc
        if dc.ok is None:
            continue
        ui_mod.tap_element(dc.ok, dwell="dialog", why=f"discard {action.qty} {action.name}")
        discarded.append((action.name, action.qty))

    state = read_overflow(omni_fn(capture_fn())) or state
    remaining = state.pending or 0
    ok = _receive(state, ui_mod)
    return {"ok": ok, "pending_before": pending_before, "discarded": discarded,
            "sacrificed": remaining,
            "reason": (f"received {pending_before - remaining} of {pending_before}"
                       + (f"; {remaining} discarded to protect the supply reserve"
                          if remaining else ""))}


def _receive(state: OverflowState, ui_mod) -> bool:
    """Tap Receive. This is the ONLY way to close the dialog without losing goods —
    the X and Back both discard whatever is still pending."""
    if state.receive is None:
        logger.error("[overflow] Receive button not found — NOT dismissing (that would "
                     "discard the pending goods)")
        return False
    return ui_mod.tap_element(state.receive, dwell="screen", why="receive pending goods")
