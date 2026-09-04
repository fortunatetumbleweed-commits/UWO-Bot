"""Gathering needs SPACE, so the trim runs before it as well as after.

The end trim alone is too late (user, 2026-08-26). A hold that arrives cluttered has two
problems, not one:

  * no room for what is being bought — live 2026-08-26 the hold reached 3,989/4,952 during
    the first gather leg, with a second leg and the barter output still to come;
  * a cargo panel so long that the bought good's tile falls below the fold, which is what
    blinded `buy_to_goal` into buying 2,000 Iron against a goal of 470 while reporting 0/470.

So: clear the non-materials and trim the materials to plan BEFORE buying anything, and trim
again after, because whole-shelf buying always overshoots.
"""

from __future__ import annotations

import types
import unittest


def _graph():
    from brain.mission import build_barter_graph
    opp = types.SimpleNamespace(good="Birch Tree", village="Svear Village",
                                sell_port="Lisboa", rounds=4)
    plan = types.SimpleNamespace(purchases={"Barcelona": {"Iron": 470},
                                            "Tripoli": {"Candle": 470}},
                                 needs={"Iron": 408, "Candle": 408, "Matchlock Gun": 212})
    return build_barter_graph(opp, plan)


class TheTrimBracketsTheGathering(unittest.TestCase):

    def setUp(self):
        self.graph = _graph()
        self.by_id = {t.id: t for t in self.graph}

    def test_there_is_a_trim_before_the_gathers(self):
        self.assertIn("trim_before_gather", self.by_id)

    def test_every_gather_waits_for_it(self):
        for t in self.graph:
            if t.kind == "gather":
                with self.subTest(gather=t.id):
                    self.assertIn("trim_before_gather", t.deps)

    def test_the_pre_gather_trim_waits_for_nothing(self):
        """It runs first, where the fleet already is."""
        self.assertEqual(self.by_id["trim_before_gather"].deps, ())

    def test_the_end_trim_still_runs_after_the_gathers(self):
        end = self.by_id["sell_surplus"]
        for t in self.graph:
            if t.kind == "gather":
                self.assertIn(t.id, end.deps)

    def test_the_pre_gather_trim_also_CLEARS(self):
        """Not just a trim to quantities: anything that is not a material or supply goes
        (user: "if it is not a material we should just sell them")."""
        self.assertTrue(self.by_id["trim_before_gather"].params.get("clear"))

    def test_the_end_trim_does_not_clear(self):
        """By then the hold should hold only materials, and clearing again is a wasted visit."""
        self.assertFalse(self.by_id["sell_surplus"].params.get("clear"))

    def test_both_trims_target_the_plan_s_needs(self):
        for node in ("trim_before_gather", "sell_surplus"):
            with self.subTest(node=node):
                self.assertEqual(self.by_id[node].params["keep_qty"],
                                 {"Iron": 408, "Candle": 408, "Matchlock Gun": 212})

    def test_it_is_first_in_the_graph(self):
        self.assertEqual(self.graph[0].id, "trim_before_gather")


class TheExecutorClearsThenTrims(unittest.TestCase):

    def _run(self, clear):
        from unittest.mock import patch
        from brain.activities.market import FreeHold, TrimHold
        from brain.barter_mission_live import make_live_executors
        from brain.dispatcher import ActivityResult, FINISHED

        goals = []
        task = types.SimpleNamespace(params={"good": "Birch Tree", "clear": clear,
                                             "keep_qty": {"Iron": 408}})
        with patch("brain.run_goal.run_goal",
                   side_effect=lambda g, **k: goals.append(g) or
                   ActivityResult(FINISHED, {"trimmed": {}}, detail="x")), \
             patch("brain.barter_mission_live._current_port", return_value="Barcelona"), \
             patch("brain.barter_mission_live._exit_market_to_overworld"):
            make_live_executors()["sell_surplus"](task)
        return goals

    def test_the_pre_gather_run_frees_then_trims(self):
        from brain.activities.market import FreeHold, TrimHold
        goals = self._run(clear=True)
        self.assertIsInstance(goals[0], FreeHold, "clear the non-materials first")
        self.assertIsInstance(goals[1], TrimHold, "then trim the materials to plan")

    def test_the_end_run_only_trims(self):
        from brain.activities.market import TrimHold
        goals = self._run(clear=False)
        self.assertEqual(len(goals), 1)
        self.assertIsInstance(goals[0], TrimHold)


if __name__ == "__main__":
    unittest.main()
