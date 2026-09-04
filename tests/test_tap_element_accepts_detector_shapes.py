"""A detected element reaches `tap_element` in one of two shapes, and both must tap.

OmniParser yields objects with `.cx`/`.cy`. The region detectors (`detect_left_menu` and
friends) yield DICTS carrying the same keys. `_centre` read attributes only, so every
dict-shaped element reported "no position" and `tap_element` returned False WITHOUT TAPPING.

Live at San Village 2026-08-24: the bot read the menu correctly, found Barter at (85, 617),
handed the dict to `tap_element` — and the tap silently did not happen. It then reported the
Barter panel as impossible to open and abandoned the mission with the fleet standing at the
village holding both materials. A hand-fired tap at that exact coordinate opened the panel
first time, which is what proves the detection was never the problem.

The warning said "element has no position". The element had a perfectly good position; the
HANDOFF between two internal shapes is what dropped it.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from actions import ui


class BothDetectorShapesTap(unittest.TestCase):

    def _tap(self, el):
        taps = []
        with patch("actions.adb_actions.tap", side_effect=lambda x, y: taps.append((x, y))), \
             patch.object(ui, "settle", lambda *_a, **_k: None):
            ok = ui.tap_element(el, why="test")
        return ok, taps

    def test_a_region_detector_dict_taps(self):
        """The exact shape `detect_left_menu` returns for the San Village Barter row."""
        item = {"label": "Barter", "bbox": (31, 599, 139, 635), "cx": 85, "cy": 617,
                "is_locked": False, "is_selected": False}
        ok, taps = self._tap(item)
        self.assertTrue(ok, "a dict-shaped element must tap, not report 'no position'")
        self.assertEqual(taps, [(85, 617)])

    def test_an_omniparser_object_taps(self):
        ok, taps = self._tap(types.SimpleNamespace(label="Barter", cx=85, cy=617))
        self.assertTrue(ok)
        self.assertEqual(taps, [(85, 617)])

    def test_a_dict_with_only_a_bbox_taps_its_centre(self):
        ok, taps = self._tap({"label": "Barter", "bbox": (31, 599, 139, 635)})
        self.assertTrue(ok)
        self.assertEqual(taps, [(85, 617)])

    def test_a_positionless_element_still_refuses(self):
        """The guard must survive: no position means no blind tap."""
        ok, taps = self._tap({"label": "Barter"})
        self.assertFalse(ok)
        self.assertEqual(taps, [], "it must not tap when it genuinely has no position")


if __name__ == "__main__":
    unittest.main()
