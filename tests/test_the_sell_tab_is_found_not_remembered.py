"""The Sell tab is FOUND in the left menu — never tapped at a remembered point.

`MARKET_COORDS["sell"]` was (65, 225). Measured on the live Tripoli market, the item is at
(65, 274) — 49px away, in the dead space between 'Purchase' and 'Sell'. Live 2026-09-01 that
tap switched nothing, the clear read the Purchase grid, found nothing sellable, and reported
the hold clear; the mission then sailed to Tripoli carrying 4,448 Bambara Groundnut it meant
to have sold, and wedged with the cargo bar red.

A calibrated point is measured once against a layout the game re-bakes — the standing rule is
that a number meaning "where on the screen" should be an element lookup instead.

But a WHOLE-PAGE label search is not the fix either, because "Sell" is not unique here:

    'Sell'           @ (65,274)     the left menu    <- the only one that switches tabs
    'Sell Supplies'  @ (1322,1009)  dumps the fleet's water and food
    'Sell Overload'  @ (1560,1009)
    (and the page TITLE, which is the Back control)

Identify by ASSOCIATION — the item in the left menu column — so the dangerous two cannot be
chosen at all: they sit at y≈1009, far outside that column.

And a menu we cannot find is a REFUSAL, not a guess.
"""
import types
import unittest
from unittest import mock

from actions.buy_materials import _sell_menu_item, ensure_sell_tab


def _menu(*labels_at):
    items = [{"label": lab, "cx": cx, "cy": cy, "bbox": (cx - 40, cy - 18, cx + 40, cy + 18),
              "is_locked": False, "is_selected": False} for lab, cx, cy in labels_at]
    return types.SimpleNamespace(items=items)


# The real market left menu, read off the live Tripoli frame.
TRIPOLI = _menu(("Purchase", 207, 161), ("Sell", 65, 274), ("Trade Points", 178, 600),
                ("Trade Info", 178, 819), ("Language Effect", 178, 927))


def _frame():
    from PIL import Image
    return Image.new("RGB", (2400, 1080))


class TheItemComesFromTheLeftMenu(unittest.TestCase):
    def test_it_returns_the_menu_items_position(self):
        with mock.patch("vision.region_detectors.left_menu.detect_left_menu", return_value=TRIPOLI), \
             mock.patch("vision.omniparser.parse_fast_cached", return_value=[]):
            self.assertEqual(_sell_menu_item(_frame()), (65, 274))

    def test_it_is_not_the_old_calibrated_point(self):
        with mock.patch("vision.region_detectors.left_menu.detect_left_menu", return_value=TRIPOLI), \
             mock.patch("vision.omniparser.parse_fast_cached", return_value=[]):
            self.assertNotEqual(_sell_menu_item(_frame()), (65, 225),
                                "that point is the dead space this bug lived in")

    def test_sell_supplies_can_never_be_chosen(self):
        """It is not in the menu column, so it is not a candidate — not merely ranked lower."""
        with mock.patch("vision.region_detectors.left_menu.detect_left_menu",
                        return_value=_menu(("Purchase", 207, 161))), \
             mock.patch("vision.omniparser.parse_fast_cached", return_value=[]):
            self.assertIsNone(_sell_menu_item(_frame()),
                              "no 'Sell' in the menu means none was found, never a guess")


class AMenuWeCannotFindIsARefusal(unittest.TestCase):
    def test_no_menu_means_no_tap(self):
        taps = []
        with mock.patch("actions.buy_materials._on_sell_tab", return_value=False), \
             mock.patch("actions.buy_materials._sell_menu_item", return_value=None):
            ok = ensure_sell_tab(_frame, lambda *a: taps.append(a), 0.0)
        self.assertFalse(ok)
        self.assertEqual(taps, [], "tapping a remembered pixel is what this replaces")

    def test_a_read_that_raises_is_a_refusal_not_a_crash(self):
        with mock.patch("vision.omniparser.parse_fast_cached", side_effect=RuntimeError("no yolo")):
            self.assertIsNone(_sell_menu_item(_frame()))


