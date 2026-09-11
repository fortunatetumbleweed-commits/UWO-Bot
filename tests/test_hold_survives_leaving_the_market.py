"""The hold is only readable at a market, but it stays true at sea.

Live 2026-08-22, run 27 — the fleet carried 700 Ebony / 797 Coral / 1049 Textiles, three
rounds' worth, and the mission still refused to plan:

    [barter_command] no feasible rounds with 715 free — clearing surplus...
    FAILED at step plan: no feasible rounds: free space 715 < 781 reserved for one round

`clear_surplus_at_current_port` had answered "not at a port — cannot sell surplus here", so
`owned` came back None and the planner priced every round as though the hold were empty.
The materials were aboard; the bot simply could not open a Sell tab from the water.

A remembered hold must be dropped the instant the bot changes it — buying, selling or
bartering — because a stale hold is worse than no hold at all.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from memory import observed_facts
from brain.barter_command import _last_known_hold

HOLD = {"ebony": 700, "coral": 797, "textiles": 1049}


class LastKnownHold(unittest.TestCase):

    def setUp(self):
        observed_facts._reset_for_tests()
        self._save = patch.object(observed_facts, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)

    def test_a_recent_sighting_is_served(self):
        observed_facts.remember("hold", HOLD)
        self.assertEqual(_last_known_hold(), HOLD)

    def test_nothing_seen_yet_is_None(self):
        self.assertIsNone(_last_known_hold())

    def test_a_stale_sighting_is_withheld(self):
        """Bounded by age — an hour-old hold is not evidence about now."""
        import time
        observed_facts.remember("hold", HOLD, now=time.time() - 7200)
        self.assertIsNone(_last_known_hold(max_age_s=3600))

    def test_forgetting_it_removes_it(self):
        """What a commit does: the hold is about to change."""
        observed_facts.remember("hold", HOLD)
        observed_facts.forget("hold")
        self.assertIsNone(_last_known_hold())

    def test_the_plan_can_use_it_when_the_market_is_unreachable(self):
        """Rounds fundable from a remembered hold, against far fewer without it."""
        from brain.barter_quantity import plan_barter_rounds
        needs = {"Ebony": 152, "Coral": 228, "Textiles": 228}
        observed_facts.remember("hold", HOLD)
        with_memory = plan_barter_rounds(679, needs, 7, 715,
                                         materials_on_hand=_last_known_hold(), cushion=0.15)
        without = plan_barter_rounds(679, needs, 7, 715, cushion=0.15)
        # 1, not 0: 715 free buys one unfunded round's 608 of material outright. The point
        # stands on the GAP — remembering the hold is worth 3 extra rounds here.
        self.assertEqual(without.rounds, 1)
        self.assertEqual(with_memory.rounds, 4)
        self.assertGreaterEqual(with_memory.rounds - without.rounds, 3)


if __name__ == "__main__":
    unittest.main()
