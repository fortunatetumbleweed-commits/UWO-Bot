"""Data has an owner, and dies with it.

The question to ask of a remembered value is not "where did I read it?" but "WHO DOES IT
BELONG TO?" (user, 2026-08-26). Only the second predicts when it goes bad, and the cart is
the case that proves it: the market's right panel shows the cart and the hold in ONE
rectangle — active tiles staged, greyed tiles already owned — so a model keyed on where a
value was read gives them the same fate, and one of them is wrong.
"""

from __future__ import annotations

import types
import unittest

from brain.owned_state import (BUILDING, COMPANY, FLEET, PANEL, PLACE,
                               changed, observe, recall, remember, reset)


def _at(state, port=None):
    return types.SimpleNamespace(state=state, port=port)


class TheCartAndTheCargoShareARectangleAndNothingElse(unittest.TestCase):
    """The example the whole design rests on."""

    def setUp(self):
        reset()
        observe(_at("building:market", "Seville"))
        remember("cart", {"Iron": 470}, owner=PANEL)
        remember("hold", {"Iron": 445}, owner=FLEET)

    def test_leaving_the_market_destroys_the_cart(self):
        observe(_at("port_overworld", "Seville"))
        self.assertIsNone(recall("cart"),
                          "a remembered cart is an instruction to buy things nobody chose")

    def test_leaving_the_market_keeps_the_cargo(self):
        observe(_at("port_overworld", "Seville"))
        self.assertEqual(recall("hold"), {"Iron": 445})

    def test_the_cargo_survives_going_to_sea(self):
        """The fleet is the owner that moves. A place-based model would drop the hold on
        departure — the one fact the next leg needs."""
        observe(_at("sea"))
        self.assertEqual(recall("hold"), {"Iron": 445})


class LeavingKillsWhatBelongedToWhereYouWere(unittest.TestCase):

    def setUp(self):
        reset()

    def test_sailing_to_another_port_drops_the_first_ports_data(self):
        """Both are `port_overworld`; only the NAME changed. A state-string comparison
        misses this, and it is the common case."""
        observe(_at("port_overworld", "Seville"))
        remember("prices", {"Iron": 102}, owner=PLACE)
        observe(_at("port_overworld", "Barcelona"))
        self.assertIsNone(recall("prices"))

    def test_leaving_a_place_also_ends_its_buildings_and_panels(self):
        """Nobody enumerates them — that is the point of the nesting."""
        observe(_at("building:market", "Seville"))
        remember("amity", 60000, owner=PLACE)
        remember("shelf", ["Iron"], owner=BUILDING)
        remember("tile", "Iron", owner=PANEL)
        observe(_at("sea"))
        self.assertEqual([recall("amity"), recall("shelf"), recall("tile")], [None, None, None])

    def test_walking_between_buildings_keeps_the_port(self):
        observe(_at("building:market", "Seville"))
        remember("amity", 60000, owner=PLACE)
        remember("shelf", ["Iron"], owner=BUILDING)
        observe(_at("building:harbor", "Seville"))
        self.assertEqual(recall("amity"), 60000, "still the same port")
        self.assertIsNone(recall("shelf"), "a different building")

    def test_a_sub_menu_does_not_leave_the_building(self):
        """`sub_menu:purchase` is INSIDE the market and the state string does not say which
        building. Treating it as unknown would drop the market's contents every time the bot
        opened the Purchase tab."""
        observe(_at("building:market", "Seville"))
        remember("shelf", ["Iron"], owner=BUILDING)
        observe(_at("sub_menu:purchase", "Seville"))
        self.assertEqual(recall("shelf"), ["Iron"])

    def test_a_notice_over_a_world_does_not_leave_it(self):
        """A transient covers a world rather than replacing it. Dropping the market's
        contents because a promotion popup appeared would be the bug, not the fix."""
        observe(_at("building:market", "Seville"))
        remember("shelf", ["Iron"], owner=BUILDING)
        observe(_at("transient"))
        self.assertEqual(recall("shelf"), ["Iron"])

    def test_the_world_map_does_not_leave_the_port_either(self):
        observe(_at("port_overworld", "Seville"))
        remember("prices", {"Iron": 102}, owner=PLACE)
        observe(_at("world_map"))
        self.assertEqual(recall("prices"), {"Iron": 102})

    def test_the_sea_does(self):
        observe(_at("port_overworld", "Seville"))
        remember("prices", {"Iron": 102}, owner=PLACE)
        observe(_at("sea"))
        self.assertIsNone(recall("prices"))


