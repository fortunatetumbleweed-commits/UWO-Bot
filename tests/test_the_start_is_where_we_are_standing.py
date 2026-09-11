"""The route's origin comes from the PLACE, not from an older, flatter record.

Two settlement stores exist and they disagreed. Live 2026-09-07, eighteen seconds apart,
planning the Hutu mission from inside LONDON's market:

    18:39:37  [observation] loaded persisted settlement: 'London'
    18:39:55  [mission] 'sub_menu' does not show the port name — using 'Madeira',
              seen 21674s ago

`current_position` asked `observed_facts.recall("settlement")` — a record with no notion of
whether the fleet has since sailed — and got a name six hours old. The market's `_port_name`
asked `observation.last_known_settlement` and got London right, all run.

It is not cosmetic. `plan_gathering` scores a port as `worth / (distance + 1)`, so a start of
Madeira makes Madeira's own score 0.24/1 against Bordeaux's 1.0/687 — it wins by 165x purely
for being where we supposedly already are. The fleet sailed to the one port the KB had
already recorded scarce for Raisin, bought 110 of 1,081, and only the low-stock reroute
rescued the mission. From the true start the answer inverts to ['Bordeaux', 'Faro'].
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

COORDS = {"London": (4680, 1210), "Madeira": (4172, 2026), "Bordeaux": (4629, 1515)}


def _call(*, observation, recalled=("Madeira", 21674.0), state="sub_menu"):
    from brain import barter_mission_live as bml
    with mock.patch("actions.sail_actions.where_am_i",
                    return_value={"port": None, "location": state}), \
         mock.patch("capture.adb_capture.capture_screen", return_value=object()), \
         mock.patch("brain.observation.current", return_value=observation), \
         mock.patch("brain.observation._ensure_persisted_loaded", return_value=None), \
         mock.patch("memory.observed_facts.recall", return_value=recalled):
        return bml.current_position(COORDS, tries=1)


def _obs(settlement, *, scene="port_overworld", age=7560.0, departed_from=None):
    """The live shape. `departed_from` is set exactly when the port is popped, and the
    settlement is None for the whole voyage — so the two together say which fact is held."""
    return types.SimpleNamespace(last_known_settlement=settlement,
                                 last_known_base_scene=scene,
                                 departed_from=departed_from,
                                 settlement_age_s=lambda now=None: age)


class TheFresherReadingWins(unittest.TestCase):
    """Not "which module owns the fact" — which reading is more recent. Both are dated."""

    def test_it_uses_the_place_when_the_place_was_seen_more_recently(self):
        self.assertEqual(_call(observation=_obs("London", age=7560.0)), COORDS["London"])

    def test_not_the_six_hour_old_name(self):
        self.assertNotEqual(_call(observation=_obs("London", age=7560.0)),
                            COORDS["Madeira"])

    def test_the_OLDER_record_wins_when_it_is_the_fresher_one(self):
        """Neither store is privileged. A stale observation must not beat a fresh recall."""
        self.assertEqual(_call(observation=_obs("Madeira", age=99999.0),
                               recalled=("London", 12.0)), COORDS["London"])

    def test_the_older_record_still_answers_when_the_place_knows_nothing(self):
        """It is a worse answer, not no answer — and it is all there is here."""
        self.assertEqual(_call(observation=_obs(None)), COORDS["Madeira"])


class AtSeaTheFleetIsNowhere(unittest.TestCase):
    """Underway, the remembered name is the voyage's ORIGIN, not our position."""

    def test_a_departed_fleet_is_placed_nowhere(self):
        """No position, but an origin — which is not the same answer."""
        self.assertIsNone(_call(observation=_obs(None, scene="sea",
                                                 departed_from="London")))

    def test_and_it_does_not_fall_back_to_the_older_record_either(self):
        """Falling through would put the fleet at a port it is demonstrably not at."""
        self.assertIsNone(_call(observation=_obs(None, scene="sea", departed_from="London"),
                                recalled=("Madeira", 30.0)))

    def test_the_map_opened_MID_VOYAGE_is_still_a_voyage(self):
        self.assertIsNone(_call(observation=_obs(None, scene="world_map",
                                                 departed_from="London")))


class TheWorldMapIsNotADeparture(unittest.TestCase):
    """It is a screen opened FROM somewhere. Standing in a port with the map up leaves the
    fleet exactly where it was.

    The plan is computed immediately after the REMOTE VILLAGE CHECK, and that check runs on
    the world map — so treating the map as departure fired on essentially every mission.
    Live 2026-09-08, planning Box of Nutmeg with the fleet moored at Ambon:

        [mission] the fleet is between ports — placing it nowhere rather than at the port
                  it sailed from
        graph: [... 'gather:Santo Domingo', 'gather:Masulipatnam', 'gather:Jakarta' ...]

    With no origin the solver cannot compare distances and orders by COVERAGE alone:

        real origin (Ambon) -> ['Ambon', 'Guam', 'Kolkata']
        placed nowhere      -> ['Santo Domingo', 'Masulipatnam', 'Jakarta']

    Masulipatnam->Santo Domingo is 4,181 against 1,722 to Guam, and the fleet was standing
    on the Ebony it was about to sail away from.
    """

    def test_the_map_keeps_the_port_we_are_standing_in(self):
        self.assertEqual(_call(observation=_obs("London", scene="world_map")),
                         COORDS["London"])

    def test_a_port_overworld_obviously_does_too(self):
        self.assertEqual(_call(observation=_obs("London", scene="port_overworld")),
                         COORDS["London"])


class WhatItChangesInTheRoute(unittest.TestCase):
    """The reason this matters at all."""

    def _route(self, start):
        from brain.gathering_solver import plan_gathering
        sources = {"Raisin": ["Bordeaux", "Madeira", "Trabzon"], "Pig": ["Faro", "Gijon"]}
        coords = dict(COORDS, Faro=(4430, 1820), Trabzon=(5760, 1669))
        return plan_gathering(list(sources), sources, coords, coords.get(start),
                              quantities={"Raisin": 1081, "Pig": 1081},
                              season_fn=lambda p, m: "low" if (p, m) == ("Madeira",
                                                                        "Raisin") else None).route

    def test_the_stale_start_picks_the_scarce_port(self):
        self.assertEqual(self._route("Madeira")[0], "Madeira")

    def test_the_true_start_picks_Bordeaux(self):
        self.assertEqual(self._route("London")[0], "Bordeaux")


if __name__ == "__main__":
    unittest.main()
