"""A mission is a sequence of work orders, not a graph of executors.

`run_mission` walked the same graph but EXECUTED each leg: `executors[kind](task)`, each one
running a whole leg to completion with its own loops inside. The dispatcher never saw the
mission — it was handed "sail to Amsterdam" by an executor that had already decided to gather
there, and the reason why (Iron 822, at Amsterdam, because the plan says so) lived a layer
above anything it could consult.

    "it should be still at gathering part, and it should include what is on the next list at
     where, the dispatcher then dispatches this"  — user, 2026-08-29
"""
import types
import unittest

from brain.activities.market import FreeHold, Hold, SellHold, TrimHold
from brain.activities.village import Barter
from brain.activities.world_map import ChooseDestination
from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, UNRECOGNISED, WORKING
from brain.mission import SubTask
from brain.mission_runner import DONE, FAILED, RUNNING, MissionRunner

COORDS = {"London": (0, 0), "Amsterdam": (10, 10), "Barcelona": (200, 200),
          "Svear Village": (50, 300), "Lisboa": (150, 20)}


def _st(where="port_overworld", port="London"):
    return types.SimpleNamespace(state=where, location=where, port=port)


def _ok(**observed):
    return ActivityResult(FINISHED, observed)


def _runner(graph, **kw):
    return MissionRunner(subtasks=graph, coords=COORDS, good="Birch Tree",
                         village="Svear Village", **kw)


def _gather(port, orders, deps=()):
    return SubTask(f"gather:{port}", "gather", port,
                   params={"port": port, "orders": dict(orders)}, deps=deps)


class TheWorkOrderNamesWhatAndWhere(unittest.TestCase):
    def test_a_gather_becomes_a_voyage_and_then_a_purchase(self):
        """The whole point: the dispatcher is told to hold Iron, not to sail somewhere.

        The results are what each goal really returns — a voyage reports WORKING while the
        sea is still the sea, and FINISHED only once the fleet is ashore. Feeding FINISHED
        throughout says "arrived" on every tick, which is a test writing its own answer.
        """
        r = _runner([_gather("Amsterdam", {"Iron": 822})])
        seen = []
        ashore = _st(port="London")
        seen.append(r.next_goal(None, ashore))                     # -> course
        seen.append(r.next_goal(_ok(), ashore))                    # -> depart
        seen.append(r.next_goal(_ok(), ashore))                    # -> watch the voyage
        seen.append(r.next_goal(ActivityResult(WORKING, {"moving": True}),
                                _st("sea", None)))                 # still under way
        seen.append(r.next_goal(_ok(port="Amsterdam"), _st(port="Amsterdam")))

        self.assertIsInstance(seen[0], ChooseDestination)
        self.assertIsInstance(seen[2], type(seen[3]), "the voyage is still the voyage")
        self.assertIsInstance(seen[-1], Hold)
        self.assertEqual(seen[-1].orders, {"Iron": 822})

    def test_the_purchase_carries_the_whole_order(self):
        r = _runner([_gather("Amsterdam", {"Iron": 822, "Candle": 934})])
        ashore = _st(port="London")
        r.next_goal(None, ashore)
        r.next_goal(_ok(), ashore)
        r.next_goal(_ok(), ashore)
        r.next_goal(ActivityResult(WORKING, {}), _st("sea", None))
        goal = r.next_goal(_ok(port="Amsterdam"), _st(port="Amsterdam"))
        self.assertIsInstance(goal, Hold)
        self.assertEqual(goal.orders, {"Iron": 822, "Candle": 934})


