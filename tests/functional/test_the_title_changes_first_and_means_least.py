"""Mid-tab-switch the TITLE says Sell while the grid is still Purchase.

LIVE 2026-08-30 at Faro, frame 0003: chromed title 'Sell', right panel 'Purchase', goods grid
still the shop's. `_on_sell_tab` checked the title FIRST and returned True on it, so the
commit button — the anchor its own docstring names — was never consulted.

The caller then read the PURCHASE grid as the fleet's hold. `fill_missing_quantities` filled
owned_qty from the shop's stock badges (Ammo 124, Turron 3), and the sell filter loaded Ammo,
Chicken Meat and Turron — goods never bought — into a basket. There was no Sell button to
press, so the leg reported `sold: nothing` and walked away leaving the basket loaded; the
stranded basket then made Back raise "moving to another menu will empty the cart", which the
obstruction classifier cannot see, and the run wedged.

Purchase lists the SHOP'S stock; Sell lists what the FLEET holds. The title flips the instant
the tab is tapped and the content follows a beat later, so in the only window where the
question matters, the title is the one thing that cannot answer it.
"""
import os
import unittest

from actions.buy_materials import _on_sell_tab, _submenu_says


class _Frame:
    width, height = 2400, 1080


def _btn(label, cx=1200, cy=1000, w=200, h=50):
    from vision.omniparser import DetectedElement
    return DetectedElement(label=label, element_type="button",
                           x1=cx - w // 2, y1=cy - h // 2, x2=cx + w // 2, y2=cy + h // 2)


class TheCommitButtonDecides(unittest.TestCase):
    def _verdict(self, title, buttons):
        from unittest.mock import patch
        with patch("actions.buy_materials._submenu_says", return_value=title), \
             patch("vision.omniparser.parse_fast_cached", return_value=buttons), \
             patch("vision.region_detectors.action_buttons.detect_action_buttons") as det:
            det.return_value = type("R", (), {"labels": lambda self: [b.label for b in buttons]})()
            return _on_sell_tab(_Frame())

    def test_the_transition_is_rejected(self):
        self.assertFalse(self._verdict("sell", [_btn("Purchase")]),
                         "title flipped, grid has not — the button is the one that knows")

    def test_a_genuine_sell_grid_is_accepted(self):
        self.assertTrue(self._verdict("sell", [_btn("Sell")]))

    def test_a_purchase_title_rejects_without_looking_further(self):
        self.assertFalse(self._verdict("purchase", [_btn("Sell")]))

    def test_an_unreadable_button_falls_back_to_the_title(self):
        self.assertTrue(self._verdict("sell", []), "weak yes, but better than nothing")
        self.assertFalse(self._verdict(None, []), "no evidence at all is not a yes")


class AgainstTheFrameThatCausedIt(unittest.TestCase):
    STAGE = "tests/stage_suite/frames/faro_title_says_sell_grid_says_purchase.png"

    def test_frame_0003_is_not_the_sell_grid(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        im = Image.open(self.STAGE)
        self.assertEqual(_submenu_says(im), "sell", "the title really does say Sell")
        self.assertFalse(_on_sell_tab(im), "and it is STILL not the sell grid")

    def test_the_selling_path_reads_nothing_from_it(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        from actions.sell_goods import _sell_page
        # None, not [] — and the distinction is the point (2026-09-01). Both mean "read
        # nothing", but `[]` also means "the hold is empty", and a caller cannot tell those
        # apart. At Luanda that ambiguity turned "I never reached the Sell grid" into "the
        # clear is finished" and sailed a full hold to Tripoli. See
        # `tests/test_a_clear_that_never_saw_the_hold_is_not_finished`.
        self.assertIsNone(_sell_page(Image.open(self.STAGE)),
                          "the shop's stock must never be read as our hold, and 'I was not "
                          "looking at the hold' must not read as 'the hold is empty'")


if __name__ == "__main__":
    unittest.main()
