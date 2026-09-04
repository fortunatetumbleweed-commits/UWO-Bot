# tests/test_a_staged_cart_is_not_retapped.py
#
# Two rules about tapping a goods tile, both learned at Madeira on 2026-09-02.
#
# 1. A GREYED TILE IS NOT TAPPED (user). Grey means sold out — often because we just bought
#    the shelf dry — and a tap on it does nothing. Round 1 bought out the Raisin; round 2
#    went on tapping the grey tile it left behind.
#
# 2. A TAP THAT FOUND NO COMMIT BUTTON IS NOT RETRIED. The retry added that morning assumed
#    a tile tap is idempotent. It is not: tapping a STAGED tile UN-STAGES it. The commit
#    detector was rejecting a lit Purchase button (0.279787 against a 0.28 cut), so the
#    "retry" toggled a cart that the FIRST tap had filled correctly — three times — and left
#    325 Raisin dangling. Back then raised "Moving to another menu will empty the cart",
#    which nothing could answer, and the mission died there.
#
# The retry only ever made sense for a tap the GAME never saw, and this code cannot tell that
# from a commit button it merely failed to find. So it refuses once instead of tapping thrice.

import unittest
from unittest import mock

from PIL import Image

from actions import buy_materials as bm


def _frame():
    return Image.new("RGB", (2400, 1080), (90, 80, 70))


class _Tile:
    def __init__(self, label, cx, cy):
        self.label, self.cx, self.cy = label, cx, cy
        self.element_type = "button"
        self.x1, self.y1, self.x2, self.y2 = cx - 100, cy - 40, cx + 100, cy + 40


def _run(commit, greyed=False, targets=("Raisin",)):
    taps = []
    with mock.patch.object(bm, "_tile_is_greyed", return_value=greyed):
        res = bm.purchase_goods(
            "Madeira", goal={m: 1000 for m in targets},
            capture_fn=_frame,
            tap_fn=lambda x, y: taps.append((x, y)),
            omni_fn=lambda f: [_Tile(m, 500, 550) for m in targets],
            commit_fn=lambda f, e: commit,
            settle=0.0)
    return res, taps


class AGreyedTileIsNotTapped(unittest.TestCase):
    def test_it_is_not_tapped(self):
        res, taps = _run(commit=None, greyed=True)
        self.assertEqual(taps, [], "a sold-out tile must not be tapped")
        self.assertFalse(res["ok"])
        self.assertIn("Raisin", res.get("greyed", []))

    def test_a_live_tile_still_is(self):
        res, taps = _run(commit=None, greyed=False)
        self.assertEqual(len(taps), 1)


class AStagedCartIsNotRetapped(unittest.TestCase):
    def test_no_commit_button_means_ONE_tap_and_a_report(self):
        """The whole defect: three taps became one honest refusal."""
        res, taps = _run(commit=None)
        self.assertEqual(len(taps), 1, f"tapped {len(taps)}x — a second tap un-stages the cart")
        self.assertFalse(res["ok"])
        self.assertIn("un-stages", res["reason"])

    def test_it_says_the_cart_may_already_be_staged(self):
        """The old message asserted 'the cart never filled', which was a conclusion and was
        false — the cart HAD filled. The report must not claim what it did not observe."""
        res, _ = _run(commit=None)
        self.assertNotIn("never filled", res["reason"])

    def test_a_found_commit_button_proceeds_to_purchase(self):
        commit = mock.Mock(cx=1958, cy=997, cost="185,250", currency="ducat", verb="Purchase")
        with mock.patch.object(bm, "_react_after_purchase", return_value=True):
            res, taps = _run(commit=commit)
        self.assertTrue(res["ok"])
        self.assertIn((1958, 997), taps, "the commit button itself must be tapped")


class TheCommitButtonIsFoundWhenItIsLit(unittest.TestCase):
    """The threshold that caused all of it, pinned against both classes.

    Measured 2026-09-02: disabled Purchase 0.0207, goods tile 'Shea Butter' 0.2100,
    enabled Purchase 0.2798, dialog OK 0.3212. The cut has to fall between the tile and the
    palest enabled button — it had been sitting at 0.28, the top of the enabled range."""

    def test_the_cut_separates_a_tile_from_a_lit_button(self):
        from vision.region_detectors.commit_button import YELLOW_MIN_FRAC
        self.assertGreater(YELLOW_MIN_FRAC, 0.2100, "a goods tile would be taken for a commit")
        self.assertLess(YELLOW_MIN_FRAC, 0.2798, "a lit Purchase button would be rejected")
