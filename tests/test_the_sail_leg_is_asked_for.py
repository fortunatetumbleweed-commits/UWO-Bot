"""A sail leg is a sequence of work orders, not an eight-phase state machine.

`SailToGoal` walked to the harbour, opened the world map, picked the destination and then
POLLED FOR ARRIVAL in its own SAILING phase — a second sea watch that did not perceive through
the dispatcher, so it received neither the interruptor pass that dismisses a daily-news popup
nor `IdleLockActivity`. That is the exact failure `_await_route_arrival` was deleted for,
still live on every port and village leg.
"""
import types
import unittest

from brain.activities.harbor import Depart
from brain.activities.sea import ArriveAshore
from brain.activities.world_map import ChooseDestination
from brain.dispatcher import (ActivityResult, BLOCKED, FINISHED, UNRECOGNISED,
                              WORKING)
from brain.intents import to_intent
from brain.sail_runner import ARRIVED, FAILED, SailRunner


def _course_set(destination="Amsterdam", **kw):
    """A runner that has already chosen its destination — the step before departing.

    The course comes FIRST now: Supply Departure sails toward a destination already chosen,
    and without one the ship simply leaves (live 2026-08-29: destination None, ETA None,
    speed 0.0). Tests about the DEPARTURE start from here so they are not also re-testing
    the ordering.
    """
    r = SailRunner(destination=destination, **kw)
    r.next_goal(None, _st("port_overworld"))                       # -> ChooseDestination
    r.next_goal(ActivityResult(FINISHED, {}), _st("port_overworld"))  # course committed
    return r


def _st(where, port=None):
    return types.SimpleNamespace(state=where, location=where, port=port)


class OneLegEndToEnd(unittest.TestCase):
    def test_port_to_village(self):
        """Course first, then the departure — Supply Departure sails toward a destination
        that has already been chosen."""
        r = SailRunner(destination="Svear", kind="village")
        self.assertIsInstance(r.next_goal(None, _st("port_overworld", "Lisboa")),
                              ChooseDestination)
        self.assertIsInstance(r.next_goal(ActivityResult(FINISHED, {}),
                                          _st("port_overworld", "Lisboa")), Depart)
        self.assertIsInstance(r.next_goal(ActivityResult(WORKING, {}),
                                          _st("building:harbor")), Depart)
        self.assertIsInstance(r.next_goal(ActivityResult(FINISHED, {}), _st("sea")),
                              ArriveAshore)
        self.assertIsNone(r.next_goal(ActivityResult(FINISHED, {"port": "Svear"}),
                                      _st("village", "Svear")))
        self.assertEqual(r.status, ARRIVED)
        self.assertEqual(r.port, "Svear")

    def test_already_at_sea_skips_the_harbour(self):
        """Being at sea IS the departure, whatever produced it. Read from the world, not
        remembered from a Depart that was never asked for."""
        r = SailRunner(destination="Lisboa")
        self.assertIsInstance(r.next_goal(None, _st("sea")), ChooseDestination)

    def test_a_ship_at_sea_with_no_course_is_given_one(self):
        """The live failure this ordering fixes: the fleet left London and sat there — HUD
        destination None, ETA None, speed 0.0. Put to sea with nowhere to go."""
        r = SailRunner(destination="Amsterdam")
        goal = r.next_goal(None, _st("sea"))
        self.assertIsInstance(goal, ChooseDestination)
        self.assertEqual(goal.where, "Amsterdam")

    def test_committing_a_course_that_sails_skips_the_harbour(self):
        """If committing on the map sails immediately, the next tick reads `sea` and the
        harbour step is skipped — by OBSERVATION, not by assuming which way the game works."""
        r = SailRunner(destination="Amsterdam")
        r.next_goal(None, _st("port_overworld"))
        after = r.next_goal(ActivityResult(FINISHED, {}), _st("sea"))
        self.assertIsInstance(after, ArriveAshore)

    def test_without_resupply_the_course_is_the_departure(self):
        r = SailRunner(destination="Lisboa", resupply=False)
        self.assertIsInstance(r.next_goal(None, _st("port_overworld")), ChooseDestination)


