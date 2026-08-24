"""Tests for the P4 barter executors (#24 commit, #25 negotiation) — mocked taps."""
import types
import unittest
from unittest.mock import patch

from actions.barter_executor import (
    barter_commit_verified, decide_negotiation, execute_negotiation,
)


def _seq(values):
    it = iter(values)
    return lambda *a, **k: next(it)


def _ns(pts):
    return types.SimpleNamespace(amity_points=pts)


@patch("actions.barter_executor.time.sleep", lambda *a: None)
class BarterCommitTests(unittest.TestCase):
    def _run(self, panel_seq, cargo_seq):
        return barter_commit_verified(
            capture_fn=lambda: "F",
            read_panel_fn=_seq(panel_seq),
            read_cargo_fn=_seq(cargo_seq),
            commit_fn=lambda: [(1959, 940)],
            confirm_fn=lambda cap: False,
        )

    def test_ok_when_amity_changes(self):
        r = self._run([_ns(709), _ns(744)], [100, 100])
        self.assertTrue(r["ok"])
        self.assertIn("amity", r["reason"])
        self.assertEqual(r["tapped"], [(1959, 940)])

    def test_ok_when_cargo_up(self):
        r = self._run([_ns(709), _ns(709)], [100, 809])
        self.assertTrue(r["ok"])
        self.assertIn("cargo", r["reason"])

    def test_not_ok_when_no_change(self):
        r = self._run([_ns(709), _ns(709)], [100, 100])
        self.assertFalse(r["ok"])


class NegotiationDecisionTests(unittest.TestCase):
    def test_protect_amity_skips(self):
        self.assertEqual(decide_negotiation(carrying_surplus_materials=False,
                                            spare_cargo_space=500, protect_amity=True), "no")

    def test_surplus_materials_skips(self):
        self.assertEqual(decide_negotiation(carrying_surplus_materials=True,
                                            spare_cargo_space=500, protect_amity=False), "no")

    def test_haggle_once_when_room_and_no_surplus(self):
        self.assertEqual(decide_negotiation(carrying_surplus_materials=False,
                                            spare_cargo_space=500, protect_amity=False), "once")

    def test_no_room_skips(self):
        self.assertEqual(decide_negotiation(carrying_surplus_materials=False,
                                            spare_cargo_space=0, protect_amity=False), "no")


class NegotiationExecuteTests(unittest.TestCase):
    def test_taps_matching_button(self):
        taps = []
        # find_button returns a pos for the 'no' keyword only.
        def find(kw):
            return (500, 900) if kw == "no" else None
        r = execute_negotiation("no", capture_fn=lambda: "F",
                                tap_fn=lambda x, y: taps.append((x, y)),
                                find_button_fn=find)
        self.assertTrue(r["ok"])
        self.assertEqual(taps, [(500, 900)])

    def test_reports_when_button_absent(self):
        r = execute_negotiation("all", capture_fn=lambda: "F",
                                tap_fn=lambda x, y: None,
                                find_button_fn=lambda kw: None)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
