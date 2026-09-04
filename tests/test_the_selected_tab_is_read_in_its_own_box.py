# tests/test_the_selected_tab_is_read_in_its_own_box.py
#
# THE SELECTED TAB IS THE ONE THE BAND-WIDE PARSE LOSES, and it loses it two ways at once.
#
# `_tab_band_without_bleed` keeps pixels BRIGHTER than the cut. That is right for the
# unselected tabs — light labels on dark, with the dim map bleed dropped — and exactly wrong
# for the selected one, which is a white slab with DARK text: the mask keeps the slab and
# erases the label. Unmasked, a map pin drawn under the strip merges with it instead.
#
# Live 2026-09-03 at Casablanca, both together:
#
#     band OCR   'CasahlanPort'  conf=0.64        the pin's name fused with the tab's
#     masked     (nothing)                        the dark label erased with the bleed
#     row        ['explore', 'route', 'trade']
#     -> 'port' is not in the row -> the tab cannot be selected -> the course cannot be set
#     -> FAILED at step mission: gather:Faro
#
# The bot was ALREADY on the Port tab. It could not read that, so it could not know.
#
# This is the inversion the tab-bleed work recorded and did not act on: "the technique inverts
# by geometry — thin foreground over a translucent strip is brighter, an opaque foreground
# panel is a light slab with dark text." The mask was built for the first case only.

import pathlib

import pytest

pytestmark = pytest.mark.functional

_FRAME = pathlib.Path("tests/fixtures/world_map_casablanca_tabrow.png")


def _img():
    if not _FRAME.exists():
        pytest.skip("real-frame fixture not in the repo")
    from PIL import Image
    return Image.open(_FRAME)


def test_the_row_is_complete_on_the_frame_that_killed_the_mission():
    from actions.sail_actions import _world_map_tab_strip
    assert [t[0] for t in _world_map_tab_strip(_img())] == \
        ["port", "explore", "route", "trade"]


def test_the_active_tab_is_named():
    """The half that mattered: the bot was already on Port, and `select_world_map_tab`
    short-circuits on "already on 'port'" — so a readable row means it need not tap at all."""
    from actions.sail_actions import active_world_map_tab
    assert active_world_map_tab(_img()) == "port"


def test_the_band_wide_read_really_does_lose_it():
    """Pins the DEFECT, so the recovery cannot be quietly deleted as unnecessary."""
    from actions.sail_actions import _ocr_frame
    toks = [t for t, _c, _x, _y in _ocr_frame(_img().crop((600, 0, 1700, 110)))]
    assert not any(t.strip().lower() == "port" for t in toks), \
        "the fixture no longer shows the collision this exists for"
    assert any("port" in t.lower() and len(t) > 6 for t in toks), \
        "expected the merged token, e.g. 'CasahlanPort'"


def test_a_box_that_does_not_name_the_tab_is_left_out():
    """IT IS A READ, NOT A GUESS. The position is computed from the pitch of the tabs that
    WERE read, but the name still has to come off the screen and score. A slot whose crop does
    not name the tab is omitted, the caller gets a short row and refuses — which is the whole
    lesson of `MARKET_COORDS["sell"]`: a fallback that guesses is worse than one that
    refuses."""
    from PIL import Image
    from actions.sail_actions import _recover_missing_tabs
    blank = Image.new("RGB", (2400, 1080), (30, 30, 30))
    found = [("explore", 1047, 47, 80), ("route", 1238, 47, 80), ("trade", 1425, 47, 80)]
    assert [t[0] for t in _recover_missing_tabs(blank, found)] == \
        ["explore", "route", "trade"], "invented a tab from an empty box"


def test_nothing_is_invented_when_the_row_is_already_whole():
    from PIL import Image
    from actions.sail_actions import _recover_missing_tabs
    blank = Image.new("RGB", (2400, 1080), (30, 30, 30))
    whole = [("port", 860, 47, 80), ("explore", 1047, 47, 80),
             ("route", 1238, 47, 80), ("trade", 1425, 47, 80)]
    assert _recover_missing_tabs(blank, whole) == whole


def test_too_few_tabs_to_place_anything_is_left_alone():
    """One tab gives no pitch, so there is nothing to compute a neighbour's box from."""
    from PIL import Image
    from actions.sail_actions import _recover_missing_tabs
    blank = Image.new("RGB", (2400, 1080), (30, 30, 30))
    one = [("route", 1238, 47, 80)]
    assert _recover_missing_tabs(blank, one) == one
