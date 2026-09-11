"""Material rows at the top of a screen belong to the good ABOVE them, not to the last read.

The Trade List is scrolled with an OVERLAP so that a recipe split across the fold is never
lost. That overlap re-shows rows whose owning good has moved off the TOP of the panel with
its name clipped away — and `merge_trade_screens` used to hand those orphans to the last good
it had seen, which by then is the good BELOW them. The mechanism that exists to stop
materials being lost was duplicating them onto the wrong recipe.

Live 2026-09-10 at Cheyenne Village. The list runs

    Goldenseal 1,073  ->  Chicle 182 · Corn 157 · Gold Dust 182  ->  American Bison 859

Screen 0 (`/tmp/village_learn/Cheyenne_Village_00.png`) reads all of it, so the last good
seen is American Bison. Screen 1 is the same rows scrolled slightly, with Goldenseal's name
clipped off the top — so it parses as three unowned materials, then American Bison. Those
three went to American Bison, which came out of the read holding FIVE materials instead of
three: Corn (mangled to 'Touu') and Gold Dust, both Goldenseal's, at Goldenseal's ratios.

A recipe cannot GAIN an ingredient any more than it can lose one. `_target_complete` guards
the losing half; nothing guarded this one, and the KB now holds a five-material Bison recipe
that the planner would buy against.
"""

from __future__ import annotations

import types
import unittest

from actions.village_check import merge_trade_screens, parse_trade_list


def _el(etype, label, x1, y1, w=120, h=100):
    return types.SimpleNamespace(element_type=etype, label=label,
                                 x1=x1, y1=y1, x2=x1 + w, y2=y1 + h,
                                 cx=x1 + w // 2, cy=y1 + h // 2)


def _row(name, qty, y, indented, pin):
    els = [_el("button", qty, 1840 if indented else 1810, y)]
    if name is not None:                       # None = the name is clipped off the screen
        els.append(_el("text", name, 1950, y + 10, w=110, h=32))
    if pin:
        els.append(_el("icon", "icon", 2280, y + 60, w=46, h=48))
    return els


GOOD, MATERIAL = (False, False), (True, True)


class AnOrphanBlockBelongsAbove(unittest.TestCase):

    def _cheyenne(self):
        """Screen 0 whole, screen 1 the same rows with Goldenseal's NAME clipped."""
        s0 = parse_trade_list(_row("Goldenseal", "1,073", 330, *GOOD)
                              + _row("Chicle", "182", 450, *MATERIAL)
                              + _row("Corn", "157", 560, *MATERIAL)
                              + _row("Gold Dust", "182", 670, *MATERIAL)
                              + _row("American Bison", "859", 780, *GOOD))
        s1 = parse_trade_list(_row(None, "1,073", 330, *GOOD)
                              + _row("Chicle", "182", 420, *MATERIAL)
                              + _row("Corn", "157", 530, *MATERIAL)
                              + _row("Gold Dust", "182", 640, *MATERIAL)
                              + _row("American Bison", "859", 750, *GOOD))
        return s0, s1

    def test_THE_BISON_DOES_NOT_EAT_THE_GOLDENSEAL(self):
        merged = {t.good: t.materials for t in merge_trade_screens(list(self._cheyenne()))}
        self.assertEqual(merged["American Bison"], {},
                         "Goldenseal's materials were attributed to the good below them")

    def test_the_orphans_go_back_to_their_own_good(self):
        merged = {t.good: t.materials for t in merge_trade_screens(list(self._cheyenne()))}
        self.assertEqual(merged["Goldenseal"],
                         {"Chicle": 182, "Corn": 157, "Gold Dust": 182})

    def test_the_goods_and_their_order_survive(self):
        merged = merge_trade_screens(list(self._cheyenne()))
        self.assertEqual([t.good for t in merged], ["Goldenseal", "American Bison"])

    def test_A_RECIPE_CUT_OFF_AT_THE_FOLD_STILL_CONTINUES(self):
        """The other case, and it must keep the opposite answer.

        Here the screen's first named good has NOT been seen before, so there is nothing
        above it in the accumulation and the last good read really is the owner."""
        s1 = parse_trade_list(_row("Pulque", "1,022", 400, *GOOD)
                              + _row("Coral", "150", 540, *MATERIAL))
        s2 = parse_trade_list(_row("Silver", "150", 400, *MATERIAL)
                              + _row("Wampum", "635", 520, *GOOD)
                              + _row("Platinum", "150", 660, *MATERIAL))
        merged = {t.good: t.materials for t in merge_trade_screens([s1, s2])}
        self.assertEqual(merged["Pulque"], {"Coral": 150, "Silver": 150})
        self.assertEqual(merged["Wampum"], {"Platinum": 150})

    def test_an_orphan_above_the_very_first_good_is_dropped(self):
        """Nothing is above it, so there is no owner — and inventing one is the bug."""
        s1 = parse_trade_list(_row("Camas", "953", 400, *GOOD))
        s2 = parse_trade_list(_row("Avocado", "130", 300, *MATERIAL)
                              + _row("Camas", "953", 420, *GOOD))
        merged = {t.good: t.materials for t in merge_trade_screens([s1, s2])}
        self.assertEqual(merged["Camas"], {})

    def test_a_name_that_fails_MID_screen_still_continues_this_screen(self):
        """Past the first named good an orphan is a bad read, not a clipped owner."""
        s = parse_trade_list(_row("Camas", "953", 400, *GOOD)
                             + _row(None, "999", 500, *GOOD)
                             + _row("Avocado", "130", 600, *MATERIAL))
        merged = {t.good: t.materials for t in merge_trade_screens([s])}
        self.assertEqual(merged["Camas"], {"Avocado": 130})


if __name__ == "__main__":
    unittest.main()
