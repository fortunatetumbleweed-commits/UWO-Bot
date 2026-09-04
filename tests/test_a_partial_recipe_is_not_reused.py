"""A cached recipe missing a material the KB knows is a partial READ, not a recipe.

Materials are INVARIANT — a recipe does not lose an ingredient between runs — so if the
cache names fewer than the KB does, what was cached is a trade-list read that scrolled short.

Live 2026-08-26: the mission's cached recipe held `{Iron: 102, Candle: 102}` while the KB
held `{Iron: 102, Candle: 102, Matchlock Gun: 53}`, and every run that day logged "reusing
the recipe from the start of this task" and planned from it. The village check itself had
NOTICED — "keeping known material 'Matchlock Gun' that this read did not return" — and
protected the KB. Nothing protected the PLAN.

The fleet would have gathered Iron and Candle across two ports, sailed to Svear, and been
unable to barter at all for want of the third material.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain.barter_command import _recipe_is_partial


def _kb(*materials):
    inputs = [types.SimpleNamespace(material=m, ratio=100) for m in materials]
    return types.SimpleNamespace(inputs=inputs)


def _cached(**materials):
    return types.SimpleNamespace(materials=dict(materials), obtain=358)


class APartialRecipeIsDetected(unittest.TestCase):

    def _check(self, cached, kb):
        with patch("memory.barter_kb.load_recipe", return_value=kb):
            return _recipe_is_partial(cached, "Birch Tree")

    def test_the_live_case(self):
        """What was actually cached, against what the KB actually held."""
        self.assertTrue(self._check(_cached(Iron=102, Candle=102),
                                    _kb("Iron", "Candle", "Matchlock Gun")))

    def test_a_complete_recipe_is_reused(self):
        self.assertFalse(self._check(_cached(Iron=102, Candle=102, **{"Matchlock Gun": 53}),
                                     _kb("Iron", "Candle", "Matchlock Gun")))

    def test_quantities_may_differ_freely(self):
        """They re-roll every few hours. Only the MATERIAL NAMES are invariant."""
        self.assertFalse(self._check(_cached(Iron=1, Candle=9999),
                                     _kb("Iron", "Candle")))

    def test_case_and_spacing_do_not_matter(self):
        self.assertFalse(self._check(_cached(**{"  iron ": 1, "CANDLE": 2}),
                                     _kb("Iron", "Candle")))

    def test_an_unknown_good_is_not_called_partial(self):
        """With nothing to compare against, reusing the cache is the safe answer."""
        self.assertFalse(self._check(_cached(Iron=102), None))

    def test_a_kb_failure_does_not_discard_the_plan(self):
        with patch("memory.barter_kb.load_recipe", side_effect=RuntimeError("kb down")):
            self.assertFalse(_recipe_is_partial(_cached(Iron=102), "Birch Tree"))


class TheStaleCacheIsDiscarded(unittest.TestCase):

    def test_the_guard_is_wired_into_the_reuse_path(self):
        """It must run BEFORE the "reusing the recipe" branch, or the plan is already made."""
        import inspect
        from brain import barter_command
        src = inspect.getsource(barter_command.run_barter_command)
        i_guard = src.index("_recipe_is_partial")
        i_reuse = src.index("reusing the recipe from the start")
        self.assertLess(i_guard, i_reuse,
                        "the partial check must precede the reuse it is meant to prevent")

    def test_it_clears_the_stored_progress(self):
        """Discarding the recipe without clearing the progress would reuse it next run."""
        import inspect
        from brain import barter_command
        src = inspect.getsource(barter_command.run_barter_command)
        window = src[src.index("_recipe_is_partial"):]
        self.assertIn("mission_progress.finish()", window[:600])


if __name__ == "__main__":
    unittest.main()
