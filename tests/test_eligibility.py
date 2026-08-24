"""Tests for the barter eligibility filter (#18)."""
import unittest

from memory.barter_kb import BarterRecipe, Preconditions, Village, FleetState
from brain.eligibility import evaluate_eligibility, eligible_recipes


def _recipe(good="Camas", **pre):
    return BarterRecipe(good=good, preconditions=Preconditions(**pre))


def _village(**kw):
    base = dict(name="Apache", amity="Favorable", barter_rounds_remaining=3,
                eligible_goods=["Camas"], locked_goods=[])
    base.update(kw)
    return Village(**base)


def _fleet(**kw):
    return FleetState(**kw)


class EligibilityTests(unittest.TestCase):
    def test_eligible_when_all_met(self):
        e = evaluate_eligibility(_recipe(amity_min="Favorable"), _village(), _fleet())
        self.assertTrue(e.eligible)
        self.assertEqual(e.reasons, [])

    def test_amity_too_low(self):
        e = evaluate_eligibility(_recipe(amity_min="Friendly"),
                                 _village(amity="Neutral"), _fleet())
        self.assertFalse(e.eligible)
        self.assertTrue(any("amity" in r for r in e.reasons))

    def test_amity_higher_is_ok(self):
        e = evaluate_eligibility(_recipe(amity_min="Favorable"),
                                 _village(amity="Friendly"), _fleet())
        self.assertTrue(e.eligible)

    def test_no_rounds_remaining_blocks_even_if_amity_ok(self):
        # rounds == 0 is the red 'Cannot Exchange' note, independent of amity.
        e = evaluate_eligibility(_recipe(amity_min="Neutral"),
                                 _village(barter_rounds_remaining=0), _fleet())
        self.assertFalse(e.eligible)
        self.assertTrue(any("rounds" in r for r in e.reasons))

    def test_locked_good_blocks(self):
        e = evaluate_eligibility(_recipe(good="Wampum"),
                                 _village(locked_goods=["Wampum"]), _fleet())
        self.assertFalse(e.eligible)
        self.assertTrue(any("locked" in r for r in e.reasons))

    def test_trade_level_and_guild_and_event(self):
        rec = _recipe(trade_level_min=10, guild="Merchants", event="Summer Fair")
        # missing all three
        e = evaluate_eligibility(rec, _village(), _fleet(trade_level=5, guild="Pirates"))
        self.assertFalse(e.eligible)
        self.assertEqual(len(e.reasons), 3)
        # all satisfied
        e2 = evaluate_eligibility(rec, _village(),
                                  _fleet(trade_level=12, guild="Merchants"),
                                  active_events=["Summer Fair"])
        self.assertTrue(e2.eligible)

    def test_negotiation_expertise_gate(self):
        rec = _recipe(negotiation_expertise_min=5)
        self.assertFalse(evaluate_eligibility(rec, _village(), _fleet(negotiation_expertise=3)).eligible)
        self.assertTrue(evaluate_eligibility(rec, _village(), _fleet(negotiation_expertise=8)).eligible)

    def test_eligible_recipes_filters(self):
        recipes = [_recipe("Camas", amity_min="Favorable"),
                   _recipe("Wampum", amity_min="Friendly")]
        got = eligible_recipes(recipes, _village(amity="Favorable"), _fleet())
        self.assertEqual([r.good for r in got], ["Camas"])


if __name__ == "__main__":
    unittest.main()
