"""`trim_before_gather` asks for a clear AND a trim, and only the clear ever ran.

The node is built with both instructions (`brain/mission.py`):

    params={"good": opp.good, "keep_qty": keep_qty, "clear": True}

and its comment says what they are for: "clear the non-materials and trim the materials to
plan BEFORE buying anything". `_legs_for` branched on `clear` and returned, so `keep_qty`
was read and discarded.

They are not interchangeable, and the difference is structural rather than a matter of
degree: `FreeHold(keep=self.keep)` protects everything in `keep`, which IS the barter's
material list — so a clear cannot reduce a MATERIAL surplus, by construction. Only the trim
has per-material target quantities.

Live 2026-09-07: `trim_before_gather` ran at Faro, sold nothing, and reported done with
2,876 Pig aboard against a plan of 1,505. The fleet carried the surplus to Madeira, to Faro
again, and to San Village, where 1,812 Pig sat unusable once the Raisin ran out.
"""

from __future__ import annotations

import types
import unittest


class BothGoalsAreIssued(unittest.TestCase):

    def _steps_for(self, params):
        from brain.activities.market import FreeHold, TrimHold
        from brain.mission_runner import MissionRunner
        r = MissionRunner.__new__(MissionRunner)
        r.keep = ("Water", "Food", "Pig", "Raisin")
        leg = types.SimpleNamespace(kind="sell_surplus", id="trim_before_gather",
                                    location="", params=params)
        steps = r._runner_for(leg) or []
        return [type(getattr(s, "goal", None)).__name__ for s in steps]

    def test_the_pre_gather_node_clears_AND_trims(self):
        self.assertEqual(
            self._steps_for({"clear": True, "keep_qty": {"Pig": 1505, "Raisin": 1712}}),
            ["FreeHold", "TrimHold"])

    def test_the_clear_runs_first(self):
        """It disposes of whole goods, which shortens the grid the trim has to read."""
        got = self._steps_for({"clear": True, "keep_qty": {"Pig": 1505}})
        self.assertLess(got.index("FreeHold"), got.index("TrimHold"))

    def test_the_post_gather_node_still_only_trims(self):
        """It carries no `clear` — buying overshoots, and that is all it is there for."""
        self.assertEqual(self._steps_for({"keep_qty": {"Pig": 1505}}), ["TrimHold"])

    def test_a_clear_with_nothing_to_trim_is_still_a_clear(self):
        self.assertEqual(self._steps_for({"clear": True, "keep_qty": {}}), ["FreeHold"])

    def test_a_node_asking_for_neither_gives_no_steps(self):
        from brain.mission_runner import MissionRunner
        r = MissionRunner.__new__(MissionRunner)
        r.keep = ()
        leg = types.SimpleNamespace(kind="sell_surplus", id="x", location="", params={})
        self.assertIsNone(r._runner_for(leg))


class WhyAClearCannotDoTheTrimsJob(unittest.TestCase):

    def test_a_clear_protects_the_very_materials_that_overflow(self):
        """`keep` is the barter's material list, so Pig is safe from the clear that was
        supposed to dispose of it."""
        from brain.activities.market import FreeHold
        from brain.mission_runner import MissionRunner
        r = MissionRunner.__new__(MissionRunner)
        r.keep = ("Water", "Food", "Pig", "Raisin")
        leg = types.SimpleNamespace(kind="sell_surplus", id="trim_before_gather",
                                    location="", params={"clear": True, "keep_qty": {}})
        goal = r._runner_for(leg)[0].goal
        self.assertIsInstance(goal, FreeHold)
        self.assertIn("Pig", goal.keep)


if __name__ == "__main__":
    unittest.main()
