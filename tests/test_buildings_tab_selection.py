"""Selecting the Buildings tab must be DETECTED and VERIFIED, never computed.

Live 2026-08-21, Jakarta: `_buildings_tab_pos()` returned a calibrated ≈(2146,156) while
the real tabs sat at ≈2016 (Buildings) and ≈2114 (Players). The constant landed on
PLAYERS, so the bot selected the player tab itself; the right panel then listed
'kingdavid LV 70' instead of buildings, `navigate_to_building('Market')` timed out after
60s twice, and the mission aborted at gather:Jakarta having bought nothing there.

A wrong tab guess does not fail quietly — it SELECTS something else. So the tabs are
detected, and each candidate is verified by re-reading the list.
"""
from unittest import mock

import actions.sail_actions as sa


class _El:
    def __init__(self, cx, cy, label="icon"):
        self.cx, self.cy, self.label = cx, cy, label
        self.element_type = "icon"


PLAYER_LIST = [("kingdavid", 2200, 300), ("lv 70", 2200, 340), ("theblueberry", 2200, 380)]
# Read live at Jakarta 2026-08-21 on the PLAYER tab. The 'harbor' comes from the standalone
# "⚓ Harbor" shortcut button on the overworld, not from the list.
JAKARTA_PLAYER_TAB = [("tropical", 2200, 300), ("kingdavid", 2200, 340),
                      ("0.#", 2200, 380), ("lv 70", 2200, 420), ("harbor", 2100, 700)]
# Read live at Malé the same day, on the real Buildings tab.
MALE_BUILDINGS_TAB = [(n, 2200, 300 + 40 * i) for i, n in enumerate(
    ["dry", "harbor", "market", "shipyard", "bank", "inn", "sanctuary", "item", "shop", "bureau"])]
BUILDING_LIST = [("harbor", 2200, 300), ("market", 2200, 340), ("shipyard", 2200, 380)]


class TestOnBuildingsTab:
    def test_a_building_list_is_recognised(self):
        assert sa._on_buildings_tab(BUILDING_LIST)

    def test_a_player_list_is_not_a_building_list(self):
        """The exact failure: player names and levels must not read as buildings."""
        assert not sa._on_buildings_tab(PLAYER_LIST)

    def test_an_empty_list_is_not_a_building_list(self):
        assert not sa._on_buildings_tab([])

    def test_the_harbor_shortcut_button_alone_is_not_a_building_list(self):
        """The Jakarta failure: one stray 'harbor' from the overworld shortcut button made
        the gate think it was already on the Buildings tab, so it never switched."""
        assert not sa._on_buildings_tab(JAKARTA_PLAYER_TAB)

    def test_a_real_building_list_is_still_recognised(self):
        assert sa._on_buildings_tab(MALE_BUILDINGS_TAB)


class TestTabStripDetection:
    def _candidates(self, elements):
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=elements):
            return sa._tab_strip_candidates(object())

    def test_finds_tabs_in_the_strip_ordered_left_to_right(self):
        bx0, by0, bx1, by1 = sa._tab_strip_band()
        y = (by0 + by1) // 2
        els = [_El(bx0 + 240, y), _El(bx0 + 80, y), _El(bx0 + 160, y)]
        assert self._candidates(els) == [(bx0 + 80, y), (bx0 + 160, y), (bx0 + 240, y)]

    def test_ignores_elements_outside_the_strip(self):
        """The band filter still rejects what is not in the strip.

        The fixture carries a REAL strip (three evenly spaced icons at one height) because a
        tab strip is a row — a lone icon in the band is not one. That rule exists because an
        event popup's close-X sat inside the band at Bordeaux (2026-08-24) and was offered as
        a tab; tapping popup furniture cannot select the Buildings tab.
        """
        bx0, by0, bx1, by1 = sa._tab_strip_band()
        y = (by0 + by1) // 2
        strip = [_El(bx0 + 80, y), _El(bx0 + 180, y), _El(bx0 + 280, y)]
        below, far_left = _El(bx0 + 80, by1 + 400), _El(50, y)
        assert self._candidates(strip + [below, far_left]) == [(e.cx, e.cy) for e in strip]

    def test_a_lone_icon_in_the_band_is_not_a_tab_strip(self):
        """The Bordeaux case: a popup close-X inside the band, and nothing else."""
        bx0, by0, bx1, by1 = sa._tab_strip_band()
        y = (by0 + by1) // 2
        assert self._candidates([_El(bx0 + 80, y)]) == []

    def test_no_detection_yields_no_candidates_rather_than_a_guess(self):
        """The old code always produced a coordinate, even a wrong one."""
        assert self._candidates([]) == []

    def test_detector_failure_is_survivable(self):
        with mock.patch("vision.omniparser.parse_fast_cached", side_effect=RuntimeError("no yolo")):
            assert sa._tab_strip_candidates(object()) == []
