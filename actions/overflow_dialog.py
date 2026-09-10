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
  4. **Never dump MATERIALS that can still fund a round** — see THE LAST ROUND below.
  5. The Discard dialog defaults to ALL — always set the quantity explicitly.

THE LAST ROUND (user, 2026-09-04). Leftover materials are the best thing to dump, but only
once no further round can use them: dumping them earlier spends a whole round's product to
save a few units of space. Dumping mid-session could be made to pay, but it is much more
complicated, so it is deliberately not attempted — the rule is the last round or nothing.

  * NOT the last round -> materials are protected exactly like the output good.
  * The last round     -> ALL of them go, first, ahead of spare supply — every unit, even
    when the overflow is smaller, because the space freed beyond it is what the fleet
    resupplies into. See `plan_for_overflow`.

`last_round_reason` names the three ways a round is known to be the last. Any one of them
is enough, and each is read from this dialog plus the round count:

  1. **the overflow exceeds every material aboard** — even dumping the lot cannot clear it,
     so there is nothing left to hold back for;
  2. **a material is down to almost nothing** — below MIN_VIABLE_MATERIAL, so not even a
     minimum-size exchange can use it;
  3. **the day's last round has been played** — seven, per _MAX_DAILY_ROUNDS.

READ THE CARGO TILES, NEVER THE PANEL BEHIND. At frame 15 the Trade Material panel still
reads 182/170 Avocado and 201/170 Cassava, both GREEN, while the cargo holds 12 and 31 —
exactly 170 less, one round's consumption. The panel is a background window that has not
refreshed, so trusting it would report a funded round that does not exist.

Both halves matter. This module previously offered materials as dump candidates on EVERY
round, so a mid-barter overflow could throw away the inputs for every remaining round; that
became far more likely when plan_barter_rounds started planning deliberately into overflow.

All geometry is derived from detected elements (see actions/ui), because this dialog
moves with the camera-cutout offset like everything else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from loguru import logger
from utils.digits import SEPARATORS as _SEP

_INT_RE = re.compile(r"^\d[\d,.\']*$")
_PAIR_RE = re.compile(r"^\s*(\d[\d,.\']*)\s*/\s*(\d[\d,.\']*)")
_USED_CAP_RE = re.compile(r"(\d[\d,.\']*)\s*/\s*(\d[\d,.\']*)\s*\((\d+)%\)")

OVERFLOW_TITLE = "insufficient empty space"
DISCARD_TITLE = "discard goods"


def _int(s: str) -> Optional[int]:
    s = (s or "").strip().translate(_SEP)
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


# A material that can no longer fund a round is worth less to us than any other cargo: it
# cannot be used, and carrying it home is what the fleet was doing wrong. `plan_jettison`
# sorts trade goods by unit_value ascending, so this is what puts materials at the front.
_DEAD_WEIGHT = -1.0


def _needs_map(needs_per_round) -> dict:
    """{normalised material name: units one round consumes}."""
    return {_norm(m): int(q) for m, q in (needs_per_round or {}).items() if int(q or 0) > 0}


# Seven, not eight: the strip draws eight slots but the last is only reachable by PAYING
# for it, so seven is the ceiling for a day we actually play (brain.activities.village).
MAX_DAILY_ROUNDS = 7

# THE FLOOR IS THE MINIMUM-SIZE EXCHANGE, NOT THE FULL-SIZE ONE (user, 2026-09-04).
#
# The panel's `X/Y` is have/need AT THE CURRENT STEPPER VALUE (1-200), so a material short of
# the full-size need is NOT dead — the exchange can be stepped down and still run as a real
# round costing a real daily count (walkthrough notes, Melanesian round 2 at ~9.9%). Testing
# against the full-size need would therefore call it the last round while several usable
# rounds remained, and dump their inputs.
#
# What actually ends it is having so little of one material that no exchange can use it:
# "some barters may still go forward if one material is just 2, but most cannot — at Hutu
# there was 1 Raisin and 500 Pig left and it could not reach another round, as the minimum
# needed for Raisin is 2. And when it is less than 3 you cannot get many anyway."
#
# So 3 is a floor on VIABILITY, not on arithmetic: at or below it, the round that remains is
# too small to be worth the daily count even where the game would allow it.
MIN_VIABLE_MATERIAL = 3


