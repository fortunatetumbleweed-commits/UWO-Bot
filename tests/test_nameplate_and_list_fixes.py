"""Building-name-plate entry + building-list signature fixes.

Pins:
  - `_is_building_list_noise` recognises clock / season / month tokens
    so the right-panel header row stops polluting the building list.
  - The building-list region cap was raised to 0.97 so Fortune Teller
    (cy ~1030 in a 1080-height frame) is captured.
  - Obstruction classifier now consumes the role-tagged inventory and
    excludes chrome/player-UI roles — "Happy 2026" (appellation) and
    building nameplates can never be misread as popups/overlays.
"""

from __future__ import annotations

import unittest

from vision.ocr import _BUILDING_MENU_REGION_NORM, _is_building_list_noise


class BuildingListHeaderNoiseTests(unittest.TestCase):

    def test_clock_tokens_filtered(self):
        for tok in ["11.42", "00:00", "14:32", "23:59", "9.34"]:
            self.assertTrue(
                _is_building_list_noise(tok),
                f"expected {tok!r} to be filtered as clock noise",
            )

    def test_season_tokens_filtered(self):
        for tok in ["autumn", "Autumn", "WINTER", "Spring", "Summer",
                    "Wet Season", "Dry Season"]:
            self.assertTrue(
                _is_building_list_noise(tok),
                f"expected {tok!r} to be filtered as season noise",
            )

    def test_month_tokens_filtered(self):
        for tok in ["jan", "Feb", "MAR", "apr", "may", "Jun", "jul",
                    "AUG", "sep", "sept", "Oct", "nov", "Dec"]:
            self.assertTrue(
                _is_building_list_noise(tok),
                f"expected {tok!r} to be filtered as month noise",
            )

    def test_real_buildings_not_filtered(self):
        for tok in ["harbor", "market", "fortune teller", "venturer association",
                    "item shop", "bureau", "palace", "inn"]:
            self.assertFalse(
                _is_building_list_noise(tok),
                f"unexpected filter on building name {tok!r}",
            )

    def test_building_list_region_captures_bottom_row(self):
        # Fortune Teller appears at cy ~1030 in 1080-height frames.
        # The region must include it.
        _, _, _, y_max = _BUILDING_MENU_REGION_NORM
        self.assertGreaterEqual(y_max, 0.95,
                                 f"y_max={y_max} would clip Fortune Teller at cy~1030")


class ObstructionClassifierExcludesChromeTests(unittest.TestCase):
    """The classifier reads the tagged inventory and drops chrome /
    player-UI roles before its size/position detectors run.  This kills
    the false-fires on appellation / NPC bubble / building nameplate
    elements that we saw on port_overworld frames."""

    def _make_inv(self, raw_elements, tagged):
        class _Inv:
            pass
        inv = _Inv()
        inv.raw_elements = raw_elements
        inv.tagged = tagged
        inv.frame_dims = (2400, 1080)
        return inv

    def test_appellation_alone_does_not_classify_overlay(self):
        """The player appellation ('Happy 2026') is at a fixed central
        position and is tagged as role=appellation.  It must NOT be
        treated as a vertical-pair text element for overlay detection."""
        from vision.element_postprocess import TaggedElement
        from vision.obstruction_classifier import (
            KIND_OVERLAY, classify_obstruction,
        )
        from vision.omniparser import DetectedElement

        appell = DetectedElement(
            label="Happy 2026", element_type="text",
            x1=1100, y1=355, x2=1290, y2=395, confidence=0.95,
        )
        player_nameplate = DetectedElement(
            label="TaylorFP", element_type="text",
            x1=1140, y1=465, x2=1330, y2=515, confidence=0.95,
        )
        bnp = DetectedElement(
            label="Fortune Teller", element_type="button",
            x1=974, y1=80, x2=1174, y2=164, confidence=0.7,
        )
        tagged = [
            TaggedElement(raw=appell,           role="appellation"),
            TaggedElement(raw=player_nameplate, role="player_nameplate"),
            TaggedElement(raw=bnp,              role="building_nameplate"),
        ]
        inv = self._make_inv([appell, player_nameplate, bnp], tagged)
        result = classify_obstruction(inv)
        self.assertNotEqual(
            result.kind, KIND_OVERLAY,
            f"chrome-only frame must not classify as overlay; got {result.kind} signals={result.signals}",
        )


if __name__ == "__main__":
    unittest.main()
