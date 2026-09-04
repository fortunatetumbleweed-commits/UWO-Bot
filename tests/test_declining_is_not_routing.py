"""A world that cannot start a transition may still be able to REACH one that can.

`CAN_START` alone can only refuse, and refusing is not an answer. Live 2026-09-01 the
capability check — added the same day — turned a market order inside the HARBOUR into a hard
stop:

    building:harbor      to_intent -> ENTER_BUILDING   affords ['EXIT_BUILDING']  -> REFUSED

which is correct as far as it goes: a harbour has no building list. But the actual answer is
two ordinary hops, out to the overworld and in again, and nothing could say so because
`CAN_START` declares what a world can BEGIN and never where that LANDS.

`LEADS_TO` adds the second half, and the pair makes a route computable. Only the FIRST hop is
taken: the world after it is observed, not assumed, so the next look says whether it landed
and the route is recomputed from there (CLAUDE.md #2).

TWO PLACES IT DELIBERATELY REFUSES TO ROUTE, and says why rather than guessing:

  * THROUGH THE MAP. Closing the world map lands wherever the fleet was standing, which
    nothing records, so `WorldMapActivity` declares no destination at all.
  * ACROSS THE SEA. Leaving a village lands at sea, and sailing is not one of these
    transitions — it is a voyage the mission owns. A route that crossed it would be the
    dispatcher planning a journey.
"""
import unittest

from brain.activities.harbor import HarborActivity
from brain.activities.market import MarketActivity
from brain.activities.world_map import WorldMapActivity
from brain.run_goal import default_activities, first_hop_toward


class RoutingToAMarket(unittest.TestCase):
    def setUp(self):
        self.reg = default_activities()
        self.market = set(MarketActivity.SERVES)

    def _hop(self, where):
        return first_hop_toward(where, self.market, self.reg)

    def test_the_live_gap_from_inside_the_harbour(self):
        self.assertEqual(self._hop("building:harbor"), "EXIT_BUILDING",
                         "out to the overworld first; the harbour has no building list")

    def test_from_a_harbour_sub_menu_too(self):
        self.assertEqual(self._hop("sub_menu:departure"), "EXIT_BUILDING")

    def test_from_the_overworld_it_is_one_hop(self):
        self.assertEqual(self._hop("port_overworld"), "ENTER_BUILDING")

    def test_already_there_needs_no_hop(self):
        self.assertIsNone(self._hop("building:market"))
        self.assertIsNone(self._hop("sub_menu:purchase"))


class ItRefusesToPlanAVoyage(unittest.TestCase):
    def setUp(self):
        self.reg = default_activities()
        self.market = set(MarketActivity.SERVES)

    def test_no_route_from_the_sea(self):
        self.assertIsNone(first_hop_toward("sea", self.market, self.reg),
                          "sailing is a voyage the mission owns, not a transition")

    def test_no_route_out_of_a_village(self):
        # Leaving a village lands at SEA, so any route from here would have to cross it.
        self.assertIsNone(first_hop_toward("village", self.market, self.reg))

    def test_the_map_declares_no_destination(self):
        self.assertEqual(WorldMapActivity.LEADS_TO, {},
                         "closing it lands where we were, and nothing records that")


class TheTwoHalvesAreDeclaredTogether(unittest.TestCase):
    def test_every_landing_is_for_a_transition_the_world_can_start(self):
        """`LEADS_TO` may only describe hops `CAN_START` allows — otherwise a route is
        planned through a transition the world cannot begin."""
        from brain.run_goal import affordances
        reg = default_activities()
        for where, activities in reg.items():
            can = affordances(where, reg) or frozenset()
            for a in (activities if isinstance(activities, list) else [activities]):
                leads = getattr(a, "LEADS_TO", None) or {}
                table = leads.get(where, leads)
                if not isinstance(table, dict):
                    continue
                for name in table:
                    if name in ("port_overworld", "village"):   # the per-world outer keys
                        continue
                    self.assertIn(name, can,
                                  f"{type(a).__name__} says {name} leads somewhere from "
                                  f"{where}, but the world cannot start it")

    def test_leaving_a_building_lands_on_the_overworld(self):
        self.assertEqual(HarborActivity.LEADS_TO["EXIT_BUILDING"], "port_overworld")
        self.assertEqual(MarketActivity.LEADS_TO["EXIT_BUILDING"], "port_overworld")


class TheDispatcherRoutesRatherThanStopping(unittest.TestCase):
    def _dispatched(self, where):
        import types
        from brain.dispatcher import ActivityResult, Dispatcher, FINISHED
        from brain.intents import to_intent
        from brain.activities.market import Hold

        state = types.SimpleNamespace(state=where, location=where, port=None)
        out = []
        d = Dispatcher(perceive=lambda: state, activities=default_activities(),
                       next_goal=lambda r, s: Hold(orders={"Iron": 822}),
                       to_intent=to_intent, dispatch=out.append)
        d._advance(ActivityResult(FINISHED, {}), state)
        return [i.name for i in out]

    def test_a_market_order_in_the_harbour_steps_out(self):
        self.assertEqual(self._dispatched("building:harbor"), ["EXIT_BUILDING"])

    def test_a_market_order_on_the_overworld_is_unchanged(self):
        self.assertEqual(self._dispatched("port_overworld"), ["ENTER_BUILDING"])

    def test_a_market_order_at_sea_still_dispatches_nothing(self):
        self.assertEqual(self._dispatched("sea"), [],
                         "there is no route, and inventing one would sail the fleet")


if __name__ == "__main__":
    unittest.main()
