"""The `barter <good> at <village> …` command: grammar + the driver's early exits.

The driver is tested with the device layer stubbed — the remote check, the fleet read
and the mission runner are all injected via module attributes, so no ADB is touched."""
import types
import unittest
from unittest import mock

from actions.village_check import VillageCheck, VillageTrade
from brain.barter_command import (BarterCommand, parse_barter_command,
                                  run_barter_command)


class ParseTests(unittest.TestCase):
    def test_route_tail(self):
        c = parse_barter_command(
            "barter Box of Nutmeg at Melanesian Village, then take the route jakarta to london")
        self.assertEqual(c, BarterCommand("Box of Nutmeg", "Melanesian Village",
                                          "route", "jakarta to london"))

    def test_sail_tail(self):
        c = parse_barter_command("barter Camas at Apache Village, and sail to Edinburgh")
        self.assertEqual(c, BarterCommand("Camas", "Apache Village", "sail", "Edinburgh"))

    def test_no_tail(self):
        c = parse_barter_command("barter Pulque at Apache Village")
        self.assertEqual((c.good, c.village, c.tail_kind), ("Pulque", "Apache Village", "none"))

    def test_then_and_are_interchangeable_and_comma_optional(self):
        a = parse_barter_command("barter Wampum at Apache Village and sail back to London.")
        b = parse_barter_command("barter Wampum at Apache Village, then sail to London")
        self.assertEqual((a.tail_kind, a.tail_value), ("sail", "London"))
        self.assertEqual((b.tail_kind, b.tail_value), ("sail", "London"))

    def test_multiword_good_and_village_survive(self):
        c = parse_barter_command("barter Box of Nutmeg at Melanesian Village")
        self.assertEqual(c.good, "Box of Nutmeg")
        self.assertEqual(c.village, "Melanesian Village")

    def test_not_a_barter_command(self):
        self.assertIsNone(parse_barter_command("sail to London"))
        self.assertIsNone(parse_barter_command("barter Camas"))
        self.assertIsNone(parse_barter_command(""))


def _check(**kw):
    base = dict(village="Apache Village", ok=True,
                barters_used=0, barters_total=7,
                amity_grade="Friendly",
                trades=[VillageTrade("Camas", 953, {"Avocado": 130, "Cassava": 150})])
    base.update(kw)
    return VillageCheck(**base)


