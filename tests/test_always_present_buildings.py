"""Test the always-present basic-buildings KB (user 2026-08-15)."""
import unittest
from brain.kb import control


class AlwaysPresentBuildingsTests(unittest.TestCase):
    def test_basic_buildings_are_always_present(self):
        ap = control().always_present_buildings()
        for b in ("harbor", "market", "inn", "bureau", "shipyard"):
            self.assertIn(b, ap)

    def test_is_always_present(self):
        kb = control()
        self.assertTrue(kb.is_always_present("Harbor"))
        self.assertTrue(kb.is_always_present("market"))
        self.assertFalse(kb.is_always_present("Palace"))
        self.assertFalse(kb.is_always_present(""))


if __name__ == "__main__":
    unittest.main()
