"""Tests for the gathering solver (#20)."""
import unittest

from brain.gathering_solver import plan_gathering, GatheringPlan


class GatheringTests(unittest.TestCase):
    def test_prefers_nearer_source_for_same_material(self):
        # Carrot at Kerch (far) and Montpellier (near) — solver must pick the near one
        # (the LLM's mistake was picking the far Kerch).
        plan = plan_gathering(
            needed=["Carrot"],
            material_sources={"Carrot": ["Kerch", "Montpellier"]},
            port_coords={"Kerch": (100, 0), "Montpellier": (10, 0)},
            start=(0, 0))
        self.assertEqual(plan.route, ["Montpellier"])
        self.assertEqual(plan.covered, {"Carrot"})

    def test_prefers_multi_material_port(self):
        # One port covers both A and B; two single-material ports are the alternative.
        plan = plan_gathering(
            needed=["A", "B"],
            material_sources={"A": ["X", "Y"], "B": ["X", "Z"]},
            port_coords={"X": (20, 0), "Y": (10, 0), "Z": (10, 5)},
            start=(0, 0))
        self.assertEqual(plan.route, ["X"])         # single stop covers both
        self.assertEqual(plan.covered, {"A", "B"})

    def test_covers_all_sofrito_materials(self):
        plan = plan_gathering(
            needed=["Onion", "Carrot", "Garlic", "Olive Oil"],
            material_sources={
                "Onion": ["Nantes", "Cairo"],
                "Carrot": ["Montpellier", "Kerch"],
                "Garlic": ["Malaga", "Alexandria"],
                "Olive Oil": ["Ragusa", "Istanbul"],
            },
            port_coords={"Nantes": (5, 5), "Cairo": (80, 10), "Montpellier": (12, 4),
                         "Kerch": (90, 8), "Malaga": (8, 2), "Alexandria": (85, 12),
                         "Ragusa": (40, 6), "Istanbul": (70, 9)},
            start=(0, 0))
        self.assertEqual(plan.covered, {"Onion", "Carrot", "Garlic", "Olive Oil"})
        self.assertEqual(plan.unsourced, set())
        # Should prefer the western cluster (Nantes/Malaga/Montpellier) over far east.
        self.assertIn("Malaga", plan.route)
        self.assertIn("Montpellier", plan.route)

    def test_unsourced_material_flagged(self):
        plan = plan_gathering(
            needed=["A", "Rare"],
            material_sources={"A": ["X"], "Rare": []},
            port_coords={"X": (10, 0)},
            start=(0, 0))
        self.assertEqual(plan.covered, {"A"})
        self.assertIn("Rare", plan.unsourced)

    def test_over_capacity_flag(self):
        plan = plan_gathering(
            needed=["A"], material_sources={"A": ["X"]},
            port_coords={"X": (10, 0)}, start=(0, 0),
            quantities={"A": 500}, cargo_capacity=300)
        self.assertTrue(plan.over_capacity)

    def test_total_distance_accumulates(self):
        plan = plan_gathering(
            needed=["A", "B"], material_sources={"A": ["X"], "B": ["Y"]},
            port_coords={"X": (10, 0), "Y": (10, 10)}, start=(0, 0))
        self.assertGreater(plan.total_distance, 0)
        self.assertEqual(set(plan.route), {"X", "Y"})


if __name__ == "__main__":
    unittest.main()