class ItWalksTheGraph(unittest.TestCase):
    def test_dependencies_are_respected(self):
        graph = [SubTask("trim", "sell_surplus", "", params={"clear": True, "keep_qty": {}}),
                 _gather("Amsterdam", {"Iron": 1}, deps=("trim",))]
        r = _runner(graph)
        first = r.next_goal(None, _st())
        self.assertIsInstance(first, FreeHold, "the gather ran before its dependency")

    def test_a_finished_leg_is_recorded_and_the_next_begins(self):
        graph = [SubTask("trim", "sell_surplus", "", params={"clear": True, "keep_qty": {}}),
                 _gather("Amsterdam", {"Iron": 1}, deps=("trim",))]
        r = _runner(graph)
        r.next_goal(None, _st())
        nxt = r.next_goal(_ok(), _st())
        self.assertEqual(r.completed, ["trim"])
        self.assertIsInstance(nxt, ChooseDestination)

    def test_the_nearest_runnable_leg_goes_first(self):
        """Re-decided from where the fleet IS, not from the last leg's destination."""
        r = _runner([_gather("Barcelona", {"Iron": 1}), _gather("Amsterdam", {"Candle": 1})])
        goal = r.next_goal(None, _st(port="London"))
        self.assertEqual(goal.where, "Amsterdam", "London is nearer Amsterdam")

    def test_an_unknown_position_does_not_rank(self):
        """A port that is not in the catalogue is unranked, not infinitely far."""
        r = _runner([_gather("Amsterdam", {"Iron": 1})])
        self.assertIsNotNone(r.next_goal(None, _st(port="Nowhere")))

    def test_every_leg_done_ends_the_mission(self):
        r = _runner([SubTask("trim", "sell_surplus", "",
                             params={"clear": True, "keep_qty": {}})])
        r.next_goal(None, _st())
        self.assertIsNone(r.next_goal(_ok(), _st()))
        self.assertEqual(r.status, DONE)

    def test_a_deadlock_is_named_not_looped_on(self):
        r = _runner([_gather("Amsterdam", {"Iron": 1}, deps=("nobody",))])
        self.assertIsNone(r.next_goal(None, _st()))
        self.assertEqual(r.status, FAILED)
        self.assertIn("deadlock", r.reason)


class EachLegKindBecomesItsOwnOrder(unittest.TestCase):
    def _first_goal(self, leg):
        r = _runner([leg])
        return r.next_goal(None, _st())

    def test_a_clear_is_a_free_hold(self):
        self.assertIsInstance(self._first_goal(
            SubTask("t", "sell_surplus", "", params={"clear": True, "keep_qty": {}})),
            FreeHold)

    def test_a_trim_is_a_trim_hold(self):
        goal = self._first_goal(
            SubTask("t", "sell_surplus", "", params={"keep_qty": {"Iron": 100}}))
        self.assertIsInstance(goal, TrimHold)
        self.assertEqual(goal.keep_qty, {"Iron": 100})

    def test_a_barter_is_a_barter(self):
        goal = self._first_goal(SubTask("b", "barter", "Svear Village",
                                        params={"good": "Birch Tree", "rounds": 7}))
        self.assertIsInstance(goal, Barter)
        self.assertEqual(goal.good, "Birch Tree")

    def test_a_sell_is_a_sell_hold(self):
        self.assertIsInstance(self._first_goal(SubTask("s", "sell", "Lisboa", params={})),
                              SellHold)

    def test_a_village_leg_sails_to_the_village_tab(self):
        goal = self._first_goal(SubTask("v", "sail_to_village", "Svear Village",
                                        params={"village": "Svear Village"}))
        self.assertIsInstance(goal, ChooseDestination)
        self.assertEqual(goal.kind, "village", "villages are on the Explore tab")

    def test_a_route_leg_sails_the_route_tab(self):
        goal = self._first_goal(SubTask("r", "sail_route", "Lisboa",
                                        params={"route": "san to london"}))
        self.assertEqual(goal.kind, "route")

    def test_an_unknown_kind_is_reported_not_invented(self):
        r = _runner([SubTask("x", "dance", "", params={})])
        self.assertIsNone(r.next_goal(None, _st()))
        self.assertEqual(r.status, FAILED)
        self.assertIn("no work order", r.reason)


class ItReportsRatherThanRetries(unittest.TestCase):
    def test_a_blocked_leg_stops_the_mission_with_a_reason(self):
        """What to do about a port that will not sell needs the plan, which is not here."""
        r = _runner([SubTask("t", "sell_surplus", "", params={"clear": True, "keep_qty": {}})])
        r.next_goal(None, _st())
        self.assertIsNone(r.next_goal(ActivityResult(BLOCKED, {}, detail="shelves full"),
                                      _st()))
        self.assertEqual(r.status, FAILED)
        self.assertIn("shelves full", r.reason)

    def test_working_and_routing_do_not_end_a_leg(self):
        r = _runner([SubTask("t", "sell_surplus", "", params={"clear": True, "keep_qty": {}})])
        first = r.next_goal(None, _st())
        for status in (WORKING, UNRECOGNISED, WORKING):
            again = r.next_goal(ActivityResult(status, {}), _st())
            self.assertIs(type(again), type(first))
        self.assertEqual(r.status, RUNNING)


