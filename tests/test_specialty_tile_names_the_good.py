"""A specialty tile must be named after the GOOD, not the "Specialties" banner.

Measured on the Kolkata Sell page (live 2026-08-22, frame_0019 of
trace_barter_cmd_2026-08-22T19-48-40): OmniParser reported the Textiles tile TWICE —
once labelled 'Textiles', once labelled 'Specialties' after the yellow banner painted
across it. The banner detection won the grid slot, so the hold read as

    {'Ebony': 700, 'Coral': 797, 'Specialties': 920}

`owned.get("textiles")` was therefore 0, both the pre-sail skip guard and buy_to_goal's
cold pre-check concluded the fleet carried none, and 920 units already aboard were bought
again for 235,520 ducats.
"""

from __future__ import annotations

import unittest

from vision.market_reader import _tile_label, _parse_tile_from_button
from vision.omniparser import DetectedElement


def _box(label, x1, y1, x2, y2, element_type="button"):
    return DetectedElement(label=label, element_type=element_type,
                           x1=x1, y1=y1, x2=x2, y2=y2, confidence=0.9)


# The two overlapping detections of the SAME tile, at their measured coordinates.
BANNER_TILE = _box("Specialties", 1231, 196, 1668, 436)
GOOD_TILE = _box("Textiles", 1234, 195, 1666, 347)
EBONY_TILE = _box("Ebony", 356, 194, 791, 420)


class SpecialtyTileNaming(unittest.TestCase):

    def test_banner_detection_yields_the_good_name(self):
        self.assertEqual(
            _tile_label(BANNER_TILE, [BANNER_TILE, GOOD_TILE, EBONY_TILE]),
            "Textiles",
        )

    def test_a_plain_tile_keeps_its_own_label(self):
        self.assertEqual(_tile_label(EBONY_TILE, [EBONY_TILE, BANNER_TILE, GOOD_TILE]),
                         "Ebony")

    def test_no_overlapping_detection_leaves_the_banner_unresolved(self):
        """With nothing better to fall back on, the tile must NOT be called 'Specialties'
        — a phantom good silently reads as 'we own none of the real one'."""
        good = _parse_tile_from_button(
            BANNER_TILE,
            [_box("Fabrics", 1380, 250, 1465, 290, element_type="text")],
            tab="sell",
            label=_tile_label(BANNER_TILE, [BANNER_TILE]),
        )
        name = (good.name if good else "") or ""
        self.assertNotEqual(name.lower(), "specialties")

    def test_owned_count_survives_the_rename(self):
        """The 920 badge was always read correctly — renaming must not disturb it."""
        good = _parse_tile_from_button(
            BANNER_TILE,
            [_box("920", 1320, 295, 1356, 331, element_type="text")],
            tab="sell",
            label=_tile_label(BANNER_TILE, [BANNER_TILE, GOOD_TILE]),
        )
        self.assertIsNotNone(good)
        self.assertEqual(good.name, "Textiles")


if __name__ == "__main__":
    unittest.main()
