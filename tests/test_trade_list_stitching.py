"""Screens are assembled into one row sequence before any recipe is inferred.

Grouping each screen into recipes and merging afterwards cannot work at the fold: a good and
its materials routinely straddle two screens, so neither screen holds the whole recipe. Every
attempt to bridge that by inference failed the same way — the last one carried a good's NAME
onto the next screen's leading materials and, when one screen misparsed, wrote `American Bison
<- Horse, Hand Cannon, Bullet, Moccasin, American Bison, Wool`: six inputs, including itself.

The list is not ambiguous; only the windows onto it are. Stitch the windows on their shared
ROWS, then read the recipes once from the finished sequence.
"""

from __future__ import annotations

import unittest

from actions.village_check import TradeRow, rows_to_trades
from actions.trade_list_stitch import _overlap, stitch_screens


def G(name, qty):
    return TradeRow(name=name, qty=qty, is_material=False)


def M(name, qty):
    return TradeRow(name=name, qty=qty, is_material=True)


class FindingTheOverlap(unittest.TestCase):

    def test_the_shared_tail_is_found(self):
        a = [G("Goldenseal", 886), M("Chicle", 187), M("Corn", 187)]
        b = [M("Chicle", 187), M("Corn", 187), M("Gold Dust", 187)]
        self.assertEqual(_overlap(a, b), 2)

    def test_no_shared_row_is_zero(self):
        self.assertEqual(_overlap([G("A", 1)], [M("B", 2)]), 0)

    def test_position_is_not_part_of_identity(self):
        """The same row sits at a different y on every screen — that is the point."""
        a = [G("Moccasin", 769, )]
        b = [TradeRow(name="Moccasin", qty=769, is_material=False, y=904)]
        self.assertEqual(_overlap(a, b), 1)

    def test_a_good_and_a_material_of_the_same_name_are_different_rows(self):
        """American Bison is a GOOD here and a MATERIAL of Moccasin — not the same row."""
        self.assertEqual(_overlap([G("American Bison", 769)], [M("American Bison", 300)]), 0)


class AssemblingTheSequence(unittest.TestCase):

    def test_a_recipe_split_across_the_fold_is_reassembled(self):
        """Neither screen holds Moccasin's full recipe; the stitched sequence does."""
        s1 = [G("Moccasin", 769), M("American Bison", 300)]
        s2 = [G("Moccasin", 769), M("American Bison", 300), M("Wool", 340)]
        rows, _steps = stitch_screens([s1, s2], trace=False)
        got = {t.good: dict(t.materials) for t in rows_to_trades(rows)}
        self.assertEqual(got["Moccasin"], {"American Bison": 300, "Wool": 340})

    def test_a_repeated_screen_adds_nothing(self):
        s = [G("Eagle Feather", 512), M("Pulque", 75)]
        rows, steps = stitch_screens([s, s, s], trace=False)
        self.assertEqual(len(rows), 2)
        self.assertEqual(steps[-1]["appended"], 0)

    def test_a_gap_is_reported_and_not_bridged(self):
        """A jump past unseen rows must be visible, never silently stitched over."""
        rows, steps = stitch_screens([[G("A", 1)], [G("Z", 26)]], trace=False)
        self.assertEqual(steps[-1]["overlap"], 0)
        self.assertIn("NO OVERLAP", steps[-1]["note"])

    def test_the_whole_cheyenne_list_reassembles(self):
        """The measured screens, each holding part of the list."""
        screens = [
            [G("Goldenseal", 886), M("Chicle", 187), M("Corn", 187), M("Gold Dust", 187)],
            [M("Gold Dust", 187), G("American Bison", 769), M("Horse", 150),
             M("Hand Cannon", 150)],
            [M("Horse", 150), M("Hand Cannon", 150), M("Bullet", 150), G("Moccasin", 769)],
            [M("Bullet", 150), G("Moccasin", 769), M("American Bison", 300), M("Wool", 340)],
            [M("American Bison", 300), M("Wool", 340), G("Eagle Feather", 512),
             M("Pulque", 75), M("Guarana", 75)],
        ]
        rows, steps = stitch_screens(screens, trace=False)
        self.assertTrue(all(s.get("overlap", 1) or s["screen"] == 1 for s in steps),
                        "every screen after the first must share rows with the sequence")
        got = {t.good: dict(t.materials) for t in rows_to_trades(rows)}
        self.assertEqual(got["Goldenseal"], {"Chicle": 187, "Corn": 187, "Gold Dust": 187})
        self.assertEqual(got["American Bison"],
                         {"Horse": 150, "Hand Cannon": 150, "Bullet": 150})
        self.assertEqual(got["Moccasin"], {"American Bison": 300, "Wool": 340})
        self.assertEqual(got["Eagle Feather"], {"Pulque": 75, "Guarana": 75})


if __name__ == "__main__":
    unittest.main()
