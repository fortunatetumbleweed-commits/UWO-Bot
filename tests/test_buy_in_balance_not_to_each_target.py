"""A round consumes ALL its materials, so the barter is capped by the SCARCEST.

Live 2026-09-06, the Hutu run:

    Pig     bought 1,828   used 1,099   left 729
    Raisin  bought 1,100   used 1,099   left 1

Raisin capped the barter at 6 rounds, so 729 Pig — 40% of what was bought — was carried to
the village and back unused: money, gems, hold space, and the Faro refreshes that fetched it.
The plan bought each material to its own padded 1,755 as though they were independent.

WHAT THIS DOES NOT FIX, and the numbers above are exactly that case: Pig was gathered FIRST,
before Raisin's shortfall could be known, so no cap could have seen it coming. This bites when
the scarce material is gathered first, or when a later leg would top up one already past the
cap. Knowing in advance that Madeira's Raisin is thin is the season flag's job
(`docs/low_stock_as_a_planning_input.md`); the two are meant to compound.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.mission_runner import MissionRunner

RECIPE = {"Pig": 183, "Raisin": 183}        # per round, at max amity


def _runner(orders, aboard):
    m = MissionRunner.__new__(MissionRunner)
    m.subtasks = [types.SimpleNamespace(kind="gather", id="gather:Faro", done=False,
                                        params={"orders": dict(orders)})]
    m._materials_aboard = dict(aboard)
    return m


def _with_recipe(recipe):
    return mock.patch("brain.mission_progress.current",
                      return_value={"recipe": dict(recipe)} if recipe else {})


class TheWantIsCappedByTheScarcestMaterial(unittest.TestCase):

    def test_a_FINISHED_material_caps_the_one_still_being_bought(self):
        """Raisin's gathers are done at 1,100 — 6 rounds — so Pig is wanted for 6, not 1,755."""
        m = _runner({"Pig": 1755}, {"raisin": 1100, "pig": 457})
        with _with_recipe(RECIPE):
            self.assertEqual(m._everything_still_wanted(), {"Pig": 1098})

    def test_A_MATERIAL_STILL_BEING_BOUGHT_IS_NOT_A_CONSTRAINT(self):
        """Otherwise the cap is self-fulfilling: the want falls to what is already aboard and
        no more can ever be bought. Pig at 200 mid-gather would cap the mission to 1 round.

        Raisin DOES stop here, and that is the bottleneck rule below doing its job — it is
        1,464 against Pig's 200, seven rounds ahead, and the hold is needed for the material
        that is behind. What must never happen is Pig being capped."""
        m = _runner({"Pig": 1755, "Raisin": 1755}, {"raisin": 1464, "pig": 200})
        with _with_recipe(RECIPE):
            out = m._everything_still_wanted()
        self.assertEqual(out["Pig"], 1755, "the bottleneck must always be free to grow")
        self.assertEqual(out["Raisin"], 1464, "already far ahead — stop topping it up")

    def test_an_UNREAD_material_yields_no_cap(self):
        """Unread is not zero — capping on a material nobody has looked at would want 0."""
        m = _runner({"Pig": 1755, "Raisin": 1755}, {"pig": 1828})
        with _with_recipe(RECIPE):
            self.assertEqual(m._everything_still_wanted(), {"Pig": 1755, "Raisin": 1755})

    def test_NO_RECIPE_means_no_cap(self):
        """A cap computed from a guess is worse than none."""
        m = _runner({"Pig": 1755}, {"raisin": 1100})
        with _with_recipe(None):
            self.assertEqual(m._everything_still_wanted(), {"Pig": 1755})

    def test_nothing_aboard_yet_means_no_cap(self):
        m = _runner({"Pig": 1755, "Raisin": 1755}, {})
        with _with_recipe(RECIPE):
            self.assertEqual(m._everything_still_wanted(), {"Pig": 1755, "Raisin": 1755})

    def test_a_recipe_the_orders_do_not_mention_still_constrains(self):
        """Raisin is not on the list any more — which is exactly what makes it the cap."""
        m = _runner({"Pig": 1755}, {"raisin": 1100, "pig": 1828})
        with _with_recipe(RECIPE):
            self.assertEqual(m._everything_still_wanted(), {"Pig": 1098})


if __name__ == "__main__":
    unittest.main()


class NotBoughtAHEADOfTheBottleneck(unittest.TestCase):
    """Rounds are limited by the SCARCEST material, so buying more of a plentiful one buys no
    rounds — it buys cargo space away from the material that would.

    Live 2026-09-07 at Faro. Pig read 1,505 against a padded 1,755 and so read SHORT, and the
    buy round did as told. Raisin stood at 881 — three rounds — needing 654 Pig, so the hold
    already carried more than twice what any round could use. The ship finished at
    4,952/4,952, FULL OF PIG, with no room for the Raisin that gates the barter.

    The padded target was not wrong; it answered "how much would seven rounds take?" rather
    than "how much can we currently use?".
    """

    def test_the_plentiful_material_stops_and_the_scarce_one_does_not(self):
        m = _runner({"Pig": 1755, "Raisin": 1997}, {"pig": 1505, "raisin": 881})
        with _with_recipe({"Pig": 218, "Raisin": 248}):
            out = m._everything_still_wanted()
        self.assertEqual(out["Pig"], 1505, "Pig is 6 rounds against Raisin's 3 — stop")
        self.assertEqual(out["Raisin"], 1997, "Raisin is the bottleneck — keep buying")

    def test_THE_BOTTLENECK_ITSELF_IS_NEVER_CAPPED(self):
        """Capping on what is aboard is self-fulfilling — the scarce one must always grow."""
        m = _runner({"Pig": 1755, "Raisin": 1997}, {"pig": 200, "raisin": 1464})
        with _with_recipe({"Pig": 218, "Raisin": 248}):
            out = m._everything_still_wanted()
        self.assertEqual(out["Pig"], 1755, "Pig is the bottleneck — keep buying")
        self.assertEqual(out["Raisin"], 1464, "Raisin is 5 rounds ahead — stop")

    def test_materials_LEVEL_with_each_other_both_keep_buying(self):
        m = _runner({"Pig": 1755, "Raisin": 1997}, {"pig": 436, "raisin": 496})
        with _with_recipe({"Pig": 218, "Raisin": 248}):
            out = m._everything_still_wanted()
        self.assertEqual((out["Pig"], out["Raisin"]), (1755, 1997))

    def test_an_UNREAD_material_yields_no_bottleneck(self):
        m = _runner({"Pig": 1755, "Raisin": 1997}, {"pig": 1505})
        with _with_recipe({"Pig": 218, "Raisin": 248}):
            out = m._everything_still_wanted()
        self.assertEqual((out["Pig"], out["Raisin"]), (1755, 1997))

    def test_it_resumes_once_the_bottleneck_catches_up(self):
        """Stopping is not permanent — it is a "not yet", and the next look re-decides."""
        m = _runner({"Pig": 1755, "Raisin": 1997}, {"pig": 1505, "raisin": 1500})
        with _with_recipe({"Pig": 218, "Raisin": 248}):
            out = m._everything_still_wanted()
        self.assertEqual(out["Pig"], 1755, "Raisin now funds 6 rounds; Pig may grow again")