class ItOnlySwitchesWhenItIsNotAlreadyThere(unittest.TestCase):
    """Tapping 'Sell' on an open Sell page is not a no-op — the title is 'Sell' too."""

    def test_an_open_sell_page_is_not_tapped_again(self):
        taps = []
        with mock.patch("actions.buy_materials._on_sell_tab", return_value=True):
            ok = ensure_sell_tab(_frame, lambda *a: taps.append(a), 0.0)
        self.assertTrue(ok)
        self.assertEqual(taps, [])

    def test_a_switch_that_does_not_take_reports_false(self):
        with mock.patch("actions.buy_materials._on_sell_tab", return_value=False), \
             mock.patch("actions.buy_materials._sell_menu_item", return_value=(65, 274)):
            self.assertFalse(ensure_sell_tab(_frame, lambda *a: None, 0.0),
                             "True here would let a Purchase grid be read as the hold")

    def test_it_confirms_with_the_screen_not_with_the_tap(self):
        seen = iter([False, True])          # not there, tapped, now there
        with mock.patch("actions.buy_materials._on_sell_tab", side_effect=lambda f: next(seen)), \
             mock.patch("actions.buy_materials._sell_menu_item", return_value=(65, 274)):
            self.assertTrue(ensure_sell_tab(_frame, lambda *a: None, 0.0))


if __name__ == "__main__":
    unittest.main()


class TheStagedCartRaisesAConfirmAndItIsAnswered(unittest.TestCase):
    """Switching tabs with goods staged makes the game ask before it will switch.

    Live 2026-09-01 at Tripoli: the trim tapped Sell, the game asked "Moving to another menu
    will empty the cart. Continue?", `_on_sell_tab` correctly said no, and `ensure_sell_tab`
    refused — so the leg failed over a question nobody answered. The dispatcher cleared the
    dialog seconds later and the switch completed, with the mission already dead.

    A dialog the bot's OWN action provoked is COMPLETED, not left for someone else.
    """

    def _run(self, *, on_sell, cart_ok):
        seq = list(on_sell)
        taps = []
        with mock.patch("actions.buy_materials._on_sell_tab", side_effect=lambda f: seq.pop(0)), \
             mock.patch("actions.buy_materials._sell_menu_item", return_value=(65, 274)), \
             mock.patch("actions.buy_materials._cart_confirm_ok", return_value=cart_ok):
            ok = ensure_sell_tab(_frame, lambda *a: taps.append(a), 0.0)
        return ok, taps

    def test_the_confirm_is_answered_and_the_switch_completes(self):
        ok, taps = self._run(on_sell=[False, False, True], cart_ok=(1200, 700))
        self.assertTrue(ok)
        self.assertIn((1200, 700), taps, "the confirm our own tap raised must be completed")

    def test_without_a_confirm_it_still_refuses(self):
        # Three checks either way: before the tap, after it, and the final verify.
        ok, taps = self._run(on_sell=[False, False, False], cart_ok=None)
        self.assertFalse(ok)
        self.assertEqual(taps, [(65, 274)], "no dialog found means no extra tap")

    def test_an_already_open_sell_page_asks_nothing(self):
        ok, taps = self._run(on_sell=[True], cart_ok=(1200, 700))
        self.assertTrue(ok)
        self.assertEqual(taps, [])


class ItNeverAnswersADialogItCannotName(unittest.TestCase):
    """A blind OK is how a bot confirms a purchase nobody chose."""

    def _ok_for(self, labels):
        els = [types.SimpleNamespace(label=l) for l in labels]
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=els), \
             mock.patch("actions.market_actions._dialog_ok_pos", return_value=(1200, 700)):
            from actions.buy_materials import _cart_confirm_ok
            return _cart_confirm_ok(_frame())

    def test_the_empty_cart_confirm_is_recognised(self):
        self.assertEqual(
            self._ok_for(["Moving to another menu will empty the cart. Continue?",
                          "Cancel", "OK"]), (1200, 700))

    def test_a_purchase_confirm_is_left_alone(self):
        self.assertIsNone(self._ok_for(["Purchase 709 Candle for 77,420?", "Cancel", "OK"]),
                          "answering this would spend money nobody approved")

    def test_a_dialog_with_only_one_of_the_two_words_is_left_alone(self):
        self.assertIsNone(self._ok_for(["Your cart is ready", "OK"]))
        self.assertIsNone(self._ok_for(["Empty the warehouse?", "OK"]))