def _held_materials(found: list, needs_per_round) -> dict:
    """{normalised material name: units of it aboard}, materials only."""
    needs = _needs_map(needs_per_round)
    held: dict = {}
    for f in found or []:
        key = _norm(f.get("name"))
        if key in needs:
            held[key] = held.get(key, 0) + int(f.get("qty") or 0)
    return held


def last_round_reason(found: list, needs_per_round, *, pending: Optional[int] = None,
                      rounds_done: Optional[int] = None,
                      max_rounds: int = MAX_DAILY_ROUNDS) -> Optional[str]:
    """Why no further round can use these materials, or None if one still can.

    Read from the overflow dialog itself: `found` is the probe, which names every tile, and
    `needs_per_round` is the recipe. The exchange has already taken this round's inputs by
    the time this dialog appears, so these quantities are the LEFTOVERS — at Camas (frames
    15-17) 12 Avocado and 31 Cassava against a round needing 170 of each.

    `needs_per_round` is the FULL-SIZE need and is used to identify the materials, NOT as the
    floor — see MIN_VIABLE_MATERIAL for why the floor is much lower.

    Returns a reason rather than a bool because this is the judgement that can cost a whole
    round's product, and a log saying WHICH condition fired is what makes it reviewable.
    None when the recipe is unknown: materials cannot then be told apart from any other
    cargo, and nothing here should pretend otherwise."""
    needs = _needs_map(needs_per_round)
    if not needs:
        return None

    if rounds_done is not None and int(rounds_done) >= int(max_rounds):
        return f"the day's last round ({rounds_done} of {max_rounds}) has been played"

    held = _held_materials(found, needs_per_round)
    shown = {_norm(m): str(m) for m in (needs_per_round or {})}   # the recipe's own casing
    spent = [m for m in needs if held.get(m, 0) < MIN_VIABLE_MATERIAL]
    if spent:
        have = ", ".join(f"{shown.get(m, m)} {held.get(m, 0)}" for m in spent)
        return (f"{have} left, under the {MIN_VIABLE_MATERIAL} any exchange needs, and a "
                "round needs every input")

    total = sum(held.values())
    if pending is not None and int(pending) > total:
        return (f"the {pending} pending exceed every material aboard ({total}), so dumping "
                "the lot still cannot clear it")
    return None


def is_last_round(found: list, needs_per_round, **kw) -> Optional[bool]:
    """`last_round_reason` as a bool. None when the recipe is unknown."""
    if not _needs_map(needs_per_round):
        return None
    return last_round_reason(found, needs_per_round, **kw) is not None


