"""World model — Company → Fleets → Ships, departure diagnosis, serializer."""
import unittest

from brain.world_model import WorldModel, Fleet, Ship


class DepartureDiagnosisTests(unittest.TestCase):
    def test_healthy_fleet_can_depart(self):
        f = Fleet(location="London", supply_days=6, crew_current=60,
                  crew_capacity=60, ships=[Ship("Barque", 88)])
        ok, reasons = f.can_depart()
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_crew_shortage_blocks_depart(self):
        # the actual live failure: crew short, so the fleet cannot depart
        f = Fleet(location="London", current_building="Harbor", supply_days=6,
                  crew_current=42, crew_capacity=60, ships=[Ship("Barque", 88)])
        ok, reasons = f.can_depart()
        self.assertFalse(ok)
        self.assertEqual(reasons, ["crew short (42/60)"])   # readable blocker

    def test_low_ship_life_blocks_depart(self):
        f = Fleet(location="Seville", crew_current=60, crew_capacity=60,
                  ships=[Ship("Barque", 88), Ship("Sloop", 14)])
        ok, reasons = f.can_depart()
        self.assertFalse(ok)
        self.assertIn("ship life < 20: Sloop", reasons[0])

    def test_unknown_fields_are_not_blockers(self):
        # missing crew/supply info must not be treated as a blocker
        f = Fleet(location="London", ships=[Ship("Barque", 88)])
        ok, reasons = f.can_depart()
        self.assertTrue(ok)


class CurrencyDisciplineTests(unittest.TestCase):
    def test_red_gem_needs_confirmation(self):
        wm = WorldModel(currencies={"ducat": 1_000, "blue_gem": 340, "red_gem": 12})
        self.assertTrue(wm.needs_confirmation_to_spend("red_gem"))
        self.assertFalse(wm.needs_confirmation_to_spend("blue_gem"))
        self.assertFalse(wm.needs_confirmation_to_spend("ducat"))


class ActiveFleetTests(unittest.TestCase):
    def test_active_fleet_selection(self):
        a, b = Fleet(location="London"), Fleet(location="Seville")
        wm = WorldModel(fleets=[a, b], active_fleet_idx=1)
        self.assertIs(wm.fleet, b)

    def test_no_fleet_returns_none(self):
        self.assertIsNone(WorldModel().fleet)


class SerializerTests(unittest.TestCase):
    def test_to_prompt_surfaces_the_blocker(self):
        wm = WorldModel(
            currencies={"ducat": 14_001_701_263, "blue_gem": 340, "red_gem": 12},
            fleets=[Fleet(location="London", current_building="Harbor",
                          supply_days=6, crew_current=42, crew_capacity=60,
                          cargo_used=566, cargo_capacity=4108,
                          ships=[Ship("Barque", 88)])],
            discovered_ports=["London", "Amsterdam", "Port Royal"],
        )
        text = wm.to_prompt()
        self.assertIn("inside Harbor", text)
        self.assertIn("42/60 SHORT", text)
        self.assertIn("can_depart: NO", text)
        self.assertIn("crew short (42/60)", text)
        self.assertIn("ducat=14,001,701,263", text)


if __name__ == "__main__":
    unittest.main()
