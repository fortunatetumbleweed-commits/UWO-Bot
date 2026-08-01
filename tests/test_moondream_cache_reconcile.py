"""Tests for the post-classification Moondream cache reconciliation.

Catches the case described by the user: Moondream may have been called
during a transition frame and cached the WRONG family.  Subsequent
ticks where the rule-based classifier produces a contradicting answer
must invalidate the cache so the next ambiguous frame re-probes.
"""
import unittest

from brain import moondream_family_cache as mfc
from brain.perceive import (
    _location_to_family,
    _reconcile_moondream_family_cache,
)


class LocationToFamilyTests(unittest.TestCase):

    def test_in_town_states(self):
        for loc in (
            "port_overworld", "building", "sub_menu",
            "port_map", "world_map", "main_menu",
        ):
            self.assertEqual(_location_to_family(loc), "in_town",
                             f"{loc!r} should map to in_town")

    def test_sea_maps_to_at_sea(self):
        self.assertEqual(_location_to_family("sea"), "at_sea")

    def test_uncertain_states_return_none(self):
        for loc in ("loading", "pending", "unknown", "sea_cinematic",
                    "anything_else"):
            self.assertIsNone(_location_to_family(loc),
                              f"{loc!r} must NOT map to a family")


class ReconcileTests(unittest.TestCase):

    def setUp(self):
        mfc.reset()

    def test_no_cache_no_action(self):
        """When the cache is cold, reconcile must not fail."""
        _reconcile_moondream_family_cache("port_overworld")
        self.assertIsNone(mfc.get_family())   # still cold

    def test_in_town_classifier_with_at_sea_cache_invalidates(self):
        """The exact case the user raised: Moondream cached 'at_sea'
        from a transition frame, rule-based classifier later detects
        building chrome — must invalidate the cache."""
        mfc.set_family("at_sea")
        _reconcile_moondream_family_cache("building")
        self.assertIsNone(mfc.get_family())
        self.assertEqual(mfc.snapshot()["invalid_reason"],
                         "classifier_mismatch:building")

    def test_at_sea_classifier_with_in_town_cache_invalidates(self):
        """Symmetric case — sea HUD detected, cache says in_town."""
        mfc.set_family("in_town")
        _reconcile_moondream_family_cache("sea")
        self.assertIsNone(mfc.get_family())

    def test_matching_family_preserves_cache(self):
        """Classifier agrees with cache → cache untouched."""
        mfc.set_family("in_town")
        _reconcile_moondream_family_cache("port_overworld")
        self.assertEqual(mfc.get_family(), "in_town")

    def test_loading_does_not_invalidate(self):
        """Loading screens are transient — must not disturb a recent verdict."""
        mfc.set_family("at_sea")
        _reconcile_moondream_family_cache("loading")
        self.assertEqual(mfc.get_family(), "at_sea")

    def test_sea_cinematic_does_not_invalidate(self):
        """sea_cinematic is the idle/arrival ambiguous state — uncertain."""
        mfc.set_family("at_sea")
        _reconcile_moondream_family_cache("sea_cinematic")
        self.assertEqual(mfc.get_family(), "at_sea")

    def test_world_map_aligns_with_in_town(self):
        """The bot opens the world map FROM a port; family stays in_town."""
        mfc.set_family("in_town")
        _reconcile_moondream_family_cache("world_map")
        self.assertEqual(mfc.get_family(), "in_town")


if __name__ == "__main__":
    unittest.main()