def build_cargo(found: list, *, output_good: str, reserves: dict,
                needs_per_round=None, last_round: Optional[bool] = None) -> list:
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
    output itself must be sacrificed" — rather than silently sacrificing it first.

    MATERIALS are treated the same way until the last round: excluded, so a round's worth of
    space is never bought with a round's worth of product. On the last round they invert and
    become the cheapest thing aboard — see `plan_for_overflow`, which dumps them WHOLE rather
    than trimming them to the overflow. `last_round` overrides the reading when a caller
    knows better; None derives it from `needs_per_round`."""
    from brain.jettison_planner import CargoItem
    needs = _needs_map(needs_per_round)
    if last_round is None:
        last_round = is_last_round(found, needs_per_round)
    out = []
    for f in found:
        name, low = f["name"], _norm(f["name"])
        if _norm(output_good) and low == _norm(output_good):
            logger.info(f"[overflow] {name} is the barter output — not a dump candidate")
            continue
        resource = low if low in reserves else None
        if resource is None and low in needs:
            if not last_round:
                logger.info(f"[overflow] {name} is a MATERIAL and a round can still use it "
                            "— not a dump candidate")
                continue
            logger.info(f"[overflow] {name} is a leftover MATERIAL on the last round "
                        "— dumping it first")
            out.append(CargoItem(name=name, qty=f["qty"], unit_value=_DEAD_WEIGHT,
                                 resource=None))
            continue
        out.append(CargoItem(name=name, qty=f["qty"], unit_value=0.0, resource=resource))
    return out


def plan_for_overflow(state: OverflowState, found: list, *, output_good: str,
                      reserves: dict, needs_per_round=None,
                      last_round: Optional[bool] = None, rounds_done: Optional[int] = None):
    """(dump_plan, shortfall) for this dialog, via the canonical jettison policy.

    On the last round the materials are dumped WHOLE — every unit of every one, even when
    the overflow is smaller than they are — and only the remainder is taken from spare
    supply (user, 2026-09-04: "in these cases dump all the materials").

    THE SURPLUS IS NOT WASTE, IT IS SUPPLY HEADROOM. An overflow means the hold is at 100%,
    and a hold at 100% cannot take supply aboard. Villages cannot resupply at all
    (VILLAGE_LEG_RESERVE_DAYS is 7.0 for exactly that reason — the fleet must already be
    carrying the round trip), and the fleet arrives at the next port still full, so it cannot
    top up there either. Dumping only what the overflow needs leaves the hold full and the
    fleet sailing on whatever supply it happened to have. Dumping the lot converts dead
    material into room the auto-resupply can actually fill.

    It is also the cheaper action mechanically: the Discard dialog defaults to the full
    stack, so a whole-stack dump is one tap and never opens the keypad.
    """
    from brain.jettison_planner import DumpAction, plan_jettison
    if last_round is None:
        last_round = is_last_round(found, needs_per_round, pending=state.pending,
                                   rounds_done=rounds_done)
    need = state.pending or 0

    plan: list = []
    if last_round:
        needs = _needs_map(needs_per_round)
        for f in found or []:
            qty = int(f.get("qty") or 0)
            if _norm(f.get("name")) in needs and qty > 0:
                plan.append(DumpAction(f["name"], qty, None))
                need -= qty

    cargo = build_cargo(found, output_good=output_good, reserves=reserves,
                        needs_per_round=needs_per_round, last_round=False)
    rest, shortfall = plan_jettison(max(0, need), cargo, reserves)
    return plan + rest, shortfall


def clear_overflow(*, output_good: str, reserves: dict, needs_per_round=None,
                   rounds_done: Optional[int] = None, last_round: Optional[bool] = None,
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

    # WHILE A ROUND REMAINS, NOTHING ABOARD IS WORTH DUMPING FOR THIS. Materials are
    # protected until the last round, the output is never a candidate, and what that leaves
    # is surplus supply — which the fleet needs at sea and which cannot cover an overflow
    # anyway. So the probe has nothing to find, and the card's own answer is Receive.
    #
    # Live 2026-09-10 at San Village, five times over: twelve probe taps and ~70s a round to
    # plan `[('Water', 3), ('Food', 3)]` against 47 pending — six units recoverable at best,
    # bought with supply. User: *"if it is not the last round, just receive. Just lose the 47
    # that overflowed. Only do probe at the last round."*
    if last_round is False:
        ok = _receive(state, ui_mod)
        logger.info(f"[overflow] not the last round — receiving what fits of {pending_before} "
                    "and letting the rest go; a further round still needs the materials, and "
                    "supply is not worth spending on the remainder")
        return {"ok": ok, "pending_before": pending_before, "discarded": [],
                "sacrificed": pending_before,
                "reason": f"not the last round — {pending_before} given up rather than "
                          "spending supply or a round's materials"}

    found = probe_tiles(capture_fn, tap_fn, state, omni_fn=omni_fn, ui=ui_mod)
    # Decided BEFORE anything is discarded, and logged WITH ITS REASON, because it is the
    # one judgement here that can cost a whole round's product if it is wrong either way.
    if last_round:
        # THE CALLER READ THE PANEL; THIS CARD COVERS IT. An answer taken from the barter
        # panel beats one re-derived from the tiles in front of us — see `_is_last_round`.
        # (`last_round is False` has already returned above, so this is the only way in.)
        last = True
        why = ("LAST ROUND — the village says no further round is available; dumping every "
               "material")
    elif not _needs_map(needs_per_round):
        last, why = None, "the recipe is unknown — materials cannot be identified"
    else:
        why = last_round_reason(found, needs_per_round, pending=pending_before,
                                rounds_done=rounds_done)
        last = why is not None
        why = (f"LAST ROUND — {why}; dumping every material"
               if last else "a further round is still funded — materials are protected")
    logger.info(f"[overflow] {why}")
    plan, shortfall = plan_for_overflow(state, found, output_good=output_good,
                                        reserves=reserves,
                                        needs_per_round=needs_per_round, last_round=last)
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
