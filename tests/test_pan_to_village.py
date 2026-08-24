"""Tests for the village-navigation slice.

Covers:
  - `make_village_navigator()` — produces a WorldMapNavigator over the
    village catalogue (not the port catalogue).
  - `pan_to_village()` — switches to Explore tab, then delegates to the
    navigator with village data.  Returns None when tab-switch fails.
"""
import unittest
from unittest.mock import patch, MagicMock

from actions.world_map_nav import WorldMapNavigator, make_village_navigator


class VillageNavigatorTests(unittest.TestCase):

    def test_factory_loads_village_catalogue(self):
        # Real catalogue file should exist after the bake step.
        nav = make_village_navigator()
        self.assertIsInstance(nav, WorldMapNavigator)
        # Sanity: catalogue has the expected shape.
        self.assertGreater(len(nav._ports), 0,
            "village catalogue should be non-empty")
        # Each entry has x/y (same schema as port catalogue).
        sample_key = next(iter(nav._ports))
        sample = nav._ports[sample_key]
        self.assertIn("x", sample)
        self.assertIn("y", sample)

    def test_navigator_with_explicit_catalogue(self):
        fake_catalogue = {"foo": {"name": "Foo", "x": 100, "y": 200}}
        nav = WorldMapNavigator(catalogue=fake_catalogue, aliases={})
        self.assertEqual(nav._ports, fake_catalogue)
        self.assertEqual(nav._aliases, {})

    def test_navigator_default_uses_port_catalogue(self):
        """Backwards compat: no args → port catalogue + port aliases."""
        nav = WorldMapNavigator()
        # Should have hundreds of port entries.
        self.assertGreater(len(nav._ports), 100)
        # Port aliases include known entries like "lisbon" → ["lisboa", ...]
        # We don't assert specific keys; just that aliases is non-empty.
        self.assertGreater(len(nav._aliases), 0)


class LookupPortFallbackTests(unittest.TestCase):
    """The village navigator must be able to look up the *departing*
    port — which is always a real port, not a village.  Without this
    fallback the navigator fails on attempt 1 when no villages are
    visible in the initial view (no anchor for stride / odometry).
    """

    def test_village_nav_falls_back_to_port_catalogue(self):
        vnav = make_village_navigator()
        # Tripoli is a real port (in port catalogue, not village catalogue).
        info = vnav._lookup_port("Tripoli")
        self.assertIsNotNone(info,
            "Village nav must fall back to port catalogue for departing port")
        self.assertIn("x", info)
        self.assertIn("y", info)

    def test_village_nav_finds_its_own_villages(self):
        vnav = make_village_navigator()
        info = vnav._lookup_port("Berber")
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "Berber Village")

    def test_port_nav_does_not_fall_back_to_villages(self):
        """Port nav lookup of a village should return None — we don't
        want to accidentally treat a village as a port destination."""
        pnav = WorldMapNavigator()
        # Berber is village-only; should NOT be found by port nav.
        self.assertIsNone(pnav._lookup_port("Berber"))

    def test_port_nav_finds_ports(self):
        pnav = WorldMapNavigator()
        self.assertIsNotNone(pnav._lookup_port("London"))
        self.assertIsNotNone(pnav._lookup_port("Tripoli"))

    def test_village_nav_port_alias_resolved(self):
        """Lisbon (canonical) is stored as 'lisboa' in the port catalogue
        slug; alias lookup should resolve it even via the fallback path."""
        vnav = make_village_navigator()
        info = vnav._lookup_port("Lisbon")
        self.assertIsNotNone(info)


class PanToVillageWrapperTests(unittest.TestCase):

    def test_pan_to_village_calls_select_explore_then_delegates(self):
        from actions.sail_actions import pan_to_village

        # Anchor-port typed-search mocked OFF (it drives the DEVICE — never in tests)
        # and the village lookup mocked to miss → the FALLBACK pan path runs:
        # select Explore, delegate to the village navigator.
        mock_nav = MagicMock()
        mock_nav.pan_to_port.return_value = (1234, 567)
        mock_nav._lookup_port.return_value = None    # skip the anchor-port primary path
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.5, 2.5)), \
             patch("actions.sail_actions._try_port_search",
                   return_value=None), \
             patch("actions.sail_actions.select_world_map_tab",
                   return_value=True) as mock_tab, \
             patch("actions.world_map_nav.make_village_navigator",
                   return_value=mock_nav) as mock_factory:
            pos = pan_to_village("Berber", from_port="Lisbon", max_pans=6)
        mock_tab.assert_called_once_with("explore")
        mock_factory.assert_called_once_with()
        mock_nav.pan_to_port.assert_called_once_with(
            "Berber", max_pans=6, from_port="Lisbon",
        )
        self.assertEqual(pos, (1234, 567))

    def test_pan_to_village_returns_none_when_tab_select_fails(self):
        from actions.sail_actions import pan_to_village
        mock_nav = MagicMock()
        mock_nav._lookup_port.return_value = None    # skip the anchor-port primary path
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.5, 2.5)), \
             patch("actions.sail_actions._try_port_search",
                   return_value=None), \
             patch("actions.sail_actions.select_world_map_tab",
                   return_value=False), \
             patch("actions.world_map_nav.make_village_navigator",
                   return_value=mock_nav):
            pos = pan_to_village("Berber")
        self.assertIsNone(pos)
        # If tab-switch failed, the navigator's pan must never run.
        mock_nav.pan_to_port.assert_not_called()

    def test_pan_to_village_pre_calibrates_when_no_persisted_scale(self):
        """When no scale is on disk, run one-shot calibration on Port tab
        before switching to Explore.  Verifies the call sequence.
        """
        from actions.sail_actions import pan_to_village

        mock_nav = MagicMock()
        mock_nav.pan_to_port.return_value = (100, 200)
        mock_nav._lookup_port.return_value = None    # skip the anchor-port primary path
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(None, None)), \
             patch("actions.sail_actions._try_port_search",
                   return_value=None), \
             patch("actions.sail_actions._calibrate_scale_on_port_tab",
                   return_value=True) as mock_precal, \
             patch("actions.sail_actions.select_world_map_tab",
                   return_value=True) as mock_tab, \
             patch("actions.world_map_nav.make_village_navigator",
                   return_value=mock_nav):
            pos = pan_to_village("Berber", from_port="Lisbon")
        mock_precal.assert_called_once_with()
        mock_tab.assert_called_once_with("explore")
        self.assertEqual(pos, (100, 200))

    def test_pan_to_village_skips_precalibration_when_scale_persisted(self):
        from actions.sail_actions import pan_to_village

        mock_nav = MagicMock()
        mock_nav.pan_to_port.return_value = (50, 60)
        mock_nav._lookup_port.return_value = None    # skip the anchor-port primary path
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.3, 2.4)), \
             patch("actions.sail_actions._try_port_search",
                   return_value=None), \
             patch("actions.sail_actions._calibrate_scale_on_port_tab",
                   return_value=True) as mock_precal, \
             patch("actions.sail_actions.select_world_map_tab",
                   return_value=True), \
             patch("actions.world_map_nav.make_village_navigator",
                   return_value=mock_nav):
            pan_to_village("Berber", from_port="Lisbon")
        mock_precal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
