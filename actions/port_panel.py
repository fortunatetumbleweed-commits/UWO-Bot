"""The port overworld's right panel, and the way into a building.

MOVED OUT OF `actions/sail_actions.py` (2026-09-11). The port overworld is the only world
whose work had no owner, so it accumulated here among the SAILING primitives — which is why
the intent dispatcher came to import `tap_building_entry` by name (user: *"dispatcher should
not know about tap building entry, that is the PortActivity's responsibility"*). Nothing in
this move changes behaviour; it changes who the code belongs to.

WHAT BELONGS HERE: everything about the panel on a port overworld — its tab strip, its
building list, the nameplate over a door, and the port map as the last way in. `PortActivity`
is the decision-maker above it and `brain.port_context` classifies what is showing; this file
is the UI layer they reach through, which is the one direction `brain/layers.py` allows.

WHAT DID NOT MOVE: `selected_tab_index`, because the WORLD MAP has a tab strip too and reads
it with the same luminance test. It is shared, so it stays where both can reach it.

STILL A SUB-LOOP, DELIBERATELY UNCHANGED BY THIS MOVE. `tap_building_entry` claims to tap
"ONCE" and, in that one call, checks for a nameplate, selects the Buildings tab (trying each
candidate and verifying by re-reading the list), reads the menu, pages it up to four times,
opens the port map, and taps. Flattening it into one action per tick needs the dispatcher's
in-flight guard and stall tuple to carry a progress marker — `_screen_signature` returns the
family verdict on a port (0.9998), so a tab switch or a scroll leaves it identical and the
next tick reads the tap as lost. That is a separate change; this one only gives the code a
home to be fixed in. See `docs/activity_architecture.md` §6.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

from loguru import logger

from actions.adb_actions import tap
from capture.adb_capture import capture_screen
from vision.ocr import read_building_menu
from utils.fuzzy import token_sim

# How much longer than the building's own name a label may be and still count as a substring
# match — enough for 'the Market' or a trailing glyph, not a sentence.
_NAME_SLACK = 6

# NOT appear in Tasks/quest text — 'union'/'palace' were dropped because quests like "from Istanbul
# Union" / "Palace:" false-matched a SUBSTRING check and made the bot think it was on the Buildings
# tab when it was actually on Tasks (live 2026-08-19). Matched EXACTLY, not as substrings.
_BUILDING_NAMES = frozenset({"harbor", "market", "shipyard", "bank", "inn", "sanctuary",
                             "item shop", "bureau"})


# NOTE: the "several names, not one" threshold moved to `brain.port_context` with the rule it
# belongs to. `_BUILDING_NAMES` stays here because other readers in this module use it.


def _on_buildings_tab(buildings) -> bool:
    """True if the read list is the BUILDINGS tab.

    Matched EXACTLY, not as substrings: Tasks entries 'from istanbul union' / 'palace:' must
    not count (live 2026-08-19).

    And matched at least twice. A single hit is not evidence, because the port overworld has
    a standalone "⚓ Harbor" shortcut button that the list read picks up: at Jakarta on
    2026-08-21 the PLAYER tab read as ['tropical', 'kingdavid', '0.#', 'lv 70', 'harbor'] —
    one building word, from a button that is not in the list at all. That single match made
    this return True, so the "wrong tab?" branch never ran, and `navigate_to_building`
    scrolled a list of player names for 60s before giving up. A genuine building list carries
    seven or more matches (harbor, market, shipyard, bank, inn, sanctuary, bureau…), so the
    two cases are not close together.

    ONE IMPLEMENTATION, AND IT BELONGS TO THE PORT. "Is this the building list" is a question
    about what the PORT OVERWORLD is showing, so it is answered by `brain.port_context`, which
    the port activity owns — the same split the market and the village already have. This
    wrapper stays because the callers here read better for it.
    """
    from brain.port_context import _is_the_building_list
    return _is_the_building_list(buildings)


def _tab_strip_band() -> Tuple[int, int, int, int]:
    """SEARCH region for the port-overworld tab strip, derived from the calibrated minimap.

    A region is a safe use of a calibrated constant; a tap target is not. The strip sits just
    above the minimap, so this brackets it generously and lets detection pick the icons out.
    """
    try:
        from brain.ai_nav.vision_input import MINIMAP_CROP as _MC
    except Exception:
        _MC = (1984, 205, 2379, 395)
    x0, y0, x1, y1 = _MC
    return (x0 - 80, y0 - 100, x1 + 80, y0 - 5)


def _tab_strip_candidates(frame) -> list:
    """Detected tab icons above the minimap, ordered left→right.

    The strip is Tasks / Buildings / Players / Location. Their POSITIONS are not stable — the
    game re-bakes its camera-cutout offset per screen — so they are detected, not computed.
    That distinction is not academic: `_buildings_tab_pos()` used to return a calibrated
    ≈(2146,156), and on Jakarta 2026-08-21 the real tabs sat at ≈2016 (Buildings) and ≈2114
    (Players). The constant landed on PLAYERS, so the bot selected the player tab itself,
    the panel listed 'kingdavid LV 70' instead of buildings, and `gather:Jakarta` failed
    twice with "Could not enter 'Market' after 60s" and aborted the mission.
    """
    try:
        from vision.omniparser import parse_fast_cached
        els = parse_fast_cached(frame) or []
    except Exception as exc:
        logger.debug(f"  tab-strip detection unavailable ({type(exc).__name__}: {exc})")
        return []
    bx0, by0, bx1, by1 = _tab_strip_band()
    hits = [e for e in els if bx0 <= e.cx <= bx1 and by0 <= e.cy <= by1]
    hits.sort(key=lambda e: e.cx)
    if hits:
        logger.info("  Tab-strip candidates: "
                    + ", ".join(f"{e.label!r}@({e.cx},{e.cy})" for e in hits))
    hits = _row_only(hits)
    return [(e.cx, e.cy) for e in hits]


# A TAB STRIP IS A ROW, NOT ANY ICON IN THE BAND.
# Live 2026-08-24 at Bordeaux an event popup covered the right panel, and its own close-X at
# (1917,165) sat inside the tab band and was offered as a "tab". Tapping popup furniture
# cannot select the Buildings tab, so the search failed and the caller went on to match the
# word "market" inside a quest line. Real tabs come as three or more similar icons at the
# same height, evenly spaced (~100px apart, measured); two icons 300px apart are not a strip.
_TAB_MIN_ROW = 3
_TAB_SPACING_TOL = 0.45
_TAB_SAME_ROW_PX = 25


def _row_only(hits) -> list:
    """Keep the hits that actually form an evenly spaced row; [] if none do."""
    if len(hits) < _TAB_MIN_ROW:
        if hits:
            logger.info(f"  Only {len(hits)} icon(s) in the tab band — not a tab strip "
                        "(a strip is a row of similar icons); ignoring")
        return []
    ys = sorted(e.cy for e in hits)
    mid_y = ys[len(ys) // 2]
    row = [e for e in hits if abs(e.cy - mid_y) <= _TAB_SAME_ROW_PX]
    if len(row) < _TAB_MIN_ROW:
        logger.info("  Tab-band icons are not at a common height — not a tab strip")
        return []
    gaps = [b.cx - a.cx for a, b in zip(row, row[1:])]
    med = sorted(gaps)[len(gaps) // 2] if gaps else 0
    if med <= 0 or any(abs(g - med) > _TAB_SPACING_TOL * med for g in gaps):
        logger.info(f"  Tab-band icons are unevenly spaced (gaps={gaps}) — not a tab strip")
        return []
    return row


def _building_row(buildings, target: str):
    """The row that IS this building, or None.

    A BUILDING LABEL IS A NAME, NOT A SENTENCE CONTAINING ONE. `target in lbl` matched the
    quest objective "move to market in ..." when the right panel was showing the Tasks tab
    (live 2026-08-24 at Bordeaux): tapping it handed the fleet to a quest voyage to Jakarta.
    So a substring hit is trusted only when the label is about as long as the name itself
    ("Market", "the Market"), never when the name is buried in running text. `token_sim`
    still catches OCR mangling of the real label.
    """
    def _is_it(lbl: str) -> bool:
        low = lbl.lower().strip()
        if token_sim(lbl, target) >= 0.75:
            return True
        return target in low and len(low) <= len(target) + _NAME_SLACK

    return next(((x, y) for lbl, x, y in buildings if _is_it(lbl)), None)


def _list_signature(buildings) -> tuple:
    """Where the building list is scrolled to — now `actions.ui.lists.signature`.

    Kept as a name because callers and tests use it, but the logic moved: three copies of
    "has this list moved?" existed (here, `explore_actions`, and nowhere at all in the live
    world-map path), so they now all ask one function. See actions/ui/lists.py.
    """
    from actions.ui.lists import signature
    return signature(buildings)


def _scroll_list_for(target: str, frame, *, max_scrolls: int = 4, reset_swipes: int = 4):
    """Rewind the list to the top, then page down, looking for *target*. Returns a tap coord.

    NOW THE SHARED PAGER (actions.ui.lists.find_in_list). The paging, the movement test and
    the rewind all moved there so every list in the game uses one implementation; what stays
    here is what is specific to THIS list — how to read its rows (`read_building_menu`) and
    what counts as a match (`_building_row`, which knows a label is a name and not a sentence
    containing one).

    The lesson that made the column matter is now enforced inside the pager: it swipes down
    the median entry x, because a region-centre swipe can miss the list entirely — the rewind
    then never scrolls, the signature stays unchanged, "at top" is assumed, and a clipped top
    building never comes back (live 2026-08-19, Jakarta).
    """
    from vision.ocr import read_building_menu
    from actions.adb_actions import swipe_fast
    from actions.ui.lists import find_in_list
    from config.settings import BUILDING_MENU_REGION

    rows_now = read_building_menu(frame)
    my = (rows_now[len(rows_now) // 2][2] if rows_now
          else (BUILDING_MENU_REGION[1] + BUILDING_MENU_REGION[3]) // 2)
    fallback_x = (BUILDING_MENU_REGION[0] + BUILDING_MENU_REGION[2]) // 2

    return find_in_list(
        target,
        match=_building_row,
        read_rows=read_building_menu,
        capture=capture_screen,
        swipe=lambda x1, y1, x2, y2: swipe_fast(x1, y1, x2, y2,
                                                duration_ms=300, settle_ms=600),
        fallback_x=fallback_x, y=my,
        max_pages=max_scrolls, rewind_pages=reset_swipes,
        label="building list")


def select_buildings_tab(frame) -> dict:
    """Put the right panel on the Buildings tab. Returns {ok, frame, reason}.

    The tab bar above the minimap toggles Tasks / Buildings / Players, and on arrival the
    Tasks tab can be auto-selected (live 2026-08-19 at Jakarta → empty list → 60s timeout).
    Which index is Buildings varies with how many tabs a port shows, and a wrong guess does
    not fail quietly — it SELECTS another tab. So each candidate is tried and VERIFIED by
    re-reading the list.
    """
    from vision.ocr import read_building_menu

    if _on_buildings_tab(read_building_menu(frame)):
        return {"ok": True, "frame": frame, "reason": "already there"}

    candidates = _tab_strip_candidates(frame)
    if not candidates:
        logger.warning("  Building list not on screen and no tab icons detected")
        return {"ok": False, "frame": frame, "reason": "no tab icons"}

    # Try the tabs we are NOT already on first. The highlight read is a hint, not an authority
    # (the location toggle lights up warm too), so it may only RE-ORDER the attempts.
    # LAZY, AND BOTH WAYS. `selected_tab_index` stayed in `sail_actions` because the world
    # map reads its own strip with it, and `navigate_to_building` there still reaches
    # `tap_building_entry` here — so a top-level import in either direction is a cycle.
    from actions.sail_actions import selected_tab_index
    already = selected_tab_index(frame, candidates)
    order = ([i for i in range(len(candidates)) if i != already]
             + ([already] if already is not None else []))
    for i in order:
        bx, by = candidates[i]
        logger.info(f"  Trying tab {i + 1}/{len(candidates)} @ ({bx},{by})")
        tap(bx, by)
        time.sleep(1.5)
        frame = capture_screen()
        if _on_buildings_tab(read_building_menu(frame)):
            logger.info(f"  Buildings tab selected @ ({bx},{by})")
            return {"ok": True, "frame": frame, "reason": "selected"}

    # KNOWING IT IS THE WRONG LIST AND TAPPING ANYWAY IS THE WORST OUTCOME.
    return {"ok": False, "frame": frame, "reason": f"none of {len(candidates)} tabs listed buildings"}


def _nameplate_for(target: str, frame):
    """The floating nameplate above a building entrance, if it names *target*.

    It appears once the character has walked to the door, and TAPPING IT ENTERS — far more
    reliable than waiting for auto-entry, which an ambient popup can block.
    """
    from vision.screen_perception import parse_screen
    from vision.element_postprocess import ROLE_BUILDING_NAMEPLATE
    from brain.states.port_map import _canonical_name

    inv = parse_screen(frame, nav_state="port_overworld")
    for t in inv.tagged:
        if t.role != ROLE_BUILDING_NAMEPLATE:
            continue
        lbl = (t.label or "").lower().strip()
        if not lbl:
            continue
        if (target in lbl or (_canonical_name(lbl) or "") == target
                or token_sim(lbl, target) >= 0.7):
            return t
    return None


def _port_map_entry(target: str):
    """Fallback: open the port map and tap the building icon. Returns a tap coord or None."""
    from brain.states.port_map import open_port_map, read_port_map_buildings, close_port_map

    logger.info(f"  {target!r} not in the list — trying the port map")
    if not open_port_map():
        logger.error("  Could not open the port map")
        return None
    time.sleep(0.8)
    hit = next(((name, x, y) for name, x, y in read_port_map_buildings(capture_screen())
                if target in name.lower() or token_sim(name, target) >= 0.75), None)
    if hit is None:
        close_port_map()
        logger.error(f"  {target!r} is not on the port map either")
        return None
    return hit[1], hit[2]


def tap_building_entry(building_name: str, frame=None) -> dict:
    """Tap the way into *building_name*, ONCE. Returns {tapped, via, position, reason}.

    `tapped` means a control was pressed — NOT that the bot is inside. Entering takes a walk
    across the port and there is no local signal separating "walking" from "the tap missed",
    so the caller perceives on its next tick and decides. Per the task-runner contract.
    """
    target = building_name.lower()
    frame = frame if frame is not None else capture_screen()

    plate = _nameplate_for(target, frame)
    if plate is not None:
        logger.info(f"  Nameplate {plate.label!r} @ ({plate.cx},{plate.cy}) — tapping to enter")
        tap(plate.cx, plate.cy)
        return {"tapped": True, "via": "nameplate", "position": (plate.cx, plate.cy), "reason": ""}

    tab = select_buildings_tab(frame)
    if not tab["ok"]:
        # Refusing to tap is the correct outcome. Falling through here used to run the fuzzy
        # match over whatever the panel was showing — at Bordeaux that was the Tasks tab, and
        # the match hit the word "market" inside a quest objective.
        logger.error(f"  Not the building list ({tab['reason']}) — refusing to tap {building_name!r}")
        return {"tapped": False, "via": None, "position": None, "reason": tab["reason"]}
    frame = tab["frame"]

    from vision.ocr import read_building_menu
    rows = read_building_menu(frame)
    logger.info(f"  Building list: {[lbl for lbl, *_ in rows]}")
    pos = _building_row(rows, target) or _scroll_list_for(target, frame)
    via = "list"
    if pos is None:
        pos, via = _port_map_entry(target), "port_map"
    if pos is None:
        return {"tapped": False, "via": None, "position": None, "reason": "not found"}

    logger.info(f"  Tapping {building_name!r} ({via}) @ {pos}")
    tap(*pos)
    return {"tapped": True, "via": via, "position": pos, "reason": ""}
