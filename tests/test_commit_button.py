"""Yellow commit-button detector — colour signature + verb/cost token split."""
import unittest

import numpy as np

from vision.region_detectors.commit_button import (
    yellow_fraction, _split_verb_cost, CommitButton, looks_like_commit_button,
)


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
        self.assertEqual(CommitButton("Recruit", "205,848", 1, 2, 0, 0, 0, 0, 0.4).label, "Recruit")
        self.assertEqual(CommitButton("", "999", 1, 2, 0, 0, 0, 0, 0.4).label, "commit (999)")


if __name__ == "__main__":
    unittest.main()
