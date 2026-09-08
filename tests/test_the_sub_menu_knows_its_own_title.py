"""The market's Purchase page must name itself, dialog or no dialog.

`TOP_LEFT_TITLE` was authored against a BUILDING title, which centres at cy≈0.05. A market
sub-menu title centres at cy≈0.15, outside that box, so `sub_menu` never matched on the thing
its own description calls its only signal. It matched instead on an intermittent duplicate
detection near the top edge — present in 198 of 552 looks in the 2026-09-08 run.

The two frames here are the looks where that duplicate was absent: the Purchase page with a
Replenish Stock dialog over it. Both came back `learned_cargo_kris`, a learned fingerprint
keyed on the names of two goods on the shelf, which no activity serves. The dispatcher then
issued ENTER_BUILDING at a bot already standing in the market, sixteen times.

A screen that cannot say its own name is the defect. The learned fingerprint was only what
filled the silence.
"""

from __future__ import annotations

import os
import unittest

from PIL import Image

FRAMES = os.path.join(os.path.dirname(__file__), "..", "data", "test_frames",
                      "sub_menu_title")


class ThePurchasePageNamesItself(unittest.TestCase):

    def _classify(self, name):
        import vision.state_fingerprints_data          # registers the fingerprints
        from vision.state_fingerprints import classify_via_registry
        from vision.omniparser import parse_fast_cached
        frame = Image.open(os.path.join(FRAMES, name))
        found = classify_via_registry(parse_fast_cached(frame),
                                      frame.width, frame.height)
        return getattr(found, "state", None), getattr(found, "detail", "") or ""

    def test_the_purchase_page_under_a_dialog_is_still_the_purchase_page(self):
        for name in ("purchase_under_replenish_dialog_0040.png",
                     "purchase_under_replenish_dialog_0042.png"):
            with self.subTest(frame=name):
                state, detail = self._classify(name)
                self.assertEqual("sub_menu", state,
                                 f"{name} is the market Purchase page")
                self.assertIn("purchase", detail.lower())

    def test_the_named_state_is_one_an_activity_serves(self):
        """A name nothing serves is worse than no name: `unknown` has an owner."""
        from brain.run_goal import default_activities
        served = set(default_activities())
        state, detail = self._classify("purchase_under_replenish_dialog_0040.png")
        where = f"{state}:{detail.split(':')[-1].strip()}" if ":" in detail else state
        self.assertIn(where, served,
                      f"{where!r} must be a state some activity serves")


class TheSubMenuTitleBoxCoversTheTitle(unittest.TestCase):

    def test_the_box_reaches_where_a_sub_menu_title_actually_sits(self):
        """Measured at cy 0.13-0.17 over 354 detections; the box must contain that."""
        from vision.state_fingerprints_data import SUB_MENU_TITLE
        _l, top, _r, bottom = SUB_MENU_TITLE
        self.assertLessEqual(top, 0.13, "a sub-menu title centres no higher than 0.13")
        self.assertGreaterEqual(bottom, 0.17, "and no lower than 0.17")


if __name__ == "__main__":
    unittest.main()
