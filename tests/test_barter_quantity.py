"""Tests for the barter quantity solver (#19). Camas numbers are from the live panel:
709/round at Neutral, inputs Luxuries:170 + Food:170."""
import unittest

from brain.barter_quantity import solve_barter_quantity, output_for_amity, BarterPlan
from memory.barter_kb import BarterRecipe


CAMAS_INPUTS = [("Luxuries", 170), ("Food", 170)]


class BarterQuantityTests(unittest.TestCase):
    def test_material_limited(self):
        # 862 Luxuries, 881 Food → floor(862/170)=5, floor(881/170)=5 → 5 rounds.
        p = solve_barter_quantity(709, CAMAS_INPUTS,
                                  {"Luxuries": 862, "Food": 881},
                                  rounds_remaining=8, free_space=100000)
        self.assertEqual(p.rounds, 5)
        self.assertEqual(p.limited_by, "materials")
        self.assertEqual(p.output_qty, 5 * 709)
        self.assertEqual(p.materials_consumed, {"Luxuries": 850, "Food": 850})

    def test_rounds_limited(self):
        p = solve_barter_quantity(709, CAMAS_INPUTS,
                                  {"Luxuries": 5000, "Food": 5000},
                                  rounds_remaining=3, free_space=100000)
        self.assertEqual(p.rounds, 3)
        self.assertEqual(p.limited_by, "rounds")

    def test_overflow_reported_when_allowed(self):
        # 5 rounds, net = 709 - 340 = 369/round → 1845 fill; free 1000 → overflow 845.
        p = solve_barter_quantity(709, CAMAS_INPUTS,
                                  {"Luxuries": 862, "Food": 881},
                                  rounds_remaining=8, free_space=1000)
        self.assertEqual(p.rounds, 5)
        self.assertEqual(p.overflow_units, 5 * 369 - 1000)

    def test_space_limited_when_overflow_disallowed(self):
        p = solve_barter_quantity(709, CAMAS_INPUTS,
                                  {"Luxuries": 862, "Food": 881},
                                  rounds_remaining=8, free_space=1000,
                                  allow_overflow=False)
        self.assertEqual(p.rounds, 1000 // 369)   # 2 rounds fit
        self.assertEqual(p.limited_by, "space")
        self.assertEqual(p.overflow_units, 0)

    def test_zero_when_no_materials(self):
        p = solve_barter_quantity(709, CAMAS_INPUTS, {"Luxuries": 0, "Food": 0},
                                  rounds_remaining=8, free_space=100000)
        self.assertEqual(p.rounds, 0)
        self.assertEqual(p.limited_by, "materials")

    def test_output_for_amity(self):
        r = BarterRecipe(good="Camas",
                         output_per_round={"Neutral": 709, "Favorable": 744, "Friendly": 813})
        self.assertEqual(output_for_amity(r, "Friendly"), 813)
        self.assertEqual(output_for_amity(r, "Neutral"), 709)
        self.assertIsNone(output_for_amity(r, "Trusting"))


if __name__ == "__main__":
    unittest.main()


# ── Pre-gather plan: capacity + supply aware (docs/barter_command_flow.md #3) ──

from brain.barter_quantity import (BarterRoundsPlan, free_space_for_barter,
                                   plan_barter_rounds)


def test_free_space_subtracts_cargo_and_a_seven_day_supply_reserve():
    # 7 days × 27.42/day = 192 units of EACH of water and food → 384 reserved.
    assert free_space_for_barter(4108, 566) == 4108 - 566 - 384
    assert free_space_for_barter(100, 0) == 0            # never negative


def test_rounds_run_until_the_HOLD_SATURATES_not_until_a_reservation_runs_out():
    """Camas: 280 in / 953 out, 3400 free. Simulated, carried away by round count:

           1 -> 953    2 -> 1906    3 -> 2859    4 -> 3400    5,6,7 -> 3400

    So 4 is the answer: it carries 541 more than 3, and nothing beyond it gains anything.

    This asserted 3 while the planner subtracted a per-round reservation from free space
    until it ran out. That cannot find the optimum, because it prices a round BEFORE the
    materials leave the hold, and because it reads "the output does not all fit" as a
    REFUSAL when the game's own answer is to hand back what fits and discard the rest
    (user, 2026-09-04). The old comment's instinct was right — net fill would have said 5,
    which is wasteful — but the peak was the wrong correction; simulating is the right one.
    """
    plan = plan_barter_rounds(953, {"Avocado": 130, "Cassava": 150},
                              rounds_remaining=7, free_space=3400, cushion=0.0)
    assert plan.peak_per_round == 953
    assert plan.rounds == 4
    assert plan.limited_by == "space"


def test_it_stops_at_saturation_rather_than_burning_rounds_for_nothing():
    """Past the point where the hold fills, another round costs a daily count, its materials
    and a market refresh, and carries not one extra unit home."""
    plan = plan_barter_rounds(953, {"Avocado": 130, "Cassava": 150},
                              rounds_remaining=20, free_space=3400, cushion=0.0)
    assert plan.rounds == 4, "kept planning rounds whose output is entirely discarded"


def test_materials_can_be_the_peak_when_they_outweigh_the_output():
    # 500 in / 100 out → the gather leg is what must fit, not the output.
    plan = plan_barter_rounds(100, {"A": 250, "B": 250},
                              rounds_remaining=7, free_space=1000, cushion=0.0)
    assert plan.peak_per_round == 500
    assert plan.rounds == 2


def test_daily_rounds_bind_when_space_is_plentiful():
    plan = plan_barter_rounds(953, {"Avocado": 130}, rounds_remaining=2,
                              free_space=1_000_000, cushion=0.0)
    assert plan.rounds == 2
    assert plan.limited_by == "rounds"


def test_total_needs_is_the_own_goal_and_buy_targets_subtract_stock():
    plan = plan_barter_rounds(500, {"Coral": 100, "Ebony": 50}, rounds_remaining=3,
                              free_space=1_000_000, cushion=0.0,
                              materials_on_hand={"Coral": 250})
    assert plan.total_needs == {"Coral": 300, "Ebony": 150}
    assert plan.buy_targets == {"Coral": 50, "Ebony": 150}


def test_a_hold_too_small_for_one_round_yields_zero_rounds():
    plan = plan_barter_rounds(953, {"Avocado": 130}, rounds_remaining=7, free_space=100,
                              cushion=0.0)
    assert plan.rounds == 0
    assert plan.output_qty == 0
    assert plan.limited_by == "space"


def test_no_rounds_left_today_yields_zero():
    plan = plan_barter_rounds(953, {"Avocado": 130}, rounds_remaining=0,
                              free_space=1_000_000, cushion=0.0)
    assert plan.rounds == 0
    assert plan.limited_by == "rounds"


# ── Amity / 6h-refresh cushion ────────────────────────────────────────────────
# The check is a snapshot: quantities re-roll every ~6h, and bartering moves amity —
# crossing a TIER changes the ratio (higher amity = more favourable). Both directions
# break a plan sized to the snapshot exactly.

from brain.barter_quantity import AMITY_CUSHION


def _nutmeg(free_space, cushion):
    return plan_barter_rounds(552, {"Ebony": 152, "Coral": 180, "Textiles": 204},
                              rounds_remaining=7, free_space=free_space, cushion=cushion)


def test_cushion_buys_extra_material_against_a_tier_going_DOWN():
    # A worse ratio needs MORE material per round; the cushion is bought up front.
    plain = _nutmeg(3158, 0.0)
    hedged = _nutmeg(3158, 0.15)
    per_round_plain = {m: q / plain.rounds for m, q in plain.total_needs.items()}
    per_round_hedged = {m: q / hedged.rounds for m, q in hedged.total_needs.items()}
    for m in plain.needs_per_round:
        assert per_round_hedged[m] > per_round_plain[m]


def test_a_tier_going_UP_no_longer_needs_hold_RESERVED_for_it():
    """The cushion used to hold space back in case the ratio improved and the output came
    back bigger than the snapshot. Under the game's own overflow rule that is not a hazard:
    a bigger yield fills the hold sooner and the surplus is discarded, so the fleet carries a
    full hold either way. What the cushion still does — and must — is buy MORE MATERIAL in
    case the ratio goes the other way; see the DOWN test above.

    `reserved_per_round` is still computed and reported, because a plan that cannot say what
    it thought a round would cost is harder to argue with. It just no longer bounds the count.
    """
    hedged = _nutmeg(3158, 0.15)
    assert hedged.reserved_per_round == 635          # ceil(552 × 1.15), still reported
    assert hedged.rounds == _nutmeg(3158, 0.0).rounds


def test_the_cushioned_gather_still_fits_the_hold():
    hedged = _nutmeg(3158, 0.15)
    assert sum(hedged.total_needs.values()) <= hedged.free_space


def test_the_cushion_no_longer_costs_a_round():
    """It did — 5 without, 4 with — because it inflated the per-round hold reservation. That
    reservation is gone (the count is simulated now), so hedging the ratio no longer costs
    cargo. The material hedge is unaffected."""
    assert _nutmeg(3158, 0.0).rounds == 5
    assert _nutmeg(3158, 0.15).rounds == 5
    assert (sum(_nutmeg(3158, 0.15).total_needs.values())
            > sum(_nutmeg(3158, 0.0).total_needs.values())), "the material hedge was lost"


def test_cushion_is_free_when_the_daily_allowance_binds():
    # Space to spare → the cushion changes nothing but the material bought.
    plain = _nutmeg(1_000_000, 0.0)
    hedged = _nutmeg(1_000_000, 0.15)
    assert plain.rounds == hedged.rounds == 7


def test_output_qty_stays_the_nominal_estimate():
    # The cushion is reserved space, not a prediction of extra output.
    hedged = _nutmeg(3158, 0.15)
    assert hedged.output_qty == hedged.rounds * 552


def test_zero_cushion_plans_on_the_snapshot_exactly():
    plain = _nutmeg(3158, 0.0)
    assert plain.reserved_per_round == plain.peak_per_round
    assert plain.total_needs == {"Ebony": 760, "Coral": 900, "Textiles": 1020}


def test_default_cushion_is_applied_when_not_asked_for():
    assert AMITY_CUSHION > 0
    assert _nutmeg(3158, AMITY_CUSHION).rounds == plan_barter_rounds(
        552, {"Ebony": 152, "Coral": 180, "Textiles": 204},
        rounds_remaining=7, free_space=3158).rounds


# ── On-arrival panel ground truth (gap #6) ───────────────────────────────────

from brain.barter_quantity import panel_barter_state


class _Mat:
    """Stand-in for actions.barter_reader.BarterMaterial."""
    def __init__(self, label, have, need):
        self.label, self.have, self.need = label, have, need


def test_panel_state_counts_full_rounds_the_hold_can_fund():
    st = panel_barter_state([_Mat("Ebony", 760, 152), _Mat("Coral", 900, 180),
                             _Mat("Textiles", 1020, 204)], output_per_round=552)
    assert st.rounds_remaining == 5
    assert st.partial_fraction == 0.0            # 760/152 etc. divide exactly
    # `shortfall` always describes ONE MORE round than can be funded — here the 6th,
    # which needs a full set because the 5 rounds consume everything aboard.
    assert st.shortfall == {"Ebony": 152, "Coral": 180, "Textiles": 204}


def test_shortfall_is_what_one_more_round_would_need():
    st = panel_barter_state([_Mat("Ebony", 200, 152), _Mat("Coral", 900, 180)])
    assert st.rounds_remaining == 1
    assert st.shortfall == {"Ebony": 104}        # 48 left of the 152 a 2nd round needs
    assert "Coral" not in st.shortfall           # Coral could fund it


def test_melanesian_2026_08_20_regression():
    """The fleet-death session, reconstructed from the recorded numbers.

    Materials at the panel were 25% of the gather totals (Ebony 681→167, Coral 1020→252,
    Textiles 900→222). The plan wanted 6 rounds; the hold funded ONE, plus ~9.9% of a
    second — which is exactly the partial Exchange that yielded 53 of 552, and the amity
    delta scaled with it (+2,901 after +29,859 = 9.7%). This is the case the loop must
    catch on arrival instead of discovering afterwards."""
    st = panel_barter_state([_Mat("Ebony", 167, 152), _Mat("Coral", 252, 204),
                             _Mat("Textiles", 222, 180)], output_per_round=552)
    assert st.rounds_remaining == 1                  # NOT the 6 the plan asked for
    assert st.binding == "Ebony"
    assert 0.095 <= st.partial_fraction <= 0.10      # the observed 53/552 ≈ 9.6%
    assert st.shortfall["Ebony"] == 137              # 152 needed − 15 left
    # And the partial's predicted yield lands on the 53 that was actually received.
    assert round(552 * st.partial_fraction) == 54


def test_a_short_material_greys_out_the_exchange_zero_full_rounds():
    st = panel_barter_state([_Mat("Ebony", 100, 152), _Mat("Textiles", 0, 180)],
                            output_per_round=552)
    assert st.rounds_remaining == 0
    assert st.binding == "Textiles"
    assert st.partial_fraction == 0.0


def test_rows_are_keyed_by_position_so_repeated_labels_do_not_collapse():
    # Panel labels are tile OCR and can come back as a shared category name.
    st = panel_barter_state([_Mat("Fabrics", 400, 100), _Mat("Fabrics", 50, 100)])
    assert st.rounds_remaining == 0                  # the SECOND row binds, not the first
    assert st.partial_fraction == 0.5


def test_plain_have_need_pairs_are_accepted():
    st = panel_barter_state([(760, 152), (900, 180)])
    assert st.rounds_remaining == 5


def test_an_empty_panel_funds_nothing():
    assert panel_barter_state([]).rounds_remaining == 0


# ── The swap, and why a full hold does not stop a round (user, 2026-09-04) ────
#
# "It was full but buying say 200 raisins can do another round, and the exchange will swap the
# pigs and raisin to groundnuts, so it will need to abandon some groundnuts, but still will
# get more profits."
#
# A round is a SWAP: materials leave the hold, product arrives, and the game hands back only
# what fits — the "N has not been claimed yet, unclaimed trade goods will be discarded" prompt.
# The value ratio is what makes the discard irrelevant: Bambara Groundnut sold at 40,600
# profit/unit against Pig and Raisin at a few hundred, so a round converts ~434 units of
# ~300-ducat material into up to 1,036 units of 40,600-ducat product — about 300:1.
#
# Live 2026-09-04 at Hutu the mission planned 3 rounds, ran 4, and stopped holding 501 Pig and
# 1 Raisin. The optimum was 5 (the hold saturates there); at 40,600/unit the gap between the
# plan and the optimum was roughly 59M ducats.


def _hutu(free_space, on_hand=None, rounds_remaining=8):
    return plan_barter_rounds(1036, {"Raisin": 217, "Pig": 217},
                              rounds_remaining=rounds_remaining, free_space=free_space,
                              materials_on_hand=on_hand, cushion=0.0)


def test_the_hutu_run_should_have_planned_five_rounds_not_three():
    """free 4,568, 434 in, 1,036 out. Carried away by round count:

           3 -> 3,108      4 -> 4,144      5 -> 4,568      10 -> 4,568

    The old reservation loop planned 3 (4568 / (1036 x 1.15) = 3.8)."""
    assert _hutu(4568).rounds == 5


def test_a_FUNDED_round_is_never_refused_for_space():
    """Its materials are already aboard, so it costs the hold nothing it does not already
    hold — it swaps them. 434 of material aboard and only 307 free still funds a round."""
    plan = _hutu(307, on_hand={"Raisin": 434, "Pig": 434})
    assert plan.rounds >= 2, "a full hold refused a round it had already paid for"


def test_dead_material_aboard_is_still_worth_converting():
    """The shape of the loss at Hutu: material aboard, hold nearly full. Before this, the
    planner returned zero rounds and the fleet carried the material home unconverted."""
    assert _hutu(307, on_hand={"Raisin": 500, "Pig": 500}).rounds >= 2


def test_materials_that_cannot_be_LOADED_still_bound_the_plan():
    """The one thing a discard cannot rescue: you must be able to put the materials aboard in
    the first place. Unfunded rounds are still limited by room for the purchase."""
    plan = _hutu(400)                      # 400 free, 434 needed for one unfunded round
    assert plan.rounds == 0
    assert plan.limited_by == "space"


def test_the_daily_allowance_still_binds_when_space_is_ample():
    plan = _hutu(1_000_000, rounds_remaining=3)
    assert plan.rounds == 3
    assert plan.limited_by == "rounds"