class ActingKillsWhatItChanged(unittest.TestCase):
    """The second way data goes bad, and the one a transition rule alone would miss."""

    def setUp(self):
        reset()
        observe(_at("building:market", "Seville"))

    def test_buying_changes_the_hold_and_the_shelf(self):
        remember("hold", {"Iron": 0}, owner=FLEET)
        remember("shelf", ["Iron"], owner=BUILDING)
        remember("ducats", 63_619_000_000, owner=COMPANY)
        changed(FLEET, BUILDING)
        self.assertIsNone(recall("hold"))
        self.assertIsNone(recall("shelf"))
        self.assertIsNotNone(recall("ducats"), "not what the caller said had changed")

    def test_bartering_changes_the_hold_and_the_village(self):
        remember("hold", {"Iron": 445}, owner=FLEET)
        remember("amity", 60000, owner=PLACE)
        changed(FLEET, PLACE)
        self.assertEqual([recall("hold"), recall("amity")], [None, None])

    def test_fleet_data_is_immune_to_moving_and_exposed_to_acting(self):
        remember("hold", {"Iron": 445}, owner=FLEET)
        observe(_at("sea"))
        self.assertIsNotNone(recall("hold"))
        changed(FLEET)
        self.assertIsNone(recall("hold"))


class TheStoreRefusesWhatItCannotAgeHonestly(unittest.TestCase):

    def setUp(self):
        reset()

    def test_an_unknown_owner_is_refused(self):
        with self.assertRaises(ValueError):
            remember("x", 1, owner="somewhere")

    def test_the_company_outlives_everything(self):
        remember("mission", "birch tree", owner=COMPANY)
        for where in ("building:market", "sub_menu:purchase", "sea", "village", "world_map"):
            observe(_at(where, "Svear Village"))
        self.assertEqual(recall("mission"), "birch tree")

    def test_the_first_look_drops_nothing(self):
        remember("hold", 1, owner=FLEET)
        remember("prices", 2, owner=PLACE)
        self.assertEqual(observe(_at("port_overworld", "Seville")), [])
        self.assertIsNotNone(recall("prices"))


if __name__ == "__main__":
    unittest.main()


class TheDispatcherDropsOnEveryTransition(unittest.TestCase):
    """Not an error path — a rule that runs on every tick.

    The dispatcher already refreshed stale data when an activity got LOST. That is the same
    act, and confining it to the error path was the mistake: getting lost is not the only way
    a belief goes bad. Sailing away is the ordinary way.
    """

    def _dispatcher(self, states):
        from brain.dispatcher import ActivityResult, Dispatcher, FINISHED
        seq = list(states)

        class Nothing:
            name = "nothing"
            def work(self, goal, state):
                return ActivityResult(FINISHED, {}, detail="did nothing")

        # The dispatcher perceives AFTER every activity as well as before, so it looks more
        # than once per tick. Hold the last state rather than running dry.
        def look():
            return seq.pop(0) if len(seq) > 1 else seq[0]

        return Dispatcher(perceive=look,
                          activities={"port_overworld": Nothing(), "sea": Nothing(),
                                      "building:market": Nothing()},
                          next_goal=lambda r, s: "a goal",
                          to_intent=lambda g, s: None,
                          dispatch=lambda i: None)

    def test_a_market_value_does_not_survive_the_voyage(self):
        reset()
        d = self._dispatcher([_at("building:market", "Seville"), _at("sea")])
        d.step()
        remember("shelf", ["Iron"], owner=BUILDING)
        d._fresh = None                       # force the next tick to perceive again
        d.step()
        self.assertIsNone(recall("shelf"))

    def test_the_hold_does(self):
        reset()
        d = self._dispatcher([_at("building:market", "Seville"), _at("sea")])
        d.step()
        remember("hold", {"Iron": 445}, owner=FLEET)
        d._fresh = None
        d.step()
        self.assertEqual(recall("hold"), {"Iron": 445})