class TheGoalPersistsUntilThePhaseEnds(unittest.TestCase):
    """Returning None for an unrecognised screen ended the leg — None means FINISHED to the
    loop — so standing in the harbour, where the Depart is actually worked, stopped the task.
    It is also what makes routing possible: `to_intent` needs a goal to turn into a transition.
    """

    def test_a_building_does_not_end_the_leg(self):
        r = _course_set("Lisboa")
        self.assertIsNotNone(r.next_goal(ActivityResult(WORKING, {}), _st("building:market")))

    def test_the_goal_routes_the_way_out(self):
        """Whatever phase the leg is in, its goal produces a transition that leaves the
        building. Which transition depends on the phase, and that is the point: the goal is
        what the dispatcher routes on."""
        no_course = SailRunner(destination="Lisboa")
        goal = no_course.next_goal(None, _st("building:market"))
        # NOT FROM ANYWHERE — the globe is on the OVERWORLD. This asserted that the map
        # opens from inside a building, and `tap_world_map_control` says otherwise in as many
        # words: "no world-map control on 'sub_menu'" (live 2026-08-29). The dispatch code
        # agrees in its own comment — "no waking, no exiting a building... exiting [belongs]
        # to the dispatcher's routing" — and that routing step had never been written, so
        # OPEN_WORLD_MAP went out from the market's Sell submenu on every tick until the
        # no-progress guard stopped the run.
        self.assertEqual(to_intent(goal, _st("building:market")).name, "EXIT_BUILDING",
                         "the globe is on the overworld — step out before reaching for it")
        self.assertEqual(to_intent(goal, _st("port_overworld")).name, "OPEN_WORLD_MAP",
                         "and from the overworld it opens directly")

        ready = _course_set("Lisboa")
        goal = ready.next_goal(None, _st("building:market"))
        self.assertEqual(to_intent(goal, _st("building:market")).name, "ENTER_BUILDING",
                         "with a course set, the leg wants the harbour")

    def test_time_in_the_harbour_is_not_a_new_attempt(self):
        """The bound counts attempts to put to sea, not ticks spent doing it."""
        r = _course_set("Lisboa")
        for _ in range(6):
            g = r.next_goal(ActivityResult(WORKING, {}), _st("building:harbor"))
        self.assertIsInstance(g, Depart)
        self.assertNotEqual(r.status, FAILED)


class ARESULTMEANSNOTHINGWITHOUTITSGOAL(unittest.TestCase):
    def test_a_set_course_does_not_mean_arrival(self):
        """FINISHED means something different for each goal, and reading it without the goal
        ends the leg at the first success of any kind."""
        r = SailRunner(destination="Lisboa")
        r.next_goal(None, _st("sea"))                       # -> ChooseDestination
        after = r.next_goal(ActivityResult(FINISHED, {}), _st("sea"))
        self.assertIsInstance(after, ArriveAshore)
        self.assertNotEqual(r.status, ARRIVED)

    def test_not_moving_sets_the_course_again(self):
        """The ETA stopped falling, so the course never took. Watching it longer cannot help."""
        r = SailRunner(destination="Lisboa")
        r.next_goal(None, _st("sea"))
        r.next_goal(ActivityResult(FINISHED, {}), _st("sea"))
        again = r.next_goal(ActivityResult(BLOCKED, {"moving": False}), _st("sea"))
        self.assertIsInstance(again, ChooseDestination)

    def test_it_stops_rather_than_setting_a_course_forever(self):
        r = SailRunner(destination="Lisboa")
        for _ in range(8):
            g = r.next_goal(ActivityResult(BLOCKED, {"moving": False}), _st("sea"))
            if g is None:
                break
            r.next_goal(ActivityResult(FINISHED, {}), _st("sea"))
        self.assertEqual(r.status, FAILED)
        self.assertIn("could not set a course", r.reason)


class TheDispatcherRoutesEveryStep(unittest.TestCase):
    def test_depart_reaches_the_harbour(self):
        intent = to_intent(Depart("Lisboa"), _st("port_overworld"))
        self.assertEqual(intent.name, "ENTER_BUILDING")
        self.assertEqual(intent.extras["name"], "harbor")

    def test_inside_the_harbour_there_is_nothing_to_dispatch(self):
        self.assertIsNone(to_intent(Depart("Lisboa"), _st("building:harbor")))

    def test_a_course_opens_the_world_map_from_the_overworlds(self):
        """NOT "from anywhere" — that was the name of this test, and it was wrong twice.

        The globe lives on the overworlds: the port's own globe and the sea's minimap. It is
        not in a building, not in a sub-menu, and NOT IN A VILLAGE — a village screen is
        chromed, with a back arrow and the Explore/Gifting/Loot/Recruit/Barter menu, and no
        map control at all. `tap_world_map_control` says so in as many words: "no world-map
        control on 'village'".

        Live 2026-08-29 the barter finished at Svear with the Birch Tree aboard, the mission
        asked for Lisboa, and OPEN_WORLD_MAP went out at the village every tick until the run
        stopped one leg from done. Sailing is not the village's work: its order is finished,
        so it leaves, and the dispatcher asks what is next from wherever that lands.
        """
        for where in ("port_overworld", "sea"):
            intent = to_intent(ChooseDestination("Svear", "village"), _st(where))
            self.assertEqual(intent.name, "OPEN_WORLD_MAP", where)

        for where in ("village", "building:market", "sub_menu:sell"):
            intent = to_intent(ChooseDestination("Svear", "village"), _st(where))
            self.assertEqual(intent.name, "EXIT_BUILDING", where)


