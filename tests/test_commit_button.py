"""Yellow commit-button detector — colour signature + verb/cost token split."""
import unittest

import numpy as np

from vision.region_detectors.commit_button import (
    yellow_fraction, _split_verb_cost, CommitButton, looks_like_commit_button,
    cost_currency,
)


def _button(icon=None):
    """A 300×60 yellow commit button; optionally a coloured gem diamond on its left."""
    a = np.zeros((60, 300, 3), np.uint8)
    a[:] = (210, 170, 40)                        # yellow/gold fill
    if icon == "red":
        a[20:40, 15:45] = (200, 40, 40)          # red diamond
    if icon == "blue":
        a[20:40, 15:45] = (50, 90, 210)          # blue diamond
    return a


class LooksLikeCommitTests(unittest.TestCase):
    # measured: Recruit 554×75/0.37, dialog OK 215×77/0.32 → buttons;
    #           Whisky goods-tile highlight 432×240/0.21 → NOT a button.
    def test_wide_yellow_pills_accepted(self):
        self.assertTrue(looks_like_commit_button(554, 75, 0.37))   # Recruit
        self.assertTrue(looks_like_commit_button(215, 77, 0.32))   # dialog OK

    def test_squarish_weak_yellow_tile_rejected(self):
        self.assertFalse(looks_like_commit_button(432, 240, 0.21))  # goods tile
        self.assertFalse(looks_like_commit_button(400, 240, 0.50))  # square even if yellow
        self.assertFalse(looks_like_commit_button(600, 80, 0.10))   # wide but not yellow


class YellowFractionTests(unittest.TestCase):
    def test_yellow_region_scores_high(self):
        arr = np.zeros((40, 200, 3), dtype=np.uint8)
        arr[:, :] = (255, 200, 100)          # gold fill
        self.assertGreater(yellow_fraction(arr, 0, 0, 200, 40), 0.9)

    def test_grey_region_scores_zero(self):
        arr = np.full((40, 200, 3), 210, dtype=np.uint8)   # a grey toggle/menu row
        self.assertLess(yellow_fraction(arr, 0, 0, 200, 40), 0.05)

    def test_empty_bbox_is_zero(self):
        arr = np.zeros((10, 10, 3), dtype=np.uint8)
        self.assertEqual(yellow_fraction(arr, 5, 5, 5, 5), 0.0)


class SplitVerbCostTests(unittest.TestCase):
    def test_cost_then_verb(self):
        self.assertEqual(_split_verb_cost("205,848 Recruit"), ("Recruit", "205,848"))

    def test_verb_then_cost(self):
        self.assertEqual(_split_verb_cost("Buy 1,200"), ("Buy", "1,200"))

    def test_verb_only(self):
        self.assertEqual(_split_verb_cost("Confirm"), ("Confirm", ""))

    def test_cost_only(self):
        self.assertEqual(_split_verb_cost("82,707,048"), ("", "82,707,048"))

    def test_commit_button_label_prefers_verb(self):
        self.assertEqual(CommitButton("Recruit", "205,848", "ducat",
                                      1, 2, 0, 0, 0, 0, 0.4).label, "Recruit")
        self.assertEqual(CommitButton("", "999", "ducat",
                                      1, 2, 0, 0, 0, 0, 0.4).label, "commit (999)")


class CostCurrencyTests(unittest.TestCase):
    """Which currency the button spends, from the cost-icon colour (red-gem gate)."""
    def test_gold_coin_reads_ducat(self):
        self.assertEqual(cost_currency(_button(None), 0, 0, 300, 60), "ducat")

    def test_red_diamond_reads_red_gem(self):
        self.assertEqual(cost_currency(_button("red"), 0, 0, 300, 60), "red_gem")

    def test_blue_diamond_reads_blue_gem(self):
        self.assertEqual(cost_currency(_button("blue"), 0, 0, 300, 60), "blue_gem")

    def test_gem_on_the_right_is_ignored(self):
        # a coloured pixel in the VERB half (right) must not flip the currency
        a = _button(None)
        a[20:40, 255:285] = (200, 40, 40)        # red blob on the right — not the icon
        self.assertEqual(cost_currency(a, 0, 0, 300, 60), "ducat")


if __name__ == "__main__":
    unittest.main()