class ItDoesNotPerceiveOrDrive(unittest.TestCase):
    def test_the_module_imports_no_ui(self):
        import ast
        import pathlib

        from brain.layers import is_ui_module

        src = pathlib.Path("brain/mission_runner.py").read_text()
        for node in ast.walk(ast.parse(src)):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            self.assertFalse(mod and is_ui_module(mod), f"mission_runner imports UI: {mod}")


class ALegThatCannotRunHereIsSaidSo(unittest.TestCase):
    """A market leg at sea is not a failure of the market — there is no market.

    The graph gives these legs no location because they run wherever the fleet already is,
    which assumed the fleet was at a port. Live 2026-08-29 it was adrift, the trim was picked
    anyway, and `tap_building_entry` refused with "Not the building list (no tab icons)". It
    was right to refuse; nothing had told it we were nowhere near a market.
    """

    def test_a_REQUIRED_market_leg_at_sea_stops_the_mission(self):
        """The trim before the VILLAGE is the required one: the barter output has to fit
        somewhere, and a hold that is full when the fleet arrives cannot take it."""
        r = _runner([SubTask("sell_surplus", "sell_surplus", "",
                             params={"keep_qty": {"Iron": 100}})])
        self.assertIsNone(r.next_goal(None, _st("sea", None)))
        self.assertEqual(r.status, FAILED)
        # THE WORDING GENERALISED, THE MEANING DID NOT. It used to say "no market out
        # here", which only reads correctly at sea — a VILLAGE has no market either, and on
        # 2026-08-30 a run stalled dispatching ENTER_BUILDING at one. Assert the parts that
        # carry meaning: which leg, and where it cannot run.
        self.assertIn("market", r.reason)
        self.assertIn("sea", r.reason)
        self.assertIn("sell_surplus", r.reason)

    def test_an_OPTIONAL_market_leg_at_sea_is_skipped(self):
        """The pre-gather clear frees space before buying — worth doing, not worth stranding
        a mission over, and the gather port has a market of its own."""
        graph = [SubTask("trim_before_gather", "sell_surplus", "",
                         params={"clear": True, "keep_qty": {}}, optional=True),
                 _gather("Amsterdam", {"Iron": 822}, deps=("trim_before_gather",))]
        r = _runner(graph)
        goal = r.next_goal(None, _st("sea", None))
        self.assertIsInstance(goal, ChooseDestination, "it should move on to the gather")
        self.assertEqual(r.status, RUNNING)

    def test_a_skipped_leg_does_not_block_what_depends_on_it(self):
        graph = [SubTask("trim_before_gather", "sell_surplus", "",
                         params={"clear": True, "keep_qty": {}}, optional=True),
                 _gather("Amsterdam", {"Iron": 1}, deps=("trim_before_gather",))]
        r = _runner(graph)
        r.next_goal(None, _st("sea", None))
        self.assertTrue(graph[0].done, "the dependency must be satisfied, not merely passed")

    def test_an_optional_leg_ashore_is_still_run(self):
        """Optional means "skip when it CANNOT run here", not "skip when convenient"."""
        r = _runner([SubTask("trim_before_gather", "sell_surplus", "",
                             params={"clear": True, "keep_qty": {}}, optional=True)])
        self.assertIsInstance(r.next_goal(None, _st(port="London")), FreeHold)

    def test_the_same_leg_runs_perfectly_well_ashore(self):
        r = _runner([SubTask("trim", "sell_surplus", "",
                             params={"clear": True, "keep_qty": {}})])
        self.assertIsInstance(r.next_goal(None, _st(port="London")), FreeHold)

    def test_a_voyage_is_not_refused_at_sea(self):
        """Only the legs that happen inside a building need the fleet ashore."""
        r = _runner([_gather("Amsterdam", {"Iron": 1})])
        self.assertIsNotNone(r.next_goal(None, _st("sea", None)))


