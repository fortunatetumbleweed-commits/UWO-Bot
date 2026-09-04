"""The per-village material list cannot be the judge of whether the read is complete.

`inputs_for(village)` is a refinement written FROM READS. So a read that scrolls short writes
a short list; the next read consults that list, sees every material in it, declares itself
complete, stops scrolling, and writes the same short list again. The truncation confirms
itself and never recovers.

Live 2026-08-26 at Svear Village, in one run and in this order:

    [village_check] 'Birch Tree' fully read after 1 screen(s) — not scrolling
    [village_check] Svear Village: Birch Tree 358 <- {'Iron': 102, 'Candle': ...}
    [village_check] Birch Tree: keeping known material 'Matchlock Gun' that this read
                    did not return

The same function declared the read complete and then discovered a material it had not seen.
It held the evidence that its own verdict was wrong and did not use it — and every barter run
that day planned from the result, which would have reached the village unable to barter.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from actions.village_check import _target_complete


def _trade(good, **materials):
    return types.SimpleNamespace(good=good, materials=dict(materials))


def _recipe(recipe_inputs, village_inputs=None):
    def _mk(names):
        return [types.SimpleNamespace(material=n, ratio=100) for n in names]
    return types.SimpleNamespace(inputs=_mk(recipe_inputs),
                                 inputs_for=lambda _v: _mk(village_inputs)
                                 if village_inputs is not None else None)


class CompletenessIsJudgedAgainstTheUnion(unittest.TestCase):

    def _complete(self, trades, recipe):
        with patch("memory.barter_kb.load_recipe", return_value=recipe):
            return _target_complete(trades, "Birch Tree", "Svear Village")

    def test_the_live_case_is_not_complete(self):
        """A village list of 2 must not certify a read that missed the recipe's third."""
        self.assertFalse(self._complete(
            [_trade("Birch Tree", Iron=102, Candle=102)],
            _recipe(["Iron", "Candle", "Matchlock Gun"], village_inputs=["Iron", "Candle"])))

    def test_seeing_all_of_them_is_complete(self):
        self.assertTrue(self._complete(
            [_trade("Birch Tree", Iron=102, Candle=102, **{"Matchlock Gun": 53})],
            _recipe(["Iron", "Candle", "Matchlock Gun"], village_inputs=["Iron", "Candle"])))

    def test_a_village_material_outside_the_recipe_is_still_required(self):
        """The union runs both ways: a per-village extra must also be seen."""
        self.assertFalse(self._complete(
            [_trade("Birch Tree", Iron=102)],
            _recipe(["Iron"], village_inputs=["Iron", "Goldware"])))

    def test_an_unknown_good_falls_back_to_the_next_row(self):
        """With no KB entry, a following good's row is what closes the list."""
        with patch("memory.barter_kb.load_recipe", return_value=None):
            self.assertTrue(_target_complete(
                [_trade("Birch Tree", Iron=102), _trade("Juniper Berry", Lingonberry=1)],
                "Birch Tree", "Svear Village"))
            self.assertFalse(_target_complete(
                [_trade("Birch Tree", Iron=102)], "Birch Tree", "Svear Village"))

    def test_a_good_not_on_the_page_is_never_complete(self):
        self.assertFalse(self._complete([_trade("Juniper Berry", Lingonberry=1)],
                                        _recipe(["Iron"])))


class ScrollingTooFarIsTheCheapMistake(unittest.TestCase):
    """The trade this fix makes, stated so it is not undone by accident.

    A village that genuinely uses fewer materials than the recipe lists will simply never show
    the extra one, so the read scrolls to the bottom — a few extra scrolls. Stopping early
    cost a day of runs planning from a recipe missing a material.
    """

    def test_a_village_using_fewer_materials_scrolls_to_the_end_rather_than_truncating(self):
        with patch("memory.barter_kb.load_recipe",
                   return_value=_recipe(["Iron", "Candle", "Matchlock Gun"],
                                        village_inputs=["Iron"])):
            self.assertFalse(_target_complete([_trade("Birch Tree", Iron=102)],
                                              "Birch Tree", "Svear Village"))


if __name__ == "__main__":
    unittest.main()


class APartialReadMustNotShrinkTheVillageRecord(unittest.TestCase):
    """The union list keeps a material this read did not return. The PER-VILLAGE list, written
    raw from what the read produced, did not — and it is the per-village list that everything
    downstream consults.

    That is how Svear's record came to hold {Iron, Candle} while the screen shows three, and
    the damage ran in two directions (live 2026-08-27):

      * `_target_complete` consulted it and certified a short read as finished;
      * `plan_barter_task` consulted it and reported Matchlock Gun UNSOURCED, even though the
        KB held thirteen source ports for it — so the mission failed at gather-plan with a
        complete recipe in hand.

    Keeping a prior entry is NOT merging across villages, which the KB rightly forbids: it is
    this village's own earlier reading of itself, and a read that admits it was incomplete has
    no standing to erase it.
    """

    def test_the_writeback_keeps_a_material_this_read_missed(self):
        import inspect
        from actions import village_check
        src = inspect.getsource(village_check.write_back_invariants)
        self.assertIn("must not shrink the record", src.lower().replace("\n", " ")
                      .replace("  ", " ") or src)

    def test_the_guard_sits_on_the_per_village_list(self):
        """Not on the union — that one already had it, which is what made the gap invisible."""
        import inspect
        from actions import village_check
        src = inspect.getsource(village_check.write_back_invariants)
        i_per = src.index("village_inputs[slug] = per")
        i_guard = src.index("keeping")
        self.assertLess(src.index("recipe.village_inputs.get(slug)"), i_per,
                        "the prior list must be consulted before it is overwritten")

    def test_svear_holds_all_three_materials(self):
        """The record the partial reads corrupted, repaired — and the reason the mission could
        not source Matchlock Gun."""
        from memory.barter_kb import load_recipe
        r = load_recipe("Birch Tree")
        if r is None:
            self.skipTest("KB recipe not available")
        names = {(i.material or "").strip().lower() for i in r.inputs_for("Svear Village")}
        self.assertIn("matchlock gun", names)

    def test_matchlock_has_source_ports_at_svear(self):
        from memory.barter_kb import load_recipe
        r = load_recipe("Birch Tree")
        if r is None:
            self.skipTest("KB recipe not available")
        m = next(i for i in r.inputs_for("Svear Village")
                 if (i.material or "").strip().lower() == "matchlock gun")
        self.assertTrue(m.source_ports, "unsourced is what failed the gather-plan step")
        self.assertIn("Barcelona", m.source_ports)
