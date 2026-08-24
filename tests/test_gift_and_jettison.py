"""Tests for #26 gifting executor and #27 jettison executor (mocked taps)."""
import unittest
from unittest.mock import patch

from actions.barter_executor import gift_verified
from actions.jettison_executor import execute_jettison
from brain.jettison_planner import CargoItem


def _seq(values):
    it = iter(values)
    return lambda *a, **k: next(it)


@patch("actions.barter_executor.time.sleep", lambda *a: None)
class GiftTests(unittest.TestCase):
    def _run(self, amity_seq):
        return gift_verified(
            capture_fn=lambda: "F",
            read_amity_fn=_seq(amity_seq),
            select_gift_fn=lambda cap: True,
            gift_commit_fn=lambda: True,
            confirm_fn=lambda: True,
        )

    def test_ok_when_amity_rises(self):
        r = self._run([744, 813])         # before, after
        self.assertTrue(r["ok"])
        self.assertEqual((r["amity_before"], r["amity_after"]), (744, 813))

    def test_not_ok_when_flat(self):
        self.assertFalse(self._run([744, 744])["ok"])


class JettisonTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.discard = lambda item, qty: (self.calls.append((item.name, qty)) or True)

    def test_dumps_exact_planned_quantities_cheapest_first(self):
        cargo = [CargoItem("Camas", 800, unit_value=86),
                 CargoItem("Avocado", 200, unit_value=3)]
        r = execute_jettison(50, cargo, reserves={}, discard_fn=self.discard,
                             verify_cleared_fn=lambda: True)
        self.assertTrue(r["ok"])
        self.assertEqual(self.calls, [("Avocado", 50)])   # cheapest, exact qty

    def test_supply_never_below_reserve(self):
        # 226 each, reserve 165 → only 61 of each is dumpable. Overflow 200 > 122.
        cargo = [CargoItem("Water", 226, 1, resource="water"),
                 CargoItem("Food", 226, 1, resource="food")]
        r = execute_jettison(200, cargo, reserves={"water": 165, "food": 165},
                             discard_fn=self.discard, verify_cleared_fn=lambda: False)
        dumped = dict(self.calls)
        # never dumps more than the excess above reserve
        self.assertLessEqual(dumped.get("Water", 0), 61)
        self.assertLessEqual(dumped.get("Food", 0), 61)
        # 226 - dumped >= reserve for both
        self.assertGreaterEqual(226 - dumped.get("Water", 0), 165)
        self.assertGreaterEqual(226 - dumped.get("Food", 0), 165)
        self.assertEqual(r["shortfall"], 200 - 122)
        self.assertFalse(r["ok"])                          # couldn't clear safely

    def test_goods_dumped_before_supply(self):
        cargo = [CargoItem("Water", 300, 1, resource="water"),
                 CargoItem("Trinket", 100, 2)]
        execute_jettison(50, cargo, reserves={"water": 100}, discard_fn=self.discard,
                         verify_cleared_fn=lambda: True)
        self.assertEqual(self.calls, [("Trinket", 50)])    # supply untouched

    def test_verify_cleared_gates_ok(self):
        cargo = [CargoItem("Trinket", 100, 2)]
        r = execute_jettison(10, cargo, reserves={}, discard_fn=self.discard,
                             verify_cleared_fn=lambda: False)
        self.assertFalse(r["ok"])                           # dialog still present


if __name__ == "__main__":
    unittest.main()
