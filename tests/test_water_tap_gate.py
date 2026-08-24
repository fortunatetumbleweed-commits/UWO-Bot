"""The water-tap localiser must not tap unless the world map is actually on screen.

Live 2026-08-21: the fleet was at sea, the caller believed it was localising a world-map
camera, and `tap_water_and_read_latlon` fired eight taps into the sea view — one of them
at (1900, 500), which is where `Kochi` sits in the sea destination list. The fleet then
sailed to Kochi. A localisation routine must never be able to steer the ship."""
import unittest
from unittest import mock

from actions.water_tap import tap_water_and_read_latlon


class LocationGateTests(unittest.TestCase):
    def _run(self, on_map, **kw):
        taps = []
        with mock.patch("actions.water_tap._on_world_map", return_value=on_map), \
             mock.patch("actions.water_tap._adb_tap",
                        side_effect=lambda x, y: taps.append((x, y))), \
             mock.patch("actions.water_tap.capture_screen", return_value=object()), \
             mock.patch("actions.water_tap.read_latlon_from_frame", return_value=(1.0, 2.0)), \
             mock.patch("time.sleep"):
            return tap_water_and_read_latlon(1900, 500, **kw), taps

    def test_it_refuses_to_tap_when_not_on_the_world_map(self):
        got, taps = self._run(False)
        self.assertIsNone(got)
        self.assertEqual(taps, [])          # the sea-view tap that moved the fleet

    def test_it_taps_normally_on_the_world_map(self):
        got, taps = self._run(True)
        self.assertEqual(got, (1.0, 2.0))
        self.assertEqual(taps, [(1900, 500)])

    def test_the_gate_can_be_waived_explicitly_only(self):
        got, taps = self._run(False, require_world_map=False)
        self.assertEqual(got, (1.0, 2.0))
        self.assertEqual(taps, [(1900, 500)])
