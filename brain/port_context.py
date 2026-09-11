"""What the PORT OVERWORLD is showing right now — the right panel, and the way in.

The market has `market_context`, the village has `village_context`, and the port had nothing.
So the port's meaning was decided inside `actions.sail_actions.tap_building_entry`, a function
the dispatcher calls to perform ONE transition, which in that single call checks for a
nameplate, selects the Buildings tab, reads the menu, pages it, falls back to the port map and
taps. The classification here is the half of that which is OBSERVATION, pulled out so it can
be asked for, tested against recorded frames, and disagreed with.

WHAT THE STRIP IS. Tasks / Buildings / Players are mutually exclusive; the location pin at the
end is an INDEPENDENT toggle and is not part of the group
(`memory/port-tab-strip-two-can-be-lit`). Their positions are NOT stable — the game re-bakes
its camera-cutout offset per screen — so they are detected, never computed. A calibrated
≈(2146,156) landed on PLAYERS at Jakarta on 2026-08-21, the panel listed `kingdavid LV 70`
instead of buildings, and `gather:Jakarta` failed twice before aborting the mission.

WHY "WHICH TAB IS LIT" AND "WHAT THE LIST CONTAINS" ARE BOTH ASKED, and neither alone is
trusted:

  * The CONTENT can lie about the tab. A Tasks tab full of quest text reads as a perfectly
    healthy list, and at Bordeaux a fuzzy match hit the word "market" inside a quest
    objective. It can also lie the other way: the port overworld carries a standalone
    "⚓ Harbor" shortcut button that the list read picks up, so a PLAYERS tab once read as
    `['tropical', 'kingdavid', '0.#', 'lv 70', 'harbor']` — one building word, from a button
    that is not in the list at all.
  * The HIGHLIGHT can lie about the tab. The location pin lights warm too, which is why the
    reading is described as a hint by the code that uses it.

So `BUILDING_LIST` requires the CONTENT to agree — at least two exact building names, since a
real list carries seven or more and a stray shortcut carries one. Everything softer is
`OTHER_TAB`, which is a thing to fix rather than a thing to read.

NOTHING HERE ACTS, and nothing here is goal-aware. Whether the nameplate that is up names the
building we WANT is a goal question, answered by the activity — the same division
`village_context` states: the context reads SCREENS, the activity knows the order.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

MISS = "miss"

# A building nameplate is floating over an entrance. It appears once the character has walked
# to the door, and TAPPING IT ENTERS — more reliable than waiting for auto-entry, which an
# ambient popup can block. Checked FIRST because it is the shortest way in and it does not
# care what the right panel is showing.
NAMEPLATE = "nameplate"

# The right panel is on the Buildings tab and the list agrees.
BUILDING_LIST = "building_list"

# The strip is there and something else is selected — Tasks or Players. One tap away.
OTHER_TAB = "other_tab"

# No tab icons detected at all. Either the port has not finished drawing or this is not the
# screen we think it is; both are reasons to look again rather than to tap.
NO_PANEL = "no_panel"

CONTEXT_STATES = (NAMEPLATE, BUILDING_LIST, OTHER_TAB, NO_PANEL)

# How many exact building names make a list a BUILDING list. One is not evidence — see the
# "⚓ Harbor" shortcut above. A genuine list carries seven or more, so the two cases are not
# close together and the threshold does not need to be tuned.
_ENOUGH_BUILDING_NAMES = 2


def classify(frame, *, elements=None, rows=None, tabs=None, nameplates=None) -> str:
    """The port-scoped state, or MISS.

    Everything is INJECTABLE because the defaults parse and read; a unit test that reaches for
    the device is a slow test, and a slow test is evidence (see `VillageActivity`).

    `rows` is what `vision.ocr.read_building_menu` returned, `tabs` the detected tab icons,
    `nameplates` the building nameplates on screen. Passing them lets one tick's single parse
    answer every question here, which is the per-frame sharing rule.
    """
    if nameplates is None:
        nameplates = _nameplates(frame)
    if nameplates:
        return NAMEPLATE

    if rows is None:
        rows = _rows(frame)
    if _is_the_building_list(rows):
        return BUILDING_LIST

    if tabs is None:
        tabs = _tabs(frame, elements)
    if tabs:
        return OTHER_TAB

    # NO TABS AND NO LIST. Deliberately NOT a MISS: the port overworld is where we are, the
    # panel simply is not readable yet, and `MISS` means "this is not my screen at all".
    return NO_PANEL


def _is_the_building_list(rows) -> bool:
    """Is the read list the BUILDINGS list?

    Matched EXACTLY, not as substrings: Tasks entries `from istanbul union` and `palace:`
    must not count (live 2026-08-19). The count is what separates a list from a stray
    shortcut — see `_ENOUGH_BUILDING_NAMES`.
    """
    from actions.sail_actions import _BUILDING_NAMES

    hits = sum(1 for row in rows or []
               if str(row[0]).strip().strip(":.").lower() in _BUILDING_NAMES)
    return hits >= _ENOUGH_BUILDING_NAMES


def selected_tab(frame, tabs=None) -> Optional[int]:
    """Which tab of the exclusive group is lit, as an index, or None if it cannot be told.

    A HINT, NEVER AN AUTHORITY, and the caller is expected to treat it as one: the location
    pin at the end of the strip lights warm too, and it is not in the group. Its only honest
    use is to RE-ORDER which tab to try first.
    """
    from actions.sail_actions import selected_tab_index
    return selected_tab_index(frame, tabs if tabs is not None else _tabs(frame))


def _tabs(frame, elements=None) -> list:
    """The tab strip, from the panel that owns it.

    `vision.region_detectors.overworld_panel` finds the strip as part of the whole element,
    anchored on the season row, so it survives the camera-cutout shift that moves every
    calibrated band. Checked against both worlds on 2026-09-11: its tab points are IDENTICAL
    to the older `_tab_strip_candidates`, which derives its search band from the minimap crop,
    and `selected_tab_index` agrees on both — so this is the same reading from one owner
    rather than a second one.

    FALLING BACK IS NOT BELT-AND-BRACES. The panel needs its season row, and a port still
    drawing has not got one yet; the older reader looks only at the strip's band and can
    answer sooner. A miss here means "no tabs", which routes to NO_PANEL — a reason to look
    again, never a reason to tap.
    """
    from actions.sail_actions import _tab_strip_candidates
    from vision.region_detectors.overworld_panel import detect_overworld_panel

    panel = detect_overworld_panel(frame, elements)
    if panel is not None and panel.tabs:
        return list(panel.tab_points)
    return _tab_strip_candidates(frame) or []


def _rows(frame) -> list:
    try:
        from vision.ocr import read_building_menu
        return read_building_menu(frame) or []
    except Exception as exc:                       # noqa: BLE001 — a read, not a decision
        logger.debug(f"[port] could not read the building menu: {exc}")
        return []


def _nameplates(frame) -> list:
    """Every building nameplate on screen, with its label. WHICH one we want is not asked."""
    try:
        from vision.element_postprocess import ROLE_BUILDING_NAMEPLATE
        from vision.screen_perception import parse_screen
        inv = parse_screen(frame, nav_state="port_overworld")
        return [t for t in inv.tagged
                if t.role == ROLE_BUILDING_NAMEPLATE and (t.label or "").strip()]
    except Exception as exc:                       # noqa: BLE001 — a read, not a decision
        logger.debug(f"[port] could not read the nameplates: {exc}")
        return []
