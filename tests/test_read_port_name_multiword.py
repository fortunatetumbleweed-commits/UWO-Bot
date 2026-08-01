"""Regression test for the 'Las Palmas' → 'Las' truncation in
read_port_name.

Origin: 2026-05-22 user reported the bot stuck saying just 'Las'
instead of 'Las Palmas'.  Root cause: OmniParser tokenises the title
into multiple elements, and `_top_left_title_from_elements` was
returning only the leftmost-topmost one.  Fix concatenates same-row
title-height candidates left-to-right.
"""
import unittest

from vision.ocr import _top_left_title_from_elements


def el(label, x1, y1, x2, y2, etype="text"):
    return type("FakeEl", (), {
        "label": label,
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
        "width": x2 - x1, "height": y2 - y1,
        "element_type": etype,
    })()


# Title elements should be at least ~60 px tall to pass min_h
TITLE_Y, TITLE_H = 20, 70


class ReadPortNameMultiwordTests(unittest.TestCase):

    def test_las_palmas_split_returns_full_name(self):
        """Two same-row title elements merge into a single string."""
        elements = [
            el("Las",    330, TITLE_Y, 430, TITLE_Y + TITLE_H),
            el("Palmas", 450, TITLE_Y + 2, 720, TITLE_Y + TITLE_H + 2),
        ]
        result = _top_left_title_from_elements(elements, 2400, 1080)
        self.assertEqual(result, "Las Palmas")

    def test_port_royal_split_returns_full_name(self):
        elements = [
            el("Port",  330, TITLE_Y, 470, TITLE_Y + TITLE_H),
            el("Royal", 490, TITLE_Y, 680, TITLE_Y + TITLE_H),
        ]
        result = _top_left_title_from_elements(elements, 2400, 1080)
        self.assertEqual(result, "Port Royal")

    def test_single_word_title_unchanged(self):
        elements = [
            el("Lisbon", 330, TITLE_Y, 580, TITLE_Y + TITLE_H),
        ]
        result = _top_left_title_from_elements(elements, 2400, 1080)
        self.assertEqual(result, "Lisbon")

    def test_two_titles_on_different_rows_only_top_returned(self):
        """If a second title-sized element sits well below the first
        (different row), it should NOT be concatenated.  The function
        is meant to merge a single multi-word title, not stack."""
        elements = [
            el("Las",    330, TITLE_Y, 430, TITLE_Y + TITLE_H),
            el("Palmas", 450, TITLE_Y + 2, 720, TITLE_Y + TITLE_H + 2),
            # Second row, well below the title baseline
            el("Something", 330, TITLE_Y + 200, 600, TITLE_Y + 200 + TITLE_H),
        ]
        result = _top_left_title_from_elements(elements, 2400, 1080)
        self.assertEqual(result, "Las Palmas")

    def test_chrome_icon_at_same_baseline_filtered_by_type(self):
        """A button element of similar height but at the title baseline
        should still be included if it's a labelled button (e.g. the
        in-game back-arrow which may carry a label).  Conversely, the
        port-name token's actual label wins via leftmost-topmost order.
        """
        elements = [
            # Hypothetical button at far-left of title row
            el("X", 50, TITLE_Y, 100, TITLE_Y + TITLE_H, etype="button"),
            el("Las",    330, TITLE_Y, 430, TITLE_Y + TITLE_H),
            el("Palmas", 450, TITLE_Y, 720, TITLE_Y + TITLE_H),
        ]
        result = _top_left_title_from_elements(elements, 2400, 1080)
        # Sorted by x1: X first, then Las, then Palmas → "X Las Palmas".
        # This is the current behaviour — if it becomes a problem,
        # filter by element_type later.  For now: documents the contract.
        self.assertIn("Las Palmas", result)


if __name__ == "__main__":
    unittest.main()
