"""A PARTIAL cargo read is more dangerous than no read at all.

`_cargo_tiles` returns whatever tiles it could resolve, and `track_bought_good` compares
before/after multisets over that subset. So a hold the bot cannot fully see looks like a hold
that did not change — the buy loop reads "no progress" and keeps spending.

Live at Bordeaux 2026-08-24: six tiles on screen (114, 980, 2, 170, 281, 281) and exactly ONE
resolved (170), because the middle tile came back from OmniParser labelled 'icon' with no
number and broke the evenly-pitched run. The 980 was the Raisin being bought — invisible to
the tracker, so four Purchase taps at 149,695 ducats each registered as 0/777.

The hold counter is the cross-check: when every tile resolves, the numbers SUM to `used`.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from actions.buy_materials import cargo_read_problem


class TheHoldCounterCrossChecksTheStrip(unittest.TestCase):

    def _problem(self, tiles, used, cap=4108):
        with patch("actions.buy_materials._read_cargo_used_cap", return_value=(used, cap)):
            return cargo_read_problem(object(), tiles, [])

    def test_a_complete_read_sums_to_the_hold(self):
        """The real Bordeaux hold, fully resolved: 114+980+2+170+281+281 = 1828."""
        tiles = [((1754, 327), 114), ((1891, 327), 980), ((2029, 327), 2),
                 ((2166, 327), 170), ((1755, 464), 281), ((1890, 465), 281)]
        self.assertIsNone(self._problem(tiles, used=1828))

    def test_the_bordeaux_partial_read_is_caught(self):
        """What actually happened: one tile of six, 170 against a hold of 1828."""
        problem = self._problem([((2166, 327), 170)], used=1828)
        self.assertIsNotNone(problem)
        self.assertIn("170", problem)
        self.assertIn("1828", problem)

    def test_no_tiles_at_all_is_caught(self):
        self.assertIsNotNone(self._problem([], used=1828))

    def test_an_unreadable_counter_yields_NO_verdict(self):
        """"Cannot verify" must not masquerade as "found wrong".

        With no counter there is nothing to compare against, so this check has no verdict.
        Reporting a problem here halted a buy loop whose tile tracking was perfectly good
        (Bordeaux 2026-08-24: owned 375 -> 623, +248, exactly right) merely because the
        counter was unreadable on that frame. A missing PRIMARY reading is already handled
        by the tracker returning None.
        """
        self.assertIsNone(self._problem([((2166, 327), 170)], used=None))

    def test_an_empty_hold_reads_clean(self):
        self.assertIsNone(self._problem([], used=0))


if __name__ == "__main__":
    unittest.main()
