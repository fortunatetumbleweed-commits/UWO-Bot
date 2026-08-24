"""Tests for read_supply — Water/Food held counts off the HUD.

Synthetic elements mirror the real OmniParser tokens observed in the barter
walkthrough: a Discard dialog renders 'Food' next to a '50/226' slider pair
(226 = held), while a Cargo Hold renders 'Water'/'Food' next to a bare count.
"""
import types
import unittest

from vision.hud_readers import read_supply


def _el(label, cx, cy, etype="text"):
    return types.SimpleNamespace(label=label, element_type=etype, cx=cx, cy=cy)


class ReadSupplyTests(unittest.TestCase):
    def test_cargo_hold_bare_counts(self):
        els = [
            _el("Water", 600, 500), _el("329", 600, 560),
            _el("Food", 900, 500), _el("312", 900, 560),
        ]
        self.assertEqual(read_supply(els), (329, 312))

    def test_discard_dialog_slider_pair_uses_held_denominator(self):
        # Real tokens from frame_0020: 'Food' @ (1201,512), '50/226' @ (1201,578).
        els = [_el("Food", 1201, 512), _el("50/226", 1201, 578)]
        self.assertEqual(read_supply(els), (None, 226))

    def test_picks_nearest_number_not_a_distant_one(self):
        # A far-away number for the other resource must not bleed in.
        els = [
            _el("Water", 600, 500), _el("329", 600, 560),
            _el("9999", 2200, 60),                      # unrelated top-HUD number
        ]
        self.assertEqual(read_supply(els), (329, None))

    def test_none_when_no_anchor(self):
        self.assertIsNone(read_supply([_el("226", 620, 586), _el("Camas", 2000, 158)]))

    def test_partial_only_food(self):
        els = [_el("Food", 900, 500), _el("312", 900, 560)]
        self.assertEqual(read_supply(els), (None, 312))


if __name__ == "__main__":
    unittest.main()