class AStallNamesItsLeg(unittest.TestCase):
    """`run_task` gives up after enough ticks with no progress, and the runner is still
    RUNNING then — so its reason is empty and the caller reported only "the mission
    stopped"."""

    def test_it_says_which_leg_it_was_on(self):
        r = _runner([_gather("Amsterdam", {"Iron": 1})])
        r.next_goal(None, _st(port="London"))
        self.assertIn("gather:Amsterdam", r.where_it_stopped())

    def test_a_real_reason_is_preferred_to_the_leg_name(self):
        r = _runner([SubTask("trim", "sell_surplus", "",
                             params={"clear": True, "keep_qty": {}})])
        r.next_goal(None, _st("sea", None))
        self.assertIn("market", r.where_it_stopped())
        self.assertIn("sea", r.where_it_stopped())

    def test_stopping_before_anything_started_says_that(self):
        self.assertIn("before any leg", _runner([]).where_it_stopped())


class ALegIsOneOrMoreSteps(unittest.TestCase):
    """`_SailThenLeg` nested a sequencer inside a sequencer to say "sail there, then buy" —
    two levels of the same walking, one of them hidden. A gather is simply [voyage, purchase],
    and the mission advances through a leg's steps exactly as it advances through legs.

    The voyage step does not sail. The sea activity does: SailRunner asks for a course, a
    departure and then ArriveAshore, and it is the sea that watches the ETA.
    """

    def test_a_gather_is_a_voyage_then_a_purchase(self):
        r = _runner([_gather("Amsterdam", {"Iron": 822})])
        ashore = _st(port="London")
        goals = [r.next_goal(None, ashore),
                 r.next_goal(_ok(), ashore),
                 r.next_goal(_ok(), ashore),
                 r.next_goal(ActivityResult(WORKING, {}), _st("sea", None)),
                 r.next_goal(_ok(port="Amsterdam"), _st(port="Amsterdam"))]
        self.assertIsInstance(goals[0], ChooseDestination)
        self.assertIsInstance(goals[-1], Hold)
        self.assertIsNone(r.next_goal(_ok(), _st(port="Amsterdam")))
        self.assertEqual(r.status, DONE)
        self.assertEqual(r.completed, ["gather:Amsterdam"])

    def test_a_failed_voyage_does_not_reach_the_purchase(self):
        """A leg's later steps are not attempted when an earlier one gives up — buying at a
        port the fleet never reached is worse than not buying."""
        r = _runner([_gather("Amsterdam", {"Iron": 822})])
        ashore = _st(port="London")
        r.next_goal(None, ashore)
        for _ in range(8):
            goal = r.next_goal(ActivityResult(BLOCKED, {}, detail="no map"), ashore)
            if goal is None:
                break
        self.assertEqual(r.status, FAILED)
        self.assertEqual(r.completed, [])

    def test_a_single_step_leg_still_works(self):
        r = _runner([SubTask("b", "barter", "Svear Village",
                             params={"good": "Birch Tree", "rounds": 7})])
        self.assertIsInstance(r.next_goal(None, _st()), Barter)
        self.assertIsNone(r.next_goal(_ok(), _st()))
        self.assertEqual(r.status, DONE)

    def test_the_wrapper_is_gone(self):
        """One level of walking, not two."""
        import brain.mission_runner as mr

        self.assertFalse(hasattr(mr, "_SailThenLeg"))
        self.assertTrue(hasattr(mr, "_VoyageStep"))


class AStepMayFinishTheMomentItIsAsked(unittest.TestCase):
    """A voyage to the port the fleet is already standing in is done immediately.

    Advancing was written as a ladder, so that case returned None on the first call and the
    mission read as finished before it started — the fake world reported "stalled on
    gather:Havana" while standing in Havana.
    """

    def test_a_gather_at_the_port_we_are_in_goes_straight_to_buying(self):
        r = _runner([_gather("London", {"Iron": 822})])
        goal = r.next_goal(None, _st(port="London"))
        self.assertIsInstance(goal, Hold, "the voyage was already over")
        self.assertEqual(goal.orders, {"Iron": 822})

    def test_several_immediate_steps_still_advance(self):
        graph = [SubTask("trim", "sell_surplus", "",
                         params={"clear": True, "keep_qty": {}}, optional=True),
                 _gather("London", {"Iron": 1}, deps=("trim",))]
        r = _runner(graph)
        # at sea the trim is skipped; the gather's voyage is not immediate from here
        self.assertIsNotNone(r.next_goal(None, _st("sea", None)))

    def test_it_does_not_spin_forever(self):
        """A guard, not a policy: if every step keeps finishing without asking for anything,
        say so rather than loop."""
        r = _runner([_gather("London", {"Iron": 1})])
        r.next_goal(None, _st(port="London"))
        self.assertNotEqual(r.status, FAILED)
