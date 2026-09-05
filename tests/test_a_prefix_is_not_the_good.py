"""Almond is not Almond Oil.

Live 2026-09-05 at Lisboa. Both tiles were on screen and BOTH were read correctly:

    button 'Almond'      @ (1450, 557)
    button 'Almond Oil'  @ ( 570, 798)

`_find_material_tile` matched with `m in lab or lab in m`, so 'almond' matched 'almond oil'
and it tapped (570,798) — twice, with a blue-gem refresh between. The mission bought ~1,020
Almond Oil for ~194,000 ducats, drained Lisboa's Almond Oil shelf, and carried zero Almond.

NOTHING DOWNSTREAM COULD CATCH IT. The ledger, the goal counter and every log line print the
name the caller ASKED for, never the name on the tile that was tapped:

    [Lisboa] load Almond — tap tile @ (570, 796)
    [ledger] bought 510 Almond — believed 510
    round 1/60: owned=510 (+510) → 510/2618

The goal then read 1020/1015 — met — so it would have gathered the other two materials and
failed at the barter panel with no Almond aboard.

The game is full of these pairs (user, 2026-09-05): Almond / Almond Oil, Duck / Duck Meat,
Olive / Olive Oil. So the rule is general: THE WORDS MUST MATCH ONE FOR ONE, while each word
stays OCR-tolerant.
"""
from __future__ import annotations

import types
import unittest

from actions.buy_materials import _find_material_tile
from utils.fuzzy import same_good_name


def _tile(label, cx, cy, kind="button"):
    return types.SimpleNamespace(label=label, cx=cx, cy=cy, element_type=kind,
                                 x1=cx - 60, y1=cy - 60, x2=cx + 60, y2=cy + 60)


LISBOA = [_tile("Ammo", 570, 270), _tile("Hand Cannon", 1010, 270), _tile("Azulejo", 1450, 270),
          _tile("Ham", 570, 557), _tile("Rock Salt", 1010, 557), _tile("Almond", 1450, 557),
          _tile("Almond Oil", 570, 798), _tile("Printed Material", 1010, 798),
          _tile("Wheat", 1450, 798)]


class TheLisboaGrid(unittest.TestCase):
    """The real grid, with both goods present."""

    def test_almond_is_the_almond_tile(self):
        self.assertEqual(_find_material_tile(LISBOA, "Almond"), (1450, 557))

    def test_it_is_not_the_almond_oil_tile(self):
        self.assertNotEqual(_find_material_tile(LISBOA, "Almond"), (570, 798),
                            "bought 1,020 of the wrong good at ~194,000 ducats")

    def test_almond_oil_still_finds_its_own_tile(self):
        """The longer name must not become unreachable — asking for it is legitimate."""
        self.assertEqual(_find_material_tile(LISBOA, "Almond Oil"), (570, 798))

    def test_order_on_screen_does_not_decide(self):
        """The old matcher kept the LAST match, so the answer depended on element order."""
        self.assertEqual(_find_material_tile(list(reversed(LISBOA)), "Almond"), (1450, 557))

    def test_a_good_that_is_absent_is_absent(self):
        self.assertIsNone(_find_material_tile(LISBOA, "Myrrh"))


class TheOtherPairsTheGameHas(unittest.TestCase):
    """user, 2026-09-05: "there are many similar cases, like duck, duck meat, olive, olive
    oil, so we need to tell the difference"."""

    def test_prefix_pairs_are_different_goods(self):
        for short, long in (("Almond", "Almond Oil"), ("Duck", "Duck Meat"),
                            ("Olive", "Olive Oil"), ("Matchlock", "Matchlock Gun")):
            with self.subTest(f"{short} / {long}"):
                self.assertFalse(same_good_name(short, long))
                self.assertFalse(same_good_name(long, short))

    def test_each_still_matches_itself(self):
        for name in ("Almond", "Almond Oil", "Duck Meat", "Olive Oil", "Matchlock Gun"):
            with self.subTest(name):
                self.assertTrue(same_good_name(name, name))

    def test_picking_between_two_tiles_of_a_pair(self):
        grid = [_tile("Duck", 570, 270), _tile("Duck Meat", 1010, 270)]
        self.assertEqual(_find_material_tile(grid, "Duck"), (570, 270))
        self.assertEqual(_find_material_tile(grid, "Duck Meat"), (1010, 270))


class ItIsStillTolerantOfOCR(unittest.TestCase):
    """Strict about words, loose about spelling — the point is to reject a different GOOD,
    not a badly-read one."""

    def test_a_mangled_word_still_matches(self):
        self.assertTrue(same_good_name("Almond", "Almend"))
        self.assertTrue(same_good_name("Bambara Groundnut", "Bambara Groundnu"))

    def test_a_mangled_tile_is_still_found(self):
        grid = [_tile("Almend", 1450, 557), _tile("Almond Oil", 570, 798)]
        self.assertEqual(_find_material_tile(grid, "Almond"), (1450, 557))

    def test_different_short_goods_do_not_collide(self):
        for a, b in (("Coral", "Corn"), ("Pig", "Fig"), ("Iron", "Ivory")):
            with self.subTest(f"{a}/{b}"):
                self.assertFalse(same_good_name(a, b))

    def test_an_empty_name_matches_nothing(self):
        self.assertFalse(same_good_name("", "Almond"))
        self.assertFalse(same_good_name("Almond", ""))


if __name__ == "__main__":
    unittest.main()
