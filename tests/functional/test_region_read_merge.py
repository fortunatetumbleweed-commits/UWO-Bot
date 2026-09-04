"""Reading content from a region, and merging it with the whole-frame parse.

Built from a real frame: the Village Info trade list where the full 2400x1080 parse never
proposed the `44` on Matchlock Gun's thumbnail as text, while a x2 region read it.

Neither read is correct alone, and that is the point:

    whole-frame: {Birch Tree: 358, Iron: 102,                   Candle: 102}
    region  x2 : {Birch Tree: 358, Iron: 102, Matchlock Gun: 44, Candle: 402}
    MERGED     :                   Iron  102, Matchlock Gun  44, Candle  102
"""

from __future__ import annotations

import types
import unittest

from vision.region_read import merged_rows, rows_by_association

ROWS = ["Birch Tree", "Iron", "Matchlock Gun", "Candle"]


def _el(label, y, x=1800):
    return types.SimpleNamespace(label=label, x1=x, x2=x + 100, y1=y, y2=y + 40)


class AValueBindsToTheNameAboveIt(unittest.TestCase):
    """A row's quantity is drawn BELOW its name."""

    def test_the_trade_list_layout(self):
        els = [_el("Birch Tree", 330), _el("358", 400),
               _el("Iron", 548), _el("102", 600),
               _el("Matchlock Gun", 659), _el("44", 700),
               _el("Candle", 771), _el("102", 810)]
        self.assertEqual(rows_by_association(els, ROWS),
                         {"Birch Tree": 358, "Iron": 102, "Matchlock Gun": 44, "Candle": 102})

    def test_a_value_above_every_named_row_is_dropped(self):
        """Binding to the name ABOVE is what makes this safe: Birch Tree's output `358` has
        no named row above it, so it is dropped rather than attached to Iron. Under the
        NEAREST-name rule tried first, it bound to Iron and displaced the real 102."""
        els = [_el("Birch Tree", 330), _el("358", 400), _el("Iron", 548), _el("102", 600)]
        self.assertEqual(rows_by_association(els, ["Iron"]), {"Iron": 102})

    def test_an_unnamed_row_below_does_not_corrupt_the_row_above_it(self):
        """The reason to name EVERY row: Matchlock's 44 must not become Iron's value just
        because Matchlock was not listed. It binds to Iron, but Iron already has its own —
        so the rule that saves this is topmost-wins, tested below."""
        els = [_el("Iron", 548), _el("102", 600), _el("Matchlock Gun", 659), _el("44", 700)]
        self.assertEqual(rows_by_association(els, ["Iron"])["Iron"], 102)

    def test_the_topmost_value_wins_not_the_first_parsed(self):
        """Elements come back in whatever order the parse produced. Sorting by y makes
        "first" mean "topmost", which is what the layout means."""
        els = [_el("44", 700), _el("102", 600), _el("Iron", 548)]      # 44 first in the list
        self.assertEqual(rows_by_association(els, ["Iron"])["Iron"], 102)

    def test_a_row_with_no_value_is_absent_not_zero(self):
        els = [_el("Iron", 548), _el("102", 600), _el("Matchlock Gun", 659)]
        got = rows_by_association(els, ROWS)
        self.assertNotIn("Matchlock Gun", got)
        self.assertIsNone(got.get("Matchlock Gun"))

    def test_no_names_yields_nothing_rather_than_guessing(self):
        self.assertEqual(rows_by_association([_el("102", 600)], ROWS), {})


