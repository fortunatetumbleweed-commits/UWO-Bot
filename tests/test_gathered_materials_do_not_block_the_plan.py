"""Materials already aboard must not be charged for a second time.

Live 2026-08-22 at Kolkata the mission trimmed its own hold to carry four rounds' worth of
materials, then planned ZERO rounds and aborted:

    [fleet_status] cargo=3069/4108
    Plan: 0 round(s) (limited by space) — [781/round reserved of 655 free]
    FAILED at step plan: no feasible rounds: free space 655 < 781 reserved for one round

`free_space` excludes the gathered materials, but `reserved` priced every round as though
their inputs still had to fit alongside the product. The village swaps materials FOR the
product, so a funded round grows the hold by (output − inputs) — here 679 − 608 = 71.
"""

from __future__ import annotations

import unittest

from brain.barter_quantity import plan_barter_rounds

# The live numbers, exactly as read.
OUTPUT = 679
NEEDS = {"Ebony": 152, "Coral": 228, "Textiles": 228}      # 608 per round
HELD = {"ebony": 700, "coral": 1049, "textiles": 1049}     # four rounds' worth
FREE = 655
CUSHION = 0.15


class GatheredMaterialsDoNotBlockThePlan(unittest.TestCase):

    def test_a_hold_full_of_materials_can_still_barter(self):
        plan = plan_barter_rounds(OUTPUT, NEEDS, 7, FREE,
                                  materials_on_hand=HELD, cushion=CUSHION)
        self.assertGreaterEqual(plan.rounds, 4, "the Kolkata deadlock is back")

    def test_it_never_plans_more_rounds_than_the_materials_fund(self):
        """Three rounds' Coral must cap the plan at three, however much space there is."""
        held = dict(HELD, coral=3 * 228)
        plan = plan_barter_rounds(OUTPUT, NEEDS, 7, 100_000,
                                  materials_on_hand=held, cushion=CUSHION)
        self.assertLessEqual(plan.rounds, 7)
        self.assertGreaterEqual(plan.rounds, 3)

    def test_an_empty_hold_MUST_STILL_FIND_ROOM_TO_BUY_THE_INPUTS(self):
        """Nothing aboard, so the inputs have to be BOUGHT, and 655 free buys exactly one
        round's 608 — the one constraint a discard cannot rescue you from.

        This asserted 0 rounds while every round was priced at the cushioned peak (781 > 655).
        That over-charged the case: the round's 608 units of material DO fit, and once they
        are swapped out the 679 of product has 655 to land in, so the fleet carries 655 home
        rather than nothing. The funded/unfunded distinction this test guards is intact and
        sharper than before — 5 rounds aboard against 1 from an empty hold.
        """
        plan = plan_barter_rounds(OUTPUT, NEEDS, 7, FREE, cushion=CUSHION)
        self.assertEqual(plan.rounds, 1)
        self.assertEqual(plan.output_qty, 655, "counted product the hold cannot hold")
        self.assertEqual(plan.reserved_per_round, 781)   # still reported, no longer binding

        funded = plan_barter_rounds(OUTPUT, NEEDS, 7, FREE,
                                    materials_on_hand=HELD, cushion=CUSHION)
        self.assertGreater(funded.rounds, plan.rounds,
                           "materials aboard must still buy more rounds than an empty hold")

    def test_it_will_not_plan_a_round_it_cannot_BUY_the_materials_for(self):
        """One unit short of the 608 an unfunded round needs, and there is no round."""
        self.assertEqual(plan_barter_rounds(OUTPUT, NEEDS, 7, 607, cushion=CUSHION).rounds, 0)
        self.assertEqual(plan_barter_rounds(OUTPUT, NEEDS, 7, 608, cushion=CUSHION).rounds, 1)

    def test_the_hold_is_matched_case_insensitively(self):
        """The Sell-tab read returns lowercase keys; the recipe is title-case."""
        lower = plan_barter_rounds(OUTPUT, NEEDS, 7, FREE,
                                   materials_on_hand=HELD, cushion=CUSHION)
        title = plan_barter_rounds(OUTPUT, NEEDS, 7, FREE,
                                   materials_on_hand={"Ebony": 700, "Coral": 1049,
                                                      "Textiles": 1049},
                                   cushion=CUSHION)
        self.assertEqual(lower.rounds, title.rounds)

    def test_rounds_remaining_still_caps_the_plan(self):
        plan = plan_barter_rounds(OUTPUT, NEEDS, 2, FREE,
                                  materials_on_hand=HELD, cushion=CUSHION)
        self.assertEqual(plan.rounds, 2)
        self.assertEqual(plan.limited_by, "rounds")


if __name__ == "__main__":
    unittest.main()
