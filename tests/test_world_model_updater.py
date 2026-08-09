"""World-model updater — location/current_building/discovered from PerceivedState."""
import unittest

from brain.perceived_state import PerceivedState
from brain.world_model import WorldModel, Ship
from brain.world_model_updater import (
    update_from_perceived, set_currencies, set_crew, set_cargo, set_ships,
    AT_SEA,
)


class LocationUpdateTests(unittest.TestCase):
    def test_sea_frame_sets_at_sea(self):
        wm = WorldModel()
        update_from_perceived(wm, PerceivedState.from_legacy("sea"))
        self.assertEqual(wm.fleet.location, AT_SEA)
        self.assertIsNone(wm.fleet.current_building)

    def test_port_frame_sets_location_and_learns_port(self):
        wm = WorldModel()
        update_from_perceived(wm, PerceivedState.from_legacy("port_overworld", port="Seville"))
        self.assertEqual(wm.fleet.location, "Seville")
        self.assertIsNone(wm.fleet.current_building)
        self.assertIn("Seville", wm.discovered_ports)

    def test_port_name_override(self):
        wm = WorldModel()
        # perceived identity is noisy; caller passes the corrected port
        ps = PerceivedState.from_legacy("port_overworld", port="Sevile")
        update_from_perceived(wm, ps, port_name="Seville")
        self.assertEqual(wm.fleet.location, "Seville")

    def test_panel_sets_current_building_keeps_location(self):
        wm = WorldModel()
        update_from_perceived(wm, PerceivedState.from_legacy("port_overworld", port="London"))
        # now enter the Inn (a panel with context)
        ps = PerceivedState(base="panel", context="Inn")
        update_from_perceived(wm, ps)
        self.assertEqual(wm.fleet.current_building, "Inn")
        self.assertEqual(wm.fleet.location, "London")   # still at London

    def test_transient_screens_leave_location(self):
        wm = WorldModel()
        update_from_perceived(wm, PerceivedState.from_legacy("port_overworld", port="London"))
        update_from_perceived(wm, PerceivedState.from_legacy("world_map"))
        update_from_perceived(wm, PerceivedState.from_legacy("loading"))
        self.assertEqual(wm.fleet.location, "London")   # unchanged

    def test_no_duplicate_discovered_ports(self):
        wm = WorldModel()
        for _ in range(3):
            update_from_perceived(wm, PerceivedState.from_legacy("port_overworld", port="London"))
        self.assertEqual(wm.discovered_ports.count("London"), 1)

    def test_none_perceived_is_safe(self):
        wm = WorldModel()
        update_from_perceived(wm, None)   # no crash, no-op


class FieldSetterTests(unittest.TestCase):
    def test_crew_and_departure_diagnosis(self):
        # the crew-shortage flow, populated via the updater
        wm = WorldModel()
        update_from_perceived(wm, PerceivedState.from_legacy("port_overworld", port="London"))
        set_crew(wm, current=42, capacity=60)
        set_ships(wm, [("Barque", 88)])
        ok, reasons = wm.fleet.can_depart()
        self.assertFalse(ok)
        self.assertEqual(reasons, ["crew short (42/60)"])

    def test_currencies_and_cargo(self):
        wm = WorldModel()
        set_currencies(wm, {"ducat": 1000, "blue_gem": 5, "red_gem": None})  # None ignored
        set_cargo(wm, used=566, capacity=4108, contents={"Flax": 300})
        self.assertEqual(wm.currencies, {"ducat": 1000, "blue_gem": 5})
        self.assertEqual(wm.fleet.cargo_used, 566)
        self.assertEqual(wm.fleet.cargo["Flax"], 300)


if __name__ == "__main__":
    unittest.main()