class TheMergeLetsEachReadCoverTheOther(unittest.TestCase):

    def _merge(self, whole, region):
        frame = types.SimpleNamespace(width=2400, height=1080,
                                      crop=lambda box: types.SimpleNamespace(
                                          width=548, height=582,
                                          resize=lambda wh: types.SimpleNamespace(box=wh)))
        return merged_rows(frame, (1687, 383, 2235, 965), ROWS,
                           whole_elements=whole, parse=lambda _c: region)

    def test_the_live_case(self):
        whole = [_el("Birch Tree", 330), _el("358", 400), _el("Iron", 548), _el("102", 600),
                 _el("Matchlock Gun", 659), _el("Candle", 771), _el("102", 810)]
        region = [_el("Birch Tree", 330), _el("358", 400), _el("Iron", 548), _el("102", 600),
                  _el("Matchlock Gun", 659), _el("44", 700), _el("Candle", 771),
                  _el("402", 810)]
        got = self._merge(whole, region)
        self.assertEqual(got["Iron"], 102)
        self.assertEqual(got["Matchlock Gun"], 44, "the region fills what the frame missed")
        self.assertEqual(got["Candle"], 102, "the frame overrides the region's misread 402")

    def test_the_whole_frame_wins_where_it_has_a_value(self):
        """The region is the LESS reliable read and exists to supply what the frame could
        not see at all — never to overrule it."""
        whole = [_el("Iron", 548), _el("102", 600)]
        region = [_el("Iron", 548), _el("999", 600)]
        self.assertEqual(self._merge(whole, region)["Iron"], 102)


class TheRegionIsUpscaled(unittest.TestCase):
    """Cropping alone is not the fix — the derived 548x582 crop does not read the `44`."""

    def test_the_default_scale_is_at_least_two(self):
        from vision.region_read import _MIN_SCALE
        self.assertGreaterEqual(_MIN_SCALE, 2)

    def test_the_crop_is_resized_before_parsing(self):
        from vision.region_read import read_region
        seen = {}

        class _Crop:
            width, height = 548, 582
            def resize(self, wh):
                seen["resized_to"] = wh
                return self

        frame = types.SimpleNamespace(crop=lambda box: _Crop())
        read_region(frame, (0, 0, 548, 582), scale=2, parse=lambda c: [])
        self.assertEqual(seen["resized_to"], (1096, 1164))

    def test_region_coordinates_are_not_mapped_back_to_the_frame(self):
        """Deliberate: handing back frame coordinates would invite the positional merge that
        was the FIRST wrong attempt — the parses disagree on box geometry."""
        from vision.region_read import read_region
        el = _el("44", 700)

        class _Crop:
            width, height = 10, 10
            def resize(self, wh):
                return self

        frame = types.SimpleNamespace(crop=lambda box: _Crop())
        got = read_region(frame, (1687, 383, 2235, 965), scale=2, parse=lambda c: [el])
        self.assertEqual(got[0].y1, 700, "unchanged — still region coordinates")


if __name__ == "__main__":
    unittest.main()


class AgainstTheRealFrame(unittest.TestCase):
    """The frame the whole thing was built from. The parse tests above use elements I typed;
    this one reads the actual pixels, which is where the `44` was lost."""

    FRAME = "tests/stage_suite/frames/village_trade_list_missing_quantity.png"

    def _frame(self):
        import os
        if not os.path.exists(self.FRAME):
            self.skipTest("stage frame not available")
        from PIL import Image
        return Image.open(self.FRAME)

    def _derived_box(self, els):
        """The region from the panel's OWN landmarks — no constants."""
        from actions.village_check import _list_viewport
        top, bottom = _list_viewport(els)
        marks = [e for e in els if (e.label or "").strip().lower().startswith(
                 ("trade list", "closeout", "view by min"))]
        return (min(e.x1 for e in marks), top, max(e.x2 for e in marks), bottom)

    def test_the_merge_recovers_all_three_quantities(self):
        from vision.omniparser import parse_fast_cached
        frame = self._frame()
        els = list(parse_fast_cached(frame))
        got = merged_rows(frame, self._derived_box(els), ROWS, whole_elements=els)
        self.assertEqual(got.get("Iron"), 102)
        self.assertEqual(got.get("Matchlock Gun"), 44, "the region supplies what the "
                                                       "whole frame never proposed as text")
        self.assertEqual(got.get("Candle"), 102, "the whole frame overrides the region's 402")

    def test_the_whole_frame_alone_loses_the_quantity(self):
        """The failure this exists to fix, pinned so it cannot quietly stop being true."""
        from vision.omniparser import parse_fast_cached
        frame = self._frame()
        whole = rows_by_association(parse_fast_cached(frame), ROWS)
        self.assertEqual(whole.get("Iron"), 102)
        self.assertIsNone(whole.get("Matchlock Gun"))
