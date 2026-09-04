"""Which WORLD MAP screen is this? — the context-scoped read for the world map.

The outer `classify_nav_state` says `world_map` from the moment the map opens until it
closes, through the rail opening, a destination being selected, and the departure panel
appearing. That verdict is correct and useless: those screens need different actions and it
cannot tell them apart.

WHY THIS EXISTS. `open_world_map` was 151 lines and two nested loops doing five different
jobs at once — waking an OS lock, exiting a building, refusing a village, tapping the right
thing, and verifying. Four of those belong to other layers now (the bootstrap, the clearing
activities, the dispatcher). Live 2026-08-28 it was handed a lock screen, a daily-news popup
and an Investment Season banner, and its three available responses — wait, guess, give up —
were all wrong: it waited for a `TransientActivity` it reached ZERO times, then read "Season"
out of the banner, accepted it as a port name, and reported "Overworld confirmed" over a
scene model that had already said `sub_menu:perk`.

A navigation primitive should not be holding a system-level situation. What is left here is
only the map's own screens.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

MISS = "miss"

MAP_OPEN = "map_open"                  # the map, no rail and no panel
EVENT_SCHEDULE = "event_schedule"            # the Trade Event Schedule dialog
LOCATION_INFO = "location_info"              # a city pin's panel: its own Move button
VILLAGE_INFO_BASE = "village_info_base"      # Village Info, Base tab (amity, rounds)
VILLAGE_INFO_BARTER = "village_info_barter"  # Village Info, Barter tab (the trade list)
DESTINATION_LIST = "destination_list"  # the left rail: ports on Port, villages on Explore
DESTINATION_PANEL = "destination_panel"  # a place is selected; Move / Go is showing
ROUTE_LIST = "route_list"              # the Route tab's saved routes

CONTEXT_STATES = (MAP_OPEN, DESTINATION_LIST, DESTINATION_PANEL, ROUTE_LIST,
                  VILLAGE_INFO_BASE, VILLAGE_INFO_BARTER, EVENT_SCHEDULE, LOCATION_INFO)

# The commit control on a selected destination is what makes this state, and WHERE it sits
# is what identifies it: the bottom action bar. `actions.sail_actions` has owned that rule
# since before the world map was an activity — bare "move" and "destination" matched against
# every label on the frame was this state's signature until 2026-08-29, and the map is full
# of both words.
# The rail is a list when it carries its own search box — every one of them does, which is
# also why "did a panel open?" could never tell the port list from the goods list
# (memory: world-map-state-dies-with-the-map).
_LIST_WORDS = ("search", "filter")
_ROUTE_WORDS = ("route", "routes")
# Village Info opens over the map. Which TAB is showing decides what can be read: Base
# carries amity and the day's rounds, Barter carries the trade list.
_BARTER_TAB_WORDS = ("trade list", "closeout")
_BASE_TAB_WORDS = ("amity", "barters")
# The Trade Event Schedule, opened from the map's bottom-left. Mayors schedule market events
# and a Bazaar makes the named category fetch far more, so it is a selling opportunity.
# THE TRADE EVENT SCHEDULE DIALOG — asked of the reader that already owns it.
#
# `vision.trade_event_reader` has read this dialog since the Bremen event run, and it finds
# its own columns from the header row (`Market Event | Trade Goods | Fixed-term | Location`).
# That header IS the dialog's signature, so presence is its question, not ours — one home
# per concern, and presence can never disagree with what the reader will then read.
#
# The classifier used to match the phrase against every label on the frame, and two
# permanent things carry the name without being it: the rail button that OPENS the dialog
# (x=131) and a port panel's 'Market Event Schedule' row (x=1959) — the panel that also
# carries MOVE, so every attempt to commit a course read as this dialog (live 2026-08-29).


def _commit_control_showing(elements) -> bool:
    """Is a destination's commit control in the bottom action bar?

    Asked of `sail_actions.destination_commit_control`, which is also what TAPS it — so the
    state and the action cannot disagree about whether there is a button.
    """
    try:
        from actions.sail_actions import destination_commit_control
        return destination_commit_control(elements=elements) is not None
    except Exception as exc:
        logger.debug(f"[world_map_context] commit control unreadable: {exc}")
        return False


def _bare_move_showing(elements) -> bool:
    """Is a saved route's bare `Move` button on screen? Asked of the action layer, which is
    also what taps it — so the state and the action cannot disagree about whether it exists."""
    try:
        from actions.sail_actions import _bare_move_control
        return _bare_move_control(list(elements or [])) is not None
    except Exception as exc:
        logger.debug(f"[world_map_context] bare Move unreadable: {exc}")
        return False


def _right_panel(frame, elements):
    """The world map's right-hand info panel, or None.

    Asked of `vision.region_detectors.panels.detect_right_panel`, which already names this
    case in its own docstring ("World Map / City Info") and tells a real panel from the map
    canvas behind it — an x-range cannot, because the map draws port names in exactly those
    pixels when no panel is open.
    """
    try:
        from vision.region_detectors.panels import detect_right_panel
        w, h = getattr(frame, "width", 0) or 0, getattr(frame, "height", 0) or 0
        if not (w and h):
            return None
        return detect_right_panel(list(elements or []), w, h)
    except Exception as exc:
        logger.debug(f"[world_map_context] right panel unreadable: {exc}")
        return None


def _event_schedule_open(elements) -> bool:
    from vision.trade_event_reader import schedule_dialog_present
    return schedule_dialog_present(elements=elements)
# A city pin opens LOCATION INFO, which has its OWN Move button — a different way to depart
# than the destination panel (memory: trade-event-selling-flow).
# THE RIGHT PANEL'S TITLE SAYS WHAT WAS TAPPED. One panel, read three ways: a map pin gives
# 'City Info', an event row's pin gives 'Location Info' (both carry the gold Move button), a
# village gives 'Village Info' with its own tabs.
#
# These were matched as substrings over every label on the frame, and that failed BOTH ways:
# 'City Info' arrives SPLIT as 'City' + 'Info', so the joined text never contained the phrase
# and Amsterdam's panel — the one carrying Move — classified as MAP_OPEN (live 2026-08-29);
# and the list this checked, ("location info",), was not even the map panel's title.
_CITY_PANEL_TITLES = ("city info", "location info")
_VILLAGE_PANEL_TITLE = "village info"


def _search_box(frame, elements) -> bool:
    """Is the rail's search box on screen, whatever it currently says?"""
    try:
        from actions.sail_actions import search_box_present
        return search_box_present(frame, elements)
    except Exception:
        return False


