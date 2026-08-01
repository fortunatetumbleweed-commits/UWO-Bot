"""Tests for the horizontal title-merge step in top_left detector."""
import unittest

from vision.region_detectors.top_left import (
    _merge_adjacent_titles,
)


def el(label, x1, y1, x2, y2, etype="text"):
    return type("FakeEl", (), {
        "label": label,
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
        "width": x2 - x1, "height": y2 - y1,
        "element_type": etype,
    })()


class TitleMergeTests(unittest.TestCase):

    def test_world_map_fragments_merge(self):
        """OmniParser splits 'World Map' into two same-line elements."""
        merged = _merge_adjacent_titles([
            el("World", 100, 50, 220, 100),
            el("Map",   235, 52, 315, 102),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].label, "World Map")
        # Bounding box encloses both
        self.assertEqual(merged[0].x1, 100)
        self.assertEqual(merged[0].x2, 315)

    def test_different_lines_not_merged(self):
        merged = _merge_adjacent_titles([
            el("World", 100, 50, 220, 100),
            el("Map",   100, 200, 200, 250),
        ])
        self.assertEqual(len(merged), 2)

    def test_distant_horizontal_elements_not_merged(self):
        """Two title elements far apart shouldn't merge."""
        merged = _merge_adjacent_titles([
            el("Title",       100, 50, 220, 100),
            el("OtherText",  1500, 52, 1700, 102),
        ])
        self.assertEqual(len(merged), 2)

    def test_overlapping_button_and_text_merge(self):
        """Real case: 'World' button bbox encloses 'Map' text element."""
        merged = _merge_adjacent_titles([
            el("World", 144,  7, 435,  89, etype="button"),
            el("Map",   336, 17, 442,  81, etype="text"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].label, "World Map")

    def test_overlapping_with_duplicate_label_dedupes(self):
        """Button + inner text both labelled 'World' → single 'World'."""
        merged = _merge_adjacent_titles([
            el("World", 144,  7, 435,  89, etype="button"),
            el("World", 200, 17, 320,  81, etype="text"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].label, "World")

    def test_three_fragments_merge_in_order(self):
        merged = _merge_adjacent_titles([
            el("Recruit",  100, 50, 250, 100),
            el("Crew",     265, 52, 360, 102),
            el("Members",  375, 52, 520, 102),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].label, "Recruit Crew Members")

    def test_height_mismatch_breaks_merge(self):
        """Big header + much smaller subtitle on similar y should NOT merge."""
        merged = _merge_adjacent_titles([
            el("Header",   100, 50, 250, 110),  # height 60
            el("subtitle", 265, 55, 360, 75),   # height 20 (ratio 0.33)
        ])
        self.assertEqual(len(merged), 2)

    def test_cloud_covered_world_map_merges(self):
        """Real frame 0013_2157343518.png: world map under cloud cover.
        Button-typed 'World' is fuzzier (h=93), text-typed 'Map' hugs
        glyphs (h=61).  Same baseline, different heights — must merge."""
        merged = _merge_adjacent_titles([
            el("World", 139,  3, 438,  96, etype="button"),
            el("Map",   337, 19, 441,  80, etype="text"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].label, "World Map")

    def test_single_element_passthrough(self):
        ee = el("Solo", 100, 50, 200, 100)
        merged = _merge_adjacent_titles([ee])
        self.assertEqual(merged, [ee])

    def test_empty_input(self):
        self.assertEqual(_merge_adjacent_titles([]), [])


if __name__ == "__main__":
    unittest.main()
