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

    THE DIALOG BELONGS TO THE DISPATCHER (user, 2026-09-07: "The dialog should be checked by
    the dispatcher, and dispatch to the activity, not being perceived and handled by the
    activity"). This primitive used to answer it itself, which meant a private
    perceive-decide-act inside a primitive and a SECOND dialog reader — configured differently
    from the dispatcher's, and worse: live 2026-09-07 at Faro it found no OK on a card the
    dispatcher classifies as `confirm_dialog` with an `Ok`, called it a dropped tap, and
    re-tapped Sell behind the modal four times.

    The 2026-09-01 incident this class was written for — the leg FAILING over a question
    nobody answered — is now prevented by the shape rather than by answering here: the caller
    hands back instead of failing, the dispatcher perceives the card and offers it to the
    activity, and the next tick taps Sell again.
    """

    def _run(self, *, on_sell, cart_ok):
        seq = list(on_sell)
        taps = []
        with mock.patch("actions.buy_materials._on_sell_tab", side_effect=lambda f: seq.pop(0)), \
             mock.patch("actions.buy_materials._sell_menu_item", return_value=(65, 274)), \
             mock.patch("actions.buy_materials._cart_confirm_ok", return_value=cart_ok):
            ok = ensure_sell_tab(_frame, lambda *a: taps.append(a), 0.0)
        return ok, taps

    def test_the_confirm_is_LEFT_FOR_THE_DISPATCHER(self):
        """It reports the tab did not open; it does not reach into the dialog itself."""
        ok, taps = self._run(on_sell=[False, False, False], cart_ok=(1200, 700))
        self.assertFalse(ok, "a tab that did not open is a refusal, not a success")
        self.assertNotIn((1200, 700), taps, "answered a dialog the dispatcher owns")

    def test_no_confirm_means_the_tap_was_DROPPED_so_it_tries_once_more(self):
        """This asserted a single tap — "no dialog found means no extra tap".

        The rule that was protecting is that a dialog nobody can NAME is never answered, and
        a re-tap of the same menu item does not touch a dialog at all. Meanwhile the game
        drops about one tap in twenty, and a dropped Sell tap has no other recovery.

        Live 2026-09-04, the same point minutes apart: Faro frame 66 tapped (65,274) and
        frames 67, 68, 69 were all still the Purchase page; Madeira frame 170 tapped (65,274)
        and frame 171 was the Sell page. Identical coordinates, identical sequence. Faro's
        cost the mission 1,476 units of surplus Pig and a barter round.
        """
        ok, taps = self._run(on_sell=[False, False, False], cart_ok=None)
        self.assertFalse(ok, "two dropped taps is a refusal, not a success")
        self.assertEqual(taps, [(65, 274), (65, 274)], "a dropped tap gets exactly one retry")

    def test_the_retry_is_the_MENU_ITEM_and_never_a_blind_dialog_OK(self):
        """The property the original test defended, stated directly: with no nameable dialog
        the only thing tapped is the Sell menu item itself."""
        _, taps = self._run(on_sell=[False, False, False], cart_ok=None)
        self.assertEqual(set(taps), {(65, 274)})

    def test_it_does_not_keep_tapping(self):
        """Bounded, not looped. Past one retry the screen is refusing rather than dropping,
        and more taps are the Lisboa failure at a different address."""
        _, taps = self._run(on_sell=[False, False, False], cart_ok=None)
        self.assertLessEqual(len(taps), 2)

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
