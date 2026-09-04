"""Reading the hold is a work order, and standing in the wrong place is a routing problem.

This replaced a sixty-line ladder in `brain/barter_command.py`: read the fleet; if it came
back empty, sweep for blockers and read again; if it STILL came back empty and the reason
mentioned a missing hamburger, walk to the port overworld and read a third time. Every rung
had an owner already — the sweep is the dispatcher's `unblock`, the walk is its routing.
"""
import types
import unittest
from unittest import mock

from brain.activities.sea import AshoreActivity, ArriveAshore, ReadHold
from brain.dispatcher import BLOCKED, FINISHED, WORKING, Intent
from brain.intents import dispatch, to_intent


def _state(where, port="Lisboa"):
    return types.SimpleNamespace(state=where, location=where, port=port, frame=None)


class TheAshoreActivityReadsIt(unittest.TestCase):
    def test_it_reports_the_two_numbers(self):
        with mock.patch("actions.fleet_status.read_fleet_status",
                        return_value={"cargo_capacity": 4108, "cargo_used": 500}):
            res = AshoreActivity().work(ReadHold(), _state("port_overworld"))
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(res.observed["cargo_capacity"], 4108)
        self.assertEqual(res.observed["cargo_used"], 500)

    def test_it_gates_on_the_numbers_not_on_an_ok_flag(self):
        """A read that returned the capacity and the cargo has done its job whether or not it
        set a flag. Treating a missing flag as failure re-read a hold already read."""
        with mock.patch("actions.fleet_status.read_fleet_status",
                        return_value={"cargo_capacity": 4108, "cargo_used": 0}):  # no "ok"
            res = AshoreActivity().work(ReadHold(), _state("port_overworld"))
        self.assertEqual(res.status, FINISHED)

    def test_an_unreadable_hold_is_BLOCKED_and_carries_the_reason(self):
        with mock.patch("actions.fleet_status.read_fleet_status",
                        return_value={"cargo_capacity": None, "cargo_used": None,
                                      "reason": "no ☰ there"}):
            res = AshoreActivity().work(ReadHold(), _state("building:market"))
        self.assertEqual(res.status, BLOCKED)
        self.assertIn("☰", res.observed["reason"])

    def test_it_reads_ONCE_with_no_sweep_and_no_retry(self):
        """The ladder read up to three times inside one call. The dispatcher clears
        obstructions and re-perceives between ticks, so the retry happens by itself."""
        with mock.patch("actions.fleet_status.read_fleet_status",
                        return_value={"cargo_capacity": None, "cargo_used": None}) as read:
            AshoreActivity().work(ReadHold(), _state("port_overworld"))
        self.assertEqual(read.call_count, 1)

    def test_arriving_ashore_still_works(self):
        res = AshoreActivity().work(ArriveAshore(), _state("port_overworld"))
        self.assertEqual(res.status, FINISHED)


class StandingInTheWrongPlaceIsRouting(unittest.TestCase):
    def test_the_overworld_needs_no_transition(self):
        self.assertIsNone(to_intent(ReadHold(), _state("port_overworld")))

    def test_a_building_yields_an_exit(self):
        intent = to_intent(ReadHold(), _state("building:market"))
        self.assertEqual(intent.name, "EXIT_BUILDING")
        self.assertEqual(intent.extras["from"], "building:market")

    def test_a_sub_menu_yields_an_exit_too(self):
        self.assertEqual(to_intent(ReadHold(), _state("sub_menu:barter")).name, "EXIT_BUILDING")

    def test_inside_is_a_RULE_not_a_list_of_buildings(self):
        """Enumerating the buildings would be wrong the day one is added. The naming carries
        the fact: a prefixed state is somewhere the hamburger is not."""
        self.assertEqual(to_intent(ReadHold(), _state("building:a_shop_added_tomorrow")).name,
                         "EXIT_BUILDING")

    def test_at_sea_there_is_nothing_to_press(self):
        """Mid-voyage the hold is not reachable and no tap changes that; the fleet arrives
        on its own. A transition here would press Back at sea."""
        self.assertIsNone(to_intent(ReadHold(), _state("sea")))

    def test_the_exit_is_ONE_back_not_a_walk(self):
        """`reorient_to` walks a whole FSM path inside a single dispatch — the sub-loop this
        architecture removes. One press; the next tick sees where it landed."""
        with mock.patch("actions.adb_actions.press_back") as back:
            dispatch(Intent("EXIT_BUILDING", {"from": "building:market"}))
        self.assertEqual(back.call_count, 1)


class TheHoldReadsAtSeaToo(unittest.TestCase):
    """It was served only from ashore, so a `ReadHold` while the fleet was at sea found no
    activity and no transition and simply stalled — the same missing-route shape as the world
    map before CLOSE_WORLD_MAP.

    Checked against the real game with the fleet in open water: cargo 639/4952, supply 11.6d.
    The hamburger is there; only the routing said otherwise.
    """

    def test_the_sea_serves_it(self):
        from brain.activities.sea import ReadHold, SeaActivity

        self.assertIn(ReadHold, SeaActivity.GOALS)

    def test_it_reads_the_same_numbers_at_sea(self):
        from brain.activities.sea import ReadHold, SeaActivity

        with mock.patch("actions.fleet_status.read_fleet_status",
                        return_value={"cargo_capacity": 4952, "cargo_used": 639}):
            res = SeaActivity().work(ReadHold(), _state("sea", port=None))
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(res.observed["cargo_used"], 639)

    def test_the_sea_still_watches_a_voyage(self):
        """Serving ReadHold must not disturb what the sea is FOR."""
        from brain.activities.sea import ArriveAshore, SeaActivity

        act = SeaActivity(hud_fn=lambda: {"supply_days": 9, "eta_days": 4},
                          speed_fn=lambda: 11.0, checkback_fn=lambda d: 60.0)
        res = act.work(ArriveAshore("trip"), _state("sea"))
        self.assertEqual(res.status, WORKING)
        self.assertIs(res.observed["moving"], True)

    def test_one_reader_serves_both(self):
        """Ashore and at sea read the hold the same way; two copies would drift."""
        import inspect

        from brain.activities import sea

        self.assertTrue(hasattr(sea, "read_the_hold"))
        self.assertIn("read_the_hold", inspect.getsource(sea.AshoreActivity.work))
        self.assertIn("read_the_hold", inspect.getsource(sea.SeaActivity.work))


class TheDispatcherPicksWithoutAmbiguity(unittest.TestCase):
    def test_each_state_has_exactly_one_server_for_a_hold_read(self):
        """Both activities declare ReadHold, which is only safe because they serve disjoint
        states. If that ever overlaps, the winner would be registration order."""
        from brain.activities.sea import AshoreActivity, SeaActivity

        self.assertFalse(set(SeaActivity.SERVES) & set(AshoreActivity.SERVES))