class DriverTests(unittest.TestCase):
    """Each test stubs the check + the fleet read, then asserts WHICH step stopped."""

    def _run(self, text, check, status=None, **kw):
        status = status or {"cargo_capacity": 4108, "cargo_used": 0}
        with mock.patch("actions.village_check.read_village_barter_remote",
                        return_value=check), \
             mock.patch("actions.fleet_status.read_fleet_status", return_value=status):
            return run_barter_command(text, dry_run=True, **kw)

    def test_unparseable_stops_at_parse(self):
        res = run_barter_command("go barter something")
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "parse")

    def test_plan_uses_live_check_numbers(self):
        res = self._run("barter Camas at Apache Village, and sail to Edinburgh", _check())
        self.assertTrue(res["ok"])
        plan = res["plan"]
        # free space = 4108 − 0 − 7-day water+food reserve (192 each) = 3724;
        # peak/round = max(130+150, 953) = 953, reserved 1096 with the default cushion
        # → 3 rounds, under the 7 available.
        self.assertEqual(plan.peak_per_round, 953)
        self.assertEqual(plan.rounds, 3)
        self.assertEqual(plan.limited_by, "space")
        self.assertEqual(plan.output_qty, 2859)          # nominal yield, uncushioned
        # materials carry the cushion: ceil(130×3×1.15), ceil(150×3×1.15)
        self.assertEqual(plan.total_needs, {"Avocado": 449, "Cassava": 518})

    def test_cushion_can_be_overridden_from_the_command_line(self):
        res = self._run("barter Camas at Apache Village", _check(), cushion=0.0)
        self.assertEqual(res["plan"].total_needs, {"Avocado": 390, "Cassava": 450})
        self.assertEqual(res["plan"].cushion, 0.0)

    def test_daily_rounds_can_be_the_binding_limit(self):
        res = self._run("barter Camas at Apache Village", _check(barters_used=6),
                        cargo_capacity=100_000, cargo_used=0)
        self.assertEqual(res["plan"].rounds, 1)
        self.assertEqual(res["plan"].limited_by, "rounds")

    def test_village_without_that_good_reports_what_it_offers(self):
        res = self._run("barter Nutmeg at Apache Village", _check())
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "check")
        self.assertIn("Camas", res["reason"])

    def test_no_rounds_left_today(self):
        res = self._run("barter Camas at Apache Village", _check(barters_used=7))
        self.assertFalse(res["ok"])
        self.assertIn("no barter rounds left", res["reason"])

    def test_failed_check_propagates_its_reason(self):
        res = self._run("barter Camas at Apache Village",
                        _check(ok=False, reason="village not found on the world map"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "check")
        self.assertIn("not found", res["reason"])

    def test_unreadable_capacity_refuses_to_guess(self):
        res = self._run("barter Camas at Apache Village", _check(),
                        status={"cargo_capacity": None, "cargo_used": None})
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "plan")
        self.assertIn("capacity unreadable", res["reason"])

    def test_unreadable_current_cargo_also_refuses_to_guess(self):
        # An empty-hold assumption would over-plan the gather — neither guess is safe.
        res = self._run("barter Camas at Apache Village", _check(),
                        status={"cargo_capacity": 4108, "cargo_used": None})
        self.assertFalse(res["ok"])
        self.assertIn("current cargo unreadable", res["reason"])

    def test_overrides_bypass_the_fleet_read(self):
        res = self._run("barter Camas at Apache Village", _check(),
                        status={"cargo_capacity": None, "cargo_used": None},
                        cargo_capacity=4108, cargo_used=0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan"].rounds, 3)

    def test_hold_too_small_for_one_round(self):
        res = self._run("barter Camas at Apache Village", _check(),
                        cargo_capacity=500, cargo_used=0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "plan")
        self.assertIn("no feasible rounds", res["reason"])

    def test_unreadable_daily_progress_is_not_assumed(self):
        res = self._run("barter Camas at Apache Village",
                        _check(barters_used=None, barters_total=None))
        self.assertFalse(res["ok"])
        self.assertIn("Daily Barter Progress unreadable", res["reason"])


class ClearSurplusTests(unittest.TestCase):
    """The opt-in pre-gather clear-sell: it must run BEFORE the cargo read, must not run
    unless asked, must never run under --dry-run, and must stop the mission if it fails
    (the plan would otherwise be sized against space that was never freed)."""

    def _run(self, *, clear_surplus, dry_run=False, sell_result=None, capacity=4108,
             order=None):
        sell_result = sell_result if sell_result is not None else {
            "ok": True, "port": "Havana", "sold": ["Ebony"], "reason": "sold ['Ebony']"}

        def _clear(good):
            if order is not None:
                order.append("clear")
            return sell_result

        def _fleet():
            if order is not None:
                order.append("cargo-read")
            return {"cargo_capacity": capacity, "cargo_used": 0}

        with mock.patch("actions.village_check.read_village_barter_remote",
                        return_value=_check()), \
             mock.patch("actions.fleet_status.read_fleet_status", side_effect=_fleet), \
             mock.patch("brain.barter_mission_live.clear_surplus_at_current_port",
                        side_effect=_clear):
            return run_barter_command("barter Camas at Apache Village", dry_run=dry_run,
                                      clear_surplus=clear_surplus)

    def test_it_frees_the_hold_BEFORE_the_cargo_is_read(self):
        # Order is the whole point: read cargo first and the plan sizes against space we
        # are about to create, losing rounds for no reason.
        order = []
        # capacity=None stops at the plan step, so no mission runs in this test.
        res = self._run(clear_surplus=True, capacity=None, order=order)
        self.assertEqual(order, ["clear", "cargo-read"])
        self.assertEqual(res["cleared_surplus"]["sold"], ["Ebony"])

    def test_not_run_unless_asked(self):
        res = self._run(clear_surplus=False, capacity=None)
        self.assertEqual(res["step"], "plan")
        self.assertIsNone(res["cleared_surplus"])

    def test_a_failed_clear_stops_before_planning(self):
        res = self._run(clear_surplus=True,
                        sell_result={"ok": False, "reason": "could not reach Market"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "clear-surplus")
        self.assertIn("could not reach Market", res["reason"])

    def test_an_empty_hold_is_success_not_failure(self):
        res = self._run(clear_surplus=True, capacity=None,
                        sell_result={"ok": True, "sold": [], "reason": "nothing to sell"})
        self.assertEqual(res["cleared_surplus"]["reason"], "nothing to sell")
        self.assertEqual(res["step"], "plan")        # got past the clear, not blocked by it

    def test_dry_run_never_sells_anything(self):
        order = []
        res = self._run(clear_surplus=True, dry_run=True, order=order)
        self.assertNotIn("clear", order)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["cleared_surplus"])


class CommandToGraphTests(unittest.TestCase):
    """The seam between the parsed command and the sub-task graph: the tail the user
    typed must become the right nodes, and every node must reach a live executor."""

    def _run(self, text):
        from memory.barter_kb import BarterRecipe, RecipeInput
        recipe = BarterRecipe(good="Camas", villages=["Apache Village"],
                              inputs=[RecipeInput("Avocado", 130, ["Havana"]),
                                      RecipeInput("Cassava", 150, ["Havana"])],
                              output_per_round={"Friendly": 953})
        ran = []

        def _execs(_opp):
            kinds = ("gather", "sell_surplus", "supply_verify", "sail_to_village",
                     "barter", "sail_route", "sail_to_sell", "sell")
            return {k: (lambda t: (ran.append(t.id), {"ok": True})[1]) for k in kinds}

        with mock.patch("actions.village_check.read_village_barter_remote",
                        return_value=_check()), \
             mock.patch("actions.fleet_status.read_fleet_status",
                        return_value={"cargo_capacity": 4108, "cargo_used": 0}), \
             mock.patch("memory.barter_kb.load_recipe", return_value=recipe), \
             mock.patch("brain.barter_mission_live.catalogue_coords",
                        return_value={"Havana": (0, 0), "Edinburgh": (5, 0)}), \
             mock.patch("brain.barter_mission_live.current_position", return_value=(0, 0)), \
             mock.patch("brain.barter_mission_live.make_live_executors", side_effect=_execs):
            res = run_barter_command(text)
        return res, ran

    def test_sail_tail_runs_the_whole_line_and_sells_at_the_named_port(self):
        res, ran = self._run("barter Camas at Apache Village, and sail to Edinburgh")
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(ran, ["gather:Havana", "sell_surplus", "supply_verify",
                               "sail_to_village", "barter", "sail_to_sell", "sell"])

    def test_route_tail_swaps_the_leg_and_still_reaches_sell(self):
        res, ran = self._run(
            "barter Camas at Apache Village, then take the route jakarta to london")
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertIn("sail_route", ran)
        self.assertNotIn("sail_to_sell", ran)
        self.assertEqual(ran[-1], "sell")

    def test_no_tail_stops_at_the_village(self):
        res, ran = self._run("barter Camas at Apache Village")
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(ran[-1], "barter")
        self.assertNotIn("sell", ran)

    def test_gather_orders_come_from_the_live_check_not_the_kb_ratios(self):
        # KB says 130/150 per round; the check says the same here, but the ORDER must be
        # the cushioned total for the planned rounds, not the KB's raw ratio.
        res, _ = self._run("barter Camas at Apache Village, and sail to Edinburgh")
        self.assertEqual(res["task_plan"].purchases["Havana"],
                         {"Avocado": 449, "Cassava": 518})

    def test_an_unsourced_material_stops_before_sailing(self):
        from memory.barter_kb import BarterRecipe, RecipeInput
        recipe = BarterRecipe(good="Camas", inputs=[RecipeInput("Avocado", 130, [])])
        with mock.patch("actions.village_check.read_village_barter_remote",
                        return_value=_check()), \
             mock.patch("actions.fleet_status.read_fleet_status",
                        return_value={"cargo_capacity": 4108, "cargo_used": 0}), \
             mock.patch("memory.barter_kb.load_recipe", return_value=recipe), \
             mock.patch("brain.barter_mission_live.catalogue_coords", return_value={}), \
             mock.patch("brain.barter_mission_live.current_position", return_value=(0, 0)):
            res = run_barter_command("barter Camas at Apache Village, and sail to Edinburgh")
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "gather-plan")
        self.assertIn("Avocado", res["reason"])