class TheActivitiesDeclareWhatTheyChange(unittest.TestCase):
    """A transition rule alone would miss every action-caused staleness.

    Buying does not move the fleet an inch, and it still makes the hold and the shelf wrong.
    """

    def test_buying_invalidates_the_hold_and_the_shelf(self):
        from brain.activities.market import Hold, MarketActivity
        reset()
        observe(_at("building:market", "Seville"))
        remember("hold", {"Iron": 0}, owner=FLEET)
        remember("shelf", ["Iron"], owner=BUILDING)
        remember("mission", "birch tree", owner=COMPANY)

        MarketActivity(buy_fn=lambda p, g, **k: {"met": True, "bought_total": 445},
                       show_grid_fn=lambda: None, port_fn=lambda: "Seville"
                       ).work(Hold({"Iron": 242}), _at("building:market", "Seville"))

        self.assertIsNone(recall("hold"))
        self.assertIsNone(recall("shelf"))
        self.assertEqual(recall("mission"), "birch tree", "the company is untouched")

    def test_bartering_invalidates_the_hold_and_the_village(self):
        """A round spends materials, moves the amity, and uses one of the day's slots."""
        from brain.activities.village import Barter, VillageActivity
        reset()
        observe(_at("village", "Svear Village"))
        remember("hold", {"Wares": 445}, owner=FLEET)
        remember("amity", 60000, owner=PLACE)

        panel = types.SimpleNamespace(rounds_remaining=0, materials={}, amity_points=(1, 2),
                                      partial_fraction=0.0, binding=None, shortfall=0)
        VillageActivity(open_panel_fn=lambda: True, select_fn=lambda g, r: True,
                        read_panel_fn=lambda: panel,
                        commit_fn=lambda: {"ok": True},
                        context_fn=lambda _s: __import__("brain.village_context",
                                                         fromlist=["x"]).BARTER_PANEL_READY,
                        overflow_fn=lambda: 0, saw_fn=lambda: {},
                        exchange_live_fn=lambda: False, recipe_fn=lambda g: None
                        ).work(Barter("Birch Tree", "Svear Village"),
                               _at("village", "Svear Village"))

        self.assertIsNone(recall("hold"))
        self.assertIsNone(recall("amity"))


class ThePlaceItselfIsRemembered(unittest.TestCase):
    """"Which port is this?" is the most-asked PLACE fact, and the one a building cannot
    answer from the frame — a market has no port name on screen.

    It cost the mission three times before it was stored: `'port': ''` in the market result,
    `clear_surplus` refusing from a sub-menu, and finally `trim_before_gather` aborting the
    whole run from INSIDE Barcelona's market having just successfully sold the surplus
    (live 2026-08-27). Each was `_current_port()` — a question about the PLACE — asked of the
    screen, which only answers on the overworld.
    """

    def setUp(self):
        reset()

    def test_the_first_look_records_it(self):
        """Usually the only look taken on the overworld, where the name IS legible — so a
        first-look early return would skip the one observation that matters."""
        observe(_at("port_overworld", "Barcelona"))
        self.assertEqual(recall("port"), "Barcelona")

    def test_it_survives_walking_into_a_building(self):
        observe(_at("port_overworld", "Barcelona"))
        observe(_at("building:market", "Barcelona"))
        self.assertEqual(recall("port"), "Barcelona")

    def test_it_survives_a_sub_menu_that_names_no_port(self):
        observe(_at("port_overworld", "Barcelona"))
        observe(_at("sub_menu:sell", None))
        self.assertEqual(recall("port"), "Barcelona")

    def test_putting_to_sea_forgets_it(self):
        """PLACE dies when the fleet leaves — that is the whole point of the owner."""
        observe(_at("port_overworld", "Barcelona"))
        observe(_at("sea"))
        self.assertIsNone(recall("port"))

    def test_sailing_to_another_port_replaces_it(self):
        observe(_at("port_overworld", "Barcelona"))
        observe(_at("sea"))
        observe(_at("port_overworld", "Tripoli"))
        self.assertEqual(recall("port"), "Tripoli")
