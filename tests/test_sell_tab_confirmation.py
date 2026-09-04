"""The Sell tab is confirmed by the COMMIT BUTTON, not by OCR text order.

Purchase lists the SHOP'S stock; Sell lists what the FLEET holds. Reading one as the other
answers "do I already own enough?" with the wrong inventory, so the check must be decisive
in BOTH directions.

The previous heuristic looked for "sell" before "purchase" in the flattened OCR text. Both
tab titles are always painted in the left sub-menu, so it answered True on a Purchase grid
AND False on a genuine Sell grid (live 2026-08-22): the hold could not be read, the surplus
clear returned owned=None, and the re-plan came back at 0 rounds with the fleet carrying
four rounds' worth of materials.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from vision.omniparser import DetectedElement

FRAME = Image.new("RGB", (2400, 1080))


def _btn(label, cx, cy=1000, w=200, h=50):
    return DetectedElement(label=label, element_type="button",
                           x1=cx - w // 2, y1=cy - h // 2,
                           x2=cx + w // 2, y2=cy + h // 2, confidence=0.9)


def _on_sell_tab_with(elements):
    from actions import buy_materials
    with patch("vision.omniparser.parse_fast_cached", return_value=elements):
        return buy_materials._on_sell_tab(FRAME)


class SellTabConfirmation(unittest.TestCase):

    def test_the_sell_grid_is_confirmed(self):
        self.assertTrue(_on_sell_tab_with([_btn("Sell", 2239)]))

    def test_the_purchase_grid_is_rejected(self):
        self.assertFalse(_on_sell_tab_with([_btn("Purchase", 2177)]))

    def test_purchase_wins_when_both_labels_are_present(self):
        """A stale/overlapping detection must never be read as confirmation."""
        self.assertFalse(_on_sell_tab_with([_btn("Empty", 1916), _btn("Purchase", 2177),
                                            _btn("Sell", 1600)]))

    def test_no_buttons_is_not_a_confirmation(self):
        self.assertFalse(_on_sell_tab_with([]))

    def test_a_perceive_failure_is_not_a_confirmation(self):
        from actions import buy_materials
        with patch("vision.omniparser.parse_fast_cached", side_effect=RuntimeError("boom")):
            self.assertFalse(buy_materials._on_sell_tab(FRAME))


if __name__ == "__main__":
    unittest.main()


class AlreadyOnTheSellTab(unittest.TestCase):
    """Tapping "Sell" while the Sell page is open navigates AWAY from it.

    The page title is the word "Sell" too, beside the back arrow. Live 2026-08-22 the label
    search matched that title and tapped (107,53) on a Sell page that was already showing
    Ebony 700 / Coral 797 — the hold read was lost and the mission planned zero rounds.
    """

    def _read(self, on_sell_sequence):
        """Run _read_owned_via_sell with a scripted _on_sell_tab; return the taps it made."""
        from actions import buy_materials
        taps = []
        seq = list(on_sell_sequence)
        # WHERE the Sell item is, is a different question from WHETHER to tap it. These
        # tests are about the second: `_sell_menu_item` does a real left-menu read, and is
        # covered by `test_the_sell_tab_is_found_not_remembered`.
        with patch.object(buy_materials, "_on_sell_tab", side_effect=lambda _f: seq.pop(0)), \
             patch.object(buy_materials, "_sell_menu_item", return_value=(65, 274)), \
             patch("vision.market_reader.read_market_page_omni", return_value=[]):
            buy_materials._read_owned_via_sell(lambda: FRAME,
                                               lambda x, y: taps.append((x, y)), 0)
        return taps

    def test_no_tap_when_the_sell_page_is_already_open(self):
        self.assertEqual(self._read([True]), [])

    def test_it_still_switches_when_on_another_tab(self):
        self.assertEqual(len(self._read([False, True])), 1)