def classify(frame, *, elements=None, tab: Optional[str] = None) -> str:
    """The world-map-scoped state, or MISS.

    `tab` is the lit world-map tab when already known; it is READ, never assumed — ports and
    villages live on different tabs and the left rail belongs to whichever is lit
    (memory: look-first-then-type-never-scroll).
    """
    if elements is None:
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
    labels = [(getattr(e, "label", "") or "").strip().lower() for e in elements or []]
    text = " | ".join(l for l in labels if l)

    # THE DIALOGS OVER THE MAP, BEFORE THE MAP'S OWN SCREENS. Each opens on top, and each
    # carries wording that would otherwise read as something beneath it — Location Info's
    # Move button reads exactly like the destination panel's.
    if _event_schedule_open(elements):
        return EVENT_SCHEDULE
    # THE RIGHT PANEL, BEFORE THE MAP'S OWN SCREENS. It opens OVER the map and over the
    # rail, and it carries a Move button whose wording reads exactly like the bottom bar's.
    panel = _right_panel(frame, elements)
    title = ((getattr(panel, "title", "") or "") if panel else "").lower()
    if any(t in title for t in _CITY_PANEL_TITLES):
        return LOCATION_INFO
    if _VILLAGE_PANEL_TITLE in title:
        # WHICH TAB, read INSIDE the panel. Frame-wide, the map behind it gets a vote.
        x_min = panel.bbox[0] if getattr(panel, "bbox", None) else 0
        within = " | ".join(
            (getattr(e, "label", "") or "").strip().lower()
            for e in (elements or []) if (getattr(e, "cx", 0) or 0) >= x_min
        )
        if any(w in within for w in _BARTER_TAB_WORDS):
            return VILLAGE_INFO_BARTER
        return VILLAGE_INFO_BASE

    # THE PANEL NEXT. It sits over the rail, so reading the rail underneath and calling it a
    # list would tap a row while a departure is waiting to be confirmed.
    if _commit_control_showing(elements):
        return DESTINATION_PANEL

    if tab is None:
        tab = _active_tab(frame)

    # A SAVED ROUTE'S COMMIT BUTTON IS A BARE 'Move', and the rail stays visible behind it —
    # so the panel check above cannot see it and this screen read as a plain ROUTE_LIST. The
    # handler's only move is "find the route and tap it", which it had already done, so it
    # re-tapped the selected row until the stall guard ended the mission (live 2026-09-03,
    # 4,597 units aboard and the route drawn on the map).
    #
    # The word alone is not enough — a bare `Move` is also the sea-waypoint marker for a tap
    # that missed a port. The ROUTE TAB is what disambiguates it, which is why this is asked
    # here, after the tab is known, and not in the generic panel check.
    if tab == "route" and _bare_move_showing(elements):
        return DESTINATION_PANEL

    if tab == "route" and any(w in text for w in _ROUTE_WORDS):
        return ROUTE_LIST
    # A RAIL THAT HAS BEEN TYPED INTO IS STILL A RAIL. The word test alone missed it: an
    # empty search box reads 'Search' and a filled one reads the query, so a list the bot had
    # typed into classified as MAP_OPEN and it went on looking at the map while standing in
    # the list (live 2026-08-29). The box is found by where it sits instead.
    if any(w in text for w in _LIST_WORDS) or _search_box(frame, elements):
        return DESTINATION_LIST

    if _looks_like_the_map(text, elements):
        return MAP_OPEN
    return MISS


def _active_tab(frame) -> Optional[str]:
    try:
        from actions.sail_actions import active_world_map_tab
        return active_world_map_tab(frame)
    except Exception as exc:
        logger.debug(f"[world-map-context] tab unreadable: {exc}")
        return None


def _looks_like_the_map(text: str, elements) -> bool:
    """The tab row is the map's own furniture and is present on every one of its screens."""
    return sum(1 for t in ("port", "explore", "route", "trade") if t in text) >= 3
