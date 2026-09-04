"""Which world-map screen is this? The outer classifier says `world_map` for all of them.

From the map opening, through the rail, a destination being selected, and the departure
panel appearing, `classify_nav_state` says `world_map` throughout — correct and useless,
because those screens need different actions.
"""
import pathlib
import types

import pytest

from brain.world_map_context import (DESTINATION_LIST, DESTINATION_PANEL, EVENT_SCHEDULE,
                                     LOCATION_INFO, MAP_OPEN, MISS, ROUTE_LIST,
                                     VILLAGE_INFO_BARTER, classify)


def _els(*labels):
    return [types.SimpleNamespace(label=l) for l in labels]


TABS = _els("Port", "Explore", "Route", "Trade")


def test_the_bare_map_is_its_tab_row():
    assert classify(None, elements=TABS, tab="port") == MAP_OPEN


def test_an_open_rail_is_a_list():
    assert classify(None, elements=TABS + _els("Search"), tab="port") == DESTINATION_LIST


def test_a_selected_destination_is_a_panel():
    els = TABS + [_at("Move to Village", 1145, _ACTION_BAR_Y)]
    assert classify(None, elements=els, tab="explore") == DESTINATION_PANEL


def test_the_same_words_above_the_action_bar_are_not_a_panel():
    """The info panel says 'Move to ...' too. Position is what separates them — matching the
    words anywhere on the frame is how this state came to fire on the map itself."""
    els = TABS + [_at("Move to Village", 1961, 420)]
    assert classify(None, elements=els, tab="explore") != DESTINATION_PANEL


def test_the_panel_outranks_the_rail_beneath_it():
    """It sits OVER the list, so reading the list underneath and calling it a list would tap
    a row while a departure is waiting to be confirmed."""
    els = TABS + _els("Search") + [_at("Go to City", 1145, _ACTION_BAR_Y)]
    assert classify(None, elements=els, tab="port") == DESTINATION_PANEL


def test_the_route_tab_has_its_own_list():
    assert classify(None, elements=TABS + _els("Route", "Search"), tab="route") == ROUTE_LIST


def test_something_that_is_not_the_map_is_a_MISS():
    """The idle lock, a perk banner, the port overworld — none are world-map screens, and a
    miss is the normal signal that the context has expired."""
    assert classify(None, elements=_els("Harbor", "Market", "Shipyard"), tab=None) == MISS
    assert classify(None, elements=_els("Slide up to unlock"), tab=None) == MISS


def test_a_perk_banner_is_not_the_map():
    """The 2026-08-28 screen that `_is_on_overworld` read "Season" out of and called a port."""
    assert classify(None, elements=_els("Investment Season 7", "Starlight Event Now Live"),
                    tab=None) == MISS


# ── the Trade Event Schedule is a DIALOG, not a phrase ───────────────────────
#
# Two permanent things on the world map carry its wording, so a screen where nothing was
# opened read as the schedule. The second one cost a voyage: the row sits in the port panel
# that also carries MOVE, so every attempt to commit a course backed out of it.

def _at(label, cx, cy=0):
    return types.SimpleNamespace(label=label, cx=cx, cy=cy)


# The commit control lives in the bottom action bar and nowhere else — `sail_actions` has
# said so since before the world map was an activity. A "Move to ..." label anywhere else is
# the info panel's own wording, or the map.
_ACTION_BAR_Y = 1010


def test_the_rail_button_that_opens_the_schedule_is_not_the_schedule():
    """'Trade Event / Schedule' is always in the left rail — it is the way IN, not the room."""
    els = TABS + [_at("Trade Event Schedule", 131)]
    assert classify(None, elements=els, tab="port") != EVENT_SCHEDULE


