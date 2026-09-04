"""The 'Insufficient Empty Space' overflow flow, replayed against the REAL frames.

Source: data/sessions/barter_apache_walkthrough_2026-08-14T12-48-30 (human-played Camas
run). The recorded sequence is pending 143 → 131 → 100 → 0 while Camas rises
3,613 → 3,625 → 3,656 → 3,756 (exactly +143) and supplies fall 226 → 176.

The element fixtures below are the actual OmniParser output for those frames, so these
tests fail if the parser drifts from what the game really renders."""
import types
import unittest

from actions.overflow_dialog import (CargoTile, OverflowState, build_cargo,
                                     is_discard_dialog, is_last_round, is_overflow_dialog,
                                     plan_for_overflow, read_discard, read_overflow)
from brain.supply_planner import supply_needed_each


def _el(kind, label, x1, y1, x2, y2):
    return types.SimpleNamespace(element_type=kind, label=label, x1=x1, y1=y1, x2=x2,
                                 y2=y2, cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


def _overflow_frame(pending, tiles):
    """frame_0014 geometry: header positions are real; tile quantities vary by step."""
    els = [
        _el("text", "Insufficient Empty Space", 1011, 115, 1389, 157),
        _el("text", "Cannot receive item due to insufficient space.", 561, 186, 1607, 231),
        _el("text", "Unreceived trade goods will be discarded.", 563, 228, 1125, 272),
        _el("text", "Received Trade Goods", 561, 291, 879, 327),
        _el("text", "Cargo", 558, 485, 650, 527),
        _el("text", "4,108/4,108 (100%)", 1583, 487, 1839, 523),
        _el("button", "Receive", 1090, 895, 1310, 965),
    ]
    if pending is not None:
        els.append(_el("button", str(pending), 554, 336, 697, 472))
    for i, q in enumerate(tiles):
        x = 558 + i * 129
        els.append(_el("button", f"{q:,}", x, 523, x + 130, 650))
    return els


def _discard_frame(name, selected, held):
    """frame_0016 / frame_0020 geometry."""
    return [
        _el("text", "Insufficient Empty Space", 1011, 115, 1389, 157),
        _el("text", "Discard Goods", 1091, 237, 1311, 273),
        _el("text", name, 1133, 493, 1269, 531),
        _el("text", f"{selected}/{held}", 1160, 562, 1242, 594),
        _el("text", "Discarded items cannot be recovered.", 949, 622, 1447, 663),
        _el("button", "Cancel", 984, 771, 1200, 846),
        _el("button", "Ok", 1199, 769, 1417, 847),
        _el("button", "Receive", 1092, 896, 1310, 965),
    ]


class DialogReadingTests(unittest.TestCase):
    def test_the_opening_state_reads(self):
        st = read_overflow(_overflow_frame(143, [226, 226, 31, 12, 3613]))
        self.assertEqual(st.pending, 143)
        self.assertEqual((st.cargo_used, st.cargo_capacity), (4108, 4108))
        self.assertEqual(sorted(t.qty for t in st.tiles), [12, 31, 226, 226, 3613])
        self.assertIsNotNone(st.receive)

    def test_the_recorded_sequence_of_states(self):
        # 143 → 131 → 100 → cleared, with Camas climbing by exactly the amount received.
        steps = [(143, [226, 226, 31, 12, 3613]),
                 (131, [3625, 31, 226, 226]),
                 (100, [3656, 226, 226]),
                 (None, [3756, 176, 176])]
        pend = [read_overflow(_overflow_frame(p, t)).pending for p, t in steps]
        self.assertEqual(pend, [143, 131, 100, None])
        camas = [max(t.qty for t in read_overflow(_overflow_frame(p, t)).tiles)
                 for p, t in steps]
        self.assertEqual(camas[-1] - camas[0], 143)      # everything was received

    def test_the_discard_dialog_names_the_item(self):
        dc = read_discard(_discard_frame("Avocado", 12, 12))
        self.assertEqual((dc.name, dc.selected, dc.held), ("Avocado", 12, 12))
        self.assertIsNotNone(dc.ok)
        self.assertIsNotNone(dc.cancel)

    def test_a_partial_supply_discard_reads(self):
        dc = read_discard(_discard_frame("Food", 50, 226))
        self.assertEqual((dc.name, dc.selected, dc.held), ("Food", 50, 226))

    def test_the_two_dialogs_are_told_apart(self):
        ov, dc = _overflow_frame(143, [12]), _discard_frame("Food", 50, 226)
        self.assertTrue(is_overflow_dialog(ov) and not is_discard_dialog(ov))
        self.assertTrue(is_discard_dialog(dc))


def _probed():
    """What probing the frame-14 tiles yields — names from frames 16 and 20."""
    return [{"name": "Cassava", "qty": 31, "tile": CargoTile(31)},
            {"name": "Avocado", "qty": 12, "tile": CargoTile(12)},
            {"name": "Camas", "qty": 3613, "tile": CargoTile(3613)},
            {"name": "Water", "qty": 226, "tile": CargoTile(226)},
            {"name": "Food", "qty": 226, "tile": CargoTile(226)}]


class PlanningTests(unittest.TestCase):
    def _plan(self, days):
        r = supply_needed_each(days)
        return plan_for_overflow(OverflowState(pending=143, cargo_used=4108,
                                               cargo_capacity=4108),
                                 _probed(), output_good="Camas",
                                 reserves={"water": r, "food": r})

    def test_it_clears_143_the_way_the_human_did(self):
        plan, shortfall = self._plan(6.0)
        self.assertEqual(shortfall, 0)
        self.assertEqual(sum(d.qty for d in plan), 143)
        dumped = {d.name: d.qty for d in plan}
        self.assertEqual(dumped["Cassava"], 31)          # leftover materials go first
        self.assertEqual(dumped["Avocado"], 12)
        self.assertEqual(dumped["Water"] + dumped["Food"], 100)   # then spare supply only

    def test_the_barter_output_is_never_a_candidate(self):
        # The bug the simulation caught: pricing Camas high still let the planner walk
        # every trade good before touching a supply, so it dumped 100 Camas.
        for days in (6.0, 7.0, 9.0):
            plan, _ = self._plan(days)
            self.assertNotIn("Camas", [d.name for d in plan])
        self.assertNotIn("Camas", [c.name for c in build_cargo(
            _probed(), output_good="Camas", reserves={"water": 1, "food": 1})])

    def test_supplies_are_never_cut_below_the_reserve(self):
        reserve = supply_needed_each(6.0)
        plan, _ = self._plan(6.0)
        for d in plan:
            if d.resource:
                self.assertLessEqual(d.qty, 226 - reserve)

    def test_a_stricter_reserve_reports_a_shortfall_instead_of_starving(self):
        # A village round trip leaves too little spare supply to absorb 143.
        plan, shortfall = self._plan(7.0)
        self.assertEqual(shortfall, 32)
        self.assertEqual(sum(d.qty for d in plan), 111)
        self.assertNotIn("Camas", [d.name for d in plan])

    def test_supplies_are_matched_by_name_case_insensitively(self):
        cargo = build_cargo([{"name": "WATER", "qty": 226}], output_good="Camas",
                            reserves={"water": 165, "food": 165})
        self.assertEqual(cargo[0].resource, "water")


if __name__ == "__main__":
    unittest.main()


# ── THE LAST ROUND: when leftover materials may be dumped (user, 2026-09-04) ──
#
# Frames 15-17 of the walkthrough are the whole mechanic. Tapping the 12-unit Avocado tile
# opens Discard Goods (frame 16, spinner defaulted to 12/12); OK dumps it and the overflow
# dialog UPDATES IN PLACE (frame 17) — pending 143 -> 131, the Avocado tile gone, Camas
# 3,613 -> 3,625, cargo still 4,108/4,108. Freeing N units receives N more, one for one.
#
# That is only correct at the END. A round needs 130 Avocado and 150 Cassava, so the 12 and
# 31 aboard at frame 15 are leftovers that no round can use — dead weight worth converting.
# Dump the same materials one round earlier and the space bought costs a whole round of
# product, which at Hutu prices ran to ~40,600 ducats a unit.

CAMAS_RECIPE = {"Avocado": 130, "Cassava": 150}


def _mid_round():
    """The same overflow, but with materials for several more rounds still aboard."""
    return [{"name": "Cassava", "qty": 900, "tile": CargoTile(900)},
            {"name": "Avocado", "qty": 800, "tile": CargoTile(800)},
            {"name": "Camas", "qty": 3613, "tile": CargoTile(3613)},
            {"name": "Water", "qty": 226, "tile": CargoTile(226)},
            {"name": "Food", "qty": 226, "tile": CargoTile(226)}]


class TheLastRoundGate(unittest.TestCase):

    def test_frame_15_leftovers_cannot_fund_a_round(self):
        """12 Avocado and 31 Cassava against 130 and 150 — the recorded last round."""
        self.assertIs(is_last_round(_probed(), CAMAS_RECIPE), True)

    def test_materials_for_more_rounds_are_not_the_last_round(self):
        self.assertIs(is_last_round(_mid_round(), CAMAS_RECIPE), False)

    def test_ONE_material_running_short_ends_it(self):
        """A round needs every input, so the binding one decides. 800 Avocado is plenty and
        140 Cassava is ten short, and that is the end of the bartering."""
        found = [{"name": "Avocado", "qty": 800, "tile": CargoTile(800)},
                 {"name": "Cassava", "qty": 140, "tile": CargoTile(140)}]
        self.assertIs(is_last_round(found, CAMAS_RECIPE), True)

    def test_exactly_enough_is_not_the_last_round(self):
        found = [{"name": "Avocado", "qty": 130, "tile": CargoTile(130)},
                 {"name": "Cassava", "qty": 150, "tile": CargoTile(150)}]
        self.assertIs(is_last_round(found, CAMAS_RECIPE), False)

    def test_an_unknown_recipe_is_None_not_False(self):
        """Without the recipe a material cannot be told from any other cargo. Saying False
        would quietly claim 'a round is still funded' on no evidence."""
        self.assertIsNone(is_last_round(_probed(), None))
        self.assertIsNone(is_last_round(_probed(), {}))


class MaterialsAreProtectedUntilTheLastRound(unittest.TestCase):

    def _plan(self, found, days=6.0):
        r = supply_needed_each(days)
        return plan_for_overflow(OverflowState(pending=143, cargo_used=4108,
                                               cargo_capacity=4108),
                                 found, output_good="Camas",
                                 reserves={"water": r, "food": r},
                                 needs_per_round=CAMAS_RECIPE)

    def test_a_MID_round_overflow_never_touches_the_materials(self):
        """THE BUG THIS FIXES. Materials were dump candidates on EVERY round, so this plan
        used to open by throwing away the inputs for every round still to come.

        It costs something to protect them, and the cost is the point. Only 122 units of
        spare supply exist, so 21 of the 143 pending Camas are given up rather than cleared.
        That is the right way round: 21 units of output against 800 Avocado and 900 Cassava
        — five more rounds, thousands of units of product."""
        plan, shortfall = self._plan(_mid_round())
        names = [d.name for d in plan]
        self.assertNotIn("Cassava", names)
        self.assertNotIn("Avocado", names)
        self.assertNotIn("Camas", names)
        self.assertTrue(all(d.resource for d in plan), "only spare supply should be spent")
        self.assertEqual(sum(d.qty for d in plan), 122)
        self.assertEqual(shortfall, 21)

    def test_the_LAST_round_dumps_them_the_way_the_human_did(self):
        """Frames 15-20: Avocado 12 and Cassava 31 first, then 100 of spare supply."""
        plan, shortfall = self._plan(_probed())
        dumped = {d.name: d.qty for d in plan}
        self.assertEqual(dumped["Cassava"], 31)
        self.assertEqual(dumped["Avocado"], 12)
        self.assertEqual(dumped["Water"] + dumped["Food"], 100)
        self.assertEqual(shortfall, 0)

    def test_materials_are_dumped_BEFORE_spare_supply(self):
        """Supply is the fleet's safety margin and a material on the last round is worth
        nothing, so the order is not arbitrary — the materials must come first."""
        plan, _ = self._plan(_probed())
        first_supply = next(i for i, d in enumerate(plan) if d.resource)
        materials = [i for i, d in enumerate(plan) if d.name in ("Avocado", "Cassava")]
        self.assertTrue(all(i < first_supply for i in materials))

    def test_protecting_them_can_cost_a_shortfall_and_that_is_correct(self):
        """A protected material may leave the overflow uncleared, and losing some output now
        is the right trade against losing every remaining round's output."""
        r = supply_needed_each(9.0)
        plan, shortfall = plan_for_overflow(
            OverflowState(pending=143, cargo_used=4108, cargo_capacity=4108),
            _mid_round(), output_good="Camas", reserves={"water": r, "food": r},
            needs_per_round=CAMAS_RECIPE)
        self.assertGreater(shortfall, 0)
        self.assertNotIn("Avocado", [d.name for d in plan])

    def test_without_a_recipe_the_old_behaviour_stands(self):
        """No recipe, no material identification — unchanged, and the caller is told."""
        r = supply_needed_each(6.0)
        plan, _ = plan_for_overflow(
            OverflowState(pending=143, cargo_used=4108, cargo_capacity=4108),
            _probed(), output_good="Camas", reserves={"water": r, "food": r})
        self.assertIn("Cassava", [d.name for d in plan])

    def test_the_output_is_still_never_a_candidate_on_the_last_round(self):
        for found in (_probed(), _mid_round()):
            cargo = build_cargo(found, output_good="Camas", reserves={"water": 1, "food": 1},
                                needs_per_round=CAMAS_RECIPE)
            self.assertNotIn("Camas", [c.name for c in cargo])
