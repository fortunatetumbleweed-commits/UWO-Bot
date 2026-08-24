"""A task is joined at the step perception allows, not replayed from the top.

The rule (user, 2026-08-23): "the bot should be intelligent about running the plan... since
it is at the village, it can just tap the barter menu item and tap the box of nutmeg to see
it, instead of open the world map. And when it sees it can actually barter, then just skip to
the last step, barter and sail. This should be in the decision model of the task running,
instead of a rigid step by step."

Concretely: a recipe's MATERIALS are fixed knowledge — Box of Nutmeg at Melanesian Village
always takes Ebony, Coral and Textiles — and only the RATIO re-rolls. The barter panel shows
the ratio, so a fleet standing in the village can read it there. The remote check exists for
a village the fleet has NOT sailed to, and using it here forced the world map open, which a
village cannot do without leaving:

    [open_world_map] the fleet is AT 'village' — cannot open the world map from here
    FAILED at step check: could not open the world map
"""

from __future__ import annotations

import pytest
import unittest
from unittest.mock import patch

from brain import barter_command


def _cmd(tail_kind="none", tail_value=None):
    return barter_command.BarterCommand(good="Box of Nutmeg", village="Melanesian Village",
                                        tail_kind=tail_kind, tail_value=tail_value)


def _state(rounds):
    """What `_read_panel_state` returns — built from a LIST of material objects, the shape
    `read_barter_panel` actually produces."""
    import types
    from brain.barter_quantity import panel_barter_state
    have = {0: 1}.get(rounds, 999)
    mats = [types.SimpleNamespace(label=l, have=have if rounds else 1, need=n)
            for l, n in (("Wares", 168), ("Jewelry", 228), ("Fabrics", 252))]
    if rounds:                       # exactly `rounds` fundable, bounded by Fabrics
        mats = [types.SimpleNamespace(label=l, have=n * rounds, need=n)
                for l, n in (("Wares", 168), ("Jewelry", 228), ("Fabrics", 252))]
    return panel_barter_state(mats, 476)


@pytest.mark.live_entry_probe
class EnterAtTheBarterStep(unittest.TestCase):

    def _try(self, *, at_village, panel_ok=True, selected=True, rounds=3):
        resumed = {}
        with patch.object(barter_command, "_at_a_village",
                          return_value=(at_village, "test")), \
             patch("brain.barter_mission_live._open_barter_panel", return_value=panel_ok), \
             patch("brain.barter_mission_live._select_trade_good", return_value=selected), \
             patch("brain.barter_mission_live._read_panel_state",
                   return_value=_state(rounds)), \
             patch.object(barter_command, "_resume_at_village",
                          side_effect=lambda c, p: resumed.update(p) or {"ok": True}), \
             patch("brain.mission_progress.start"), \
             patch("brain.mission_progress.advance"):
            res = barter_command._try_barter_here(_cmd())
        return res, resumed

    def test_it_barters_here_when_the_panel_funds_a_round(self):
        res, resumed = self._try(at_village=True, rounds=3)
        self.assertIsNotNone(res, "no world map should be opened")
        self.assertEqual(resumed.get("rounds"), 3, "the PANEL sets the rounds")

    def test_it_falls_through_when_not_at_the_village(self):
        res, _r = self._try(at_village=False)
        self.assertIsNone(res, "away from the village the ordinary check-and-plan runs")

    def test_it_falls_through_when_the_panel_will_not_open(self):
        self.assertIsNone(self._try(at_village=True, panel_ok=False)[0])

    def test_it_falls_through_when_the_good_is_not_on_offer(self):
        self.assertIsNone(self._try(at_village=True, selected=False)[0])

    def test_it_falls_through_when_no_full_round_is_funded(self):
        """Not enough materials here — the trip-planning path is the right answer then."""
        self.assertIsNone(self._try(at_village=True, rounds=0)[0])

    def test_the_phase_is_recorded_so_a_later_run_resumes(self):
        with patch.object(barter_command, "_at_a_village", return_value=(True, "test")), \
             patch("brain.barter_mission_live._open_barter_panel", return_value=True), \
             patch("brain.barter_mission_live._select_trade_good", return_value=True), \
             patch("brain.barter_mission_live._read_panel_state", return_value=_state(2)), \
             patch.object(barter_command, "_resume_at_village", return_value={"ok": True}), \
             patch("brain.mission_progress.start") as start, \
             patch("brain.mission_progress.advance") as adv:
            barter_command._try_barter_here(_cmd())
        start.assert_called_once()
        self.assertIn("bartering", [c.args[0] for c in adv.call_args_list])


if __name__ == "__main__":
    unittest.main()