def test_a_port_panels_market_event_row_is_not_the_schedule():
    """LIVE 2026-08-29, Amsterdam. The panel carrying Move also carries 'Market Event
    Schedule' at x=1959, the joined-text match fired, and the course could never be set."""
    els = TABS + [_at("Market Event Schedule", 1959),
                  _at("Go to City", 1145, _ACTION_BAR_Y)]
    assert classify(None, elements=els, tab="port") == DESTINATION_PANEL


# The dialog's HEADER ROW is its signature — the four columns it is built from. Nothing
# else on the world map carries three of them, and the two things that carry its NAME carry
# none. `vision.trade_event_reader` owns this test; the classifier asks it.
_SCHEDULE_HEADERS = [("Market Event", 553), ("Trade Goods", 830),
                     ("Fixed-term", 1100), ("Location", 1500)]


def test_the_dialog_itself_is_the_schedule():
    """Its title AND its header row — that is what the rail button opens."""
    els = TABS + [_at("Trade Event Schedule", 1200)] + [_at(t, x) for t, x in _SCHEDULE_HEADERS]
    assert classify(None, elements=els, tab="port") == EVENT_SCHEDULE


def test_the_name_alone_is_not_the_dialog():
    """Even dead centre. The rail button can OCR as one merged label, and a merged label in
    the middle of the map would otherwise be indistinguishable from the dialog."""
    els = TABS + [_at("Trade Event Schedule", 1200)]
    assert classify(None, elements=els, tab="port") != EVENT_SCHEDULE


def test_the_real_dialog_on_a_real_frame():
    """The POSITIVE case, from the live dialog (captured 2026-08-29, user opened it).

    The synthetic tests above prove the two false positives are excluded. Only a real frame
    proves the true dialog is still admitted — and it is the frame that shows why the band
    works: the title lands at x=1200, the exact centre of 2400, while the rail's permanent
    button is at x=132 in the same frame.
    """
    frame_path = pathlib.Path("data/reference/world_map/event_schedule_dialog.png")
    if not frame_path.exists():
        pytest.skip("reference frame not present")
    from PIL import Image
    from vision.omniparser import parse_fast_cached
    frame = Image.open(frame_path)
    assert classify(frame, elements=parse_fast_cached(frame)) == EVENT_SCHEDULE


# ── the right panel, read from real frames ───────────────────────────────────
#
# One panel, three answers, decided by its TITLE — which is what was tapped. Substring tests
# over the joined frame text got all three wrong: 'City Info' arrives split as 'City' +
# 'Info' so the phrase was never there, and the list being matched, ("location info",), was
# not the map panel's title in the first place.

_PANEL_FRAMES = [
    ("data/reference/world_map/city_info_panel.png", LOCATION_INFO),
    ("tests/stage_suite/frames/village_trade_list_missing_quantity.png", VILLAGE_INFO_BARTER),
    ("tests/stage_suite/frames/world_map_village_list.png", DESTINATION_LIST),
]


@pytest.mark.parametrize("path,expected", _PANEL_FRAMES)
def test_the_right_panel_is_read_from_its_title(path, expected):
    frame_path = pathlib.Path(path)
    if not frame_path.exists():
        pytest.skip(f"{path} not present")
    from PIL import Image
    from vision.omniparser import parse_fast_cached
    frame = Image.open(frame_path).convert("RGB")
    assert classify(frame, elements=parse_fast_cached(frame)) == expected


def test_the_split_title_is_why_the_words_failed():
    """Amsterdam's panel — the one carrying MOVE — went unrecognised because OmniParser
    returned its title as two elements. Named here so the regression is legible."""
    frame_path = pathlib.Path("data/reference/world_map/city_info_panel.png")
    if not frame_path.exists():
        pytest.skip("reference frame not present")
    from PIL import Image
    from vision.omniparser import parse_fast_cached
    frame = Image.open(frame_path).convert("RGB")
    labels = [(getattr(e, "label", "") or "").strip() for e in parse_fast_cached(frame)]
    assert "City Info" not in labels, "the title is split — that is the whole point"
    assert {"City", "Info"} <= set(labels)