if __name__ == "__main__":
    unittest.main()

class StartPositionTests(unittest.TestCase):
    """The gather ORDER is chosen by distance from where the fleet is now, so an invented
    origin reorders the whole voyage. Live 2026-08-21: the port name would not OCR,
    `current_position` returned (0,0), and the scheduler sent the fleet toward Atuona
    (5,948 units) when Masulipatnam was 294 away — with no supplies aboard."""

    def _run(self, port_reads, **kw):
        from memory.barter_kb import BarterRecipe, RecipeInput
        recipe = BarterRecipe(good="Camas",
                              inputs=[RecipeInput("Avocado", 130, ["Havana"]),
                                      RecipeInput("Cassava", 150, ["Havana"])])
        reads = iter(port_reads)
        with mock.patch("actions.village_check.read_village_barter_remote",
                        return_value=_check()), \
             mock.patch("actions.fleet_status.read_fleet_status",
                        return_value={"cargo_capacity": 4108, "cargo_used": 0}), \
             mock.patch("memory.barter_kb.load_recipe", return_value=recipe), \
             mock.patch("brain.barter_mission_live.catalogue_coords",
                        return_value={"Havana": (10, 10), "Diu": (6641, 2340)}), \
             mock.patch("capture.adb_capture.capture_screen", return_value=object()), \
             mock.patch("actions.sail_actions.where_am_i",
                        side_effect=lambda *a, **k: {"location": "port_overworld",
                                                     "port": next(reads, None)}), \
             mock.patch("brain.barter_mission_live.make_live_executors",
                        return_value={k: (lambda t: {"ok": True}) for k in
                                      ("gather", "sell_surplus", "supply_verify",
                                       "sail_to_village", "barter", "sail_route",
                                       "sail_to_sell", "sell")}), \
             mock.patch("time.sleep"):
            return run_barter_command("barter Camas at Apache Village", **kw)

    def test_an_unreadable_port_refuses_to_route_rather_than_guess(self):
        res = self._run([None, None, None])
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "gather-plan")
        self.assertIn("--from", res["reason"])

    def test_it_retries_before_giving_up(self):
        # A port always has a name; one bad frame shouldn't sink the mission.
        res = self._run([None, "Diu"])
        self.assertTrue(res["ok"], res.get("reason"))

    def test_an_explicit_start_port_is_used(self):
        res = self._run([None, None, None], from_port="Diu")
        self.assertTrue(res["ok"], res.get("reason"))

    def test_an_unknown_start_port_is_rejected(self):
        res = self._run(["Diu"], from_port="Nowhere")
        self.assertFalse(res["ok"])
        self.assertIn("not in the catalogue", res["reason"])