class AlreadyThereMeansDoNotSail(unittest.TestCase):
    """The scheduler can pick the port the fleet is standing in, and a leg that departs
    anyway crosses the map to arrive where it started. Live 2026-08-17 at Malé it did exactly
    that, because the port name read None and the check was skipped."""

    def _arrives_without_sailing(self, port, destination):
        r = SailRunner(destination=destination)
        goal = r.next_goal(None, _st("port_overworld", port))
        return goal is None and r.status == ARRIVED

    def test_the_same_port_is_not_sailed_to(self):
        self.assertTrue(self._arrives_without_sailing("Lisboa", "Lisboa"))

    def test_accents_do_not_hide_it(self):
        """The map panel reads names without accents while the catalogue stores them with, so
        a strict comparison never matches the one port it most needs to."""
        self.assertTrue(self._arrives_without_sailing("Malé", "Male"))
        self.assertTrue(self._arrives_without_sailing("Male", "Malé"))

    def test_a_different_port_still_sails(self):
        self.assertFalse(self._arrives_without_sailing("Lisboa", "Svear"))

    def test_an_unreadable_port_sails_rather_than_guesses(self):
        """A village's interior reports no name at all, so a missing port must not read as a
        match — it would end every leg to a village at the moment it began."""
        self.assertFalse(self._arrives_without_sailing(None, "Svear"))


class OnlyATerminalResultEndsAnAttempt(unittest.TestCase):
    """WORKING is an activity part-way through; UNRECOGNISED is the dispatcher ROUTING toward
    one. Neither is a failed attempt.

    Live 2026-08-29: `Depart` at a port_overworld is served by nobody — AshoreActivity is
    there but serves ArriveAshore and ReadHold — so every tick of the walk to the harbour
    reported "no activity for state 'port_overworld'". Clearing the pending goal on that made
    each tick a fresh departure attempt: the two-attempt bound was spent in two ticks and the
    leg gave up while the character was still walking.
    """

    def _routing(self):
        return ActivityResult(UNRECOGNISED, {"state": "port_overworld"},
                              detail="no activity for 'port_overworld'")

    def test_routing_ticks_are_not_departure_attempts(self):
        r = _course_set("Amsterdam")
        for _ in range(8):
            goal = r.next_goal(self._routing(), _st("port_overworld"))
        self.assertIsInstance(goal, Depart)
        self.assertNotEqual(r.status, FAILED)

    def test_routing_ticks_are_not_course_attempts_either(self):
        r = SailRunner(destination="Lisboa")
        r.next_goal(None, _st("sea"))
        for _ in range(8):
            goal = r.next_goal(self._routing(), _st("sea"))
        self.assertIsInstance(goal, ChooseDestination)
        self.assertNotEqual(r.status, FAILED)

    def test_a_real_refusal_still_counts(self):
        """BLOCKED is terminal — the bound must still bite when the harbour actually says no."""
        r = _course_set("Amsterdam")
        for _ in range(6):
            goal = r.next_goal(ActivityResult(BLOCKED, {}, detail="the harbour refused"),
                               _st("port_overworld"))
            if goal is None:
                break
        self.assertEqual(r.status, FAILED)
        self.assertIn("could not put to sea", r.reason)


class AConfirmedDepartureIsNotRepeated(unittest.TestCase):
    """The harbour says it tapped Supply Departure and the ship left; what follows is the
    departure cinematic, not the sea. Asking again during it repeats an action whose effect
    has not landed — the dispatcher refuses that for an intent, and this refuses it for an
    activity's action.

    Live 2026-08-29: "departed for Amsterdam via supply_depart", then ONE tick still read as
    the harbour, so it asked again, found no departure button (there is none once you have
    sailed), and that doomed second attempt exhausted the bound. The leg failed on a
    departure that had worked.
    """

    def test_the_cinematic_does_not_provoke_a_second_departure(self):
        r = _course_set("Amsterdam")
        r.next_goal(None, _st("port_overworld"))                    # -> Depart
        after = r.next_goal(ActivityResult(FINISHED, {}), _st("building:harbor"))
        self.assertNotIsInstance(after, Depart, "it asked to depart a second time")
        for where in ("transient", "pending", "sea_cinematic"):
            self.assertNotIsInstance(r.next_goal(None, _st(where)), Depart, where)

    def test_a_refused_departure_is_still_retried(self):
        """BLOCKED is the harbour saying no, which is a real attempt spent."""
        r = _course_set("Amsterdam")
        r.next_goal(None, _st("port_overworld"))                    # -> Depart
        again = r.next_goal(ActivityResult(BLOCKED, {}, detail="crew short"),
                            _st("building:harbor"))
        self.assertIsInstance(again, Depart)


class ArrivedSomewhereIsNotArrivedThere(unittest.TestCase):
    """Taking a confirmed departure as committed is a BELIEF. A departure that silently did
    not take would leave the fleet ashore at the origin with the runner reporting arrival."""

    def _leg(self, runner_port):
        from unittest import mock

        import brain.barter_mission_live as bml

        runner = types.SimpleNamespace(status="arrived", port=runner_port, reason="")
        with mock.patch("brain.run_goal.run_task", return_value=runner):
            return bml._sail_to("Amsterdam")

    def test_the_right_port_is_an_arrival(self):
        self.assertTrue(self._leg("Amsterdam")["ok"])

    def test_the_origin_port_is_not(self):
        res = self._leg("London")
        self.assertFalse(res["ok"])
        self.assertIn("did not take", res["reason"])

    def test_an_unreadable_port_is_not_second_guessed(self):
        """A port that could not be read is not evidence of being in the wrong one."""
        self.assertTrue(self._leg(None)["ok"])
