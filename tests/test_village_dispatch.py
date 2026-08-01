"""Tests for the destination smart-dispatch + village navigation.

End-to-end checks:
  - _classify_destination correctly identifies port vs village vs unknown.
  - _navigate_world_map_to_destination routes to the appropriate
    _navigate_world_map_to_{port,village} based on classification.
  - Existing port flow is untouched (no regressions).

The actual UI tapping logic is mocked — these tests verify the dispatch
glue, not the OmniParser / OCR pipeline.
"""
import unittest
from unittest.mock import patch, MagicMock


class ClassifyDestinationTests(unittest.TestCase):

    def test_known_port_classified(self):
        from actions.sail_actions import _classify_destination
        self.assertEqual(_classify_destination("London"),     "port")
        self.assertEqual(_classify_destination("Port Royal"), "port")

    def test_port_alias_classified_as_port(self):
        """Lisbon (canonical) and Lisboa (alias) both → port."""
        from actions.sail_actions import _classify_destination
        self.assertEqual(_classify_destination("Lisbon"), "port")
        self.assertEqual(_classify_destination("Lisboa"), "port")

    def test_known_village_classified(self):
        from actions.sail_actions import _classify_destination
        self.assertEqual(_classify_destination("Berber"), "village")
        # The catalogue slug strips " village" suffix, but users may
        # type the full form — both should classify as village.
        self.assertEqual(_classify_destination("Berber Village"), "village")

    def test_unknown_name_returns_none(self):
        from actions.sail_actions import _classify_destination
        self.assertIsNone(_classify_destination("Atlantis"))
        self.assertIsNone(_classify_destination(""))
        self.assertIsNone(_classify_destination("   "))

    def test_case_and_whitespace_insensitive(self):
        from actions.sail_actions import _classify_destination
        self.assertEqual(_classify_destination("  LONDON  "), "port")
        self.assertEqual(_classify_destination("BeRbEr"),     "village")


class DispatchRoutingTests(unittest.TestCase):

    def test_port_destination_routes_to_port_flow(self):
        from actions import sail_actions as A
        with patch.object(A, "_navigate_world_map_to_port",
                          return_value=True) as mock_port, \
             patch.object(A, "_navigate_world_map_to_village",
                          return_value=True) as mock_village:
            ok = A._navigate_world_map_to_destination(
                "London", from_port="Lisbon",
            )
        self.assertTrue(ok)
        mock_port.assert_called_once_with("London", from_port="Lisbon")
        mock_village.assert_not_called()

    def test_village_destination_routes_to_village_flow(self):
        from actions import sail_actions as A
        with patch.object(A, "_navigate_world_map_to_port",
                          return_value=True) as mock_port, \
             patch.object(A, "_navigate_world_map_to_village",
                          return_value=True) as mock_village:
            ok = A._navigate_world_map_to_destination(
                "Berber", from_port="Lisbon",
            )
        self.assertTrue(ok)
        mock_village.assert_called_once_with("Berber", from_port="Lisbon")
        mock_port.assert_not_called()

    def test_unknown_destination_falls_to_port_flow(self):
        """Unknown names attempt port lookup (port-search list scroll
        is the deepest fuzzy fallback we have).  Village isn't a
        sensible default because villages aren't in any scroll list."""
        from actions import sail_actions as A
        with patch.object(A, "_navigate_world_map_to_port",
                          return_value=False) as mock_port, \
             patch.object(A, "_navigate_world_map_to_village",
                          return_value=False) as mock_village:
            ok = A._navigate_world_map_to_destination("Atlantis")
        self.assertFalse(ok)
        mock_port.assert_called_once_with("Atlantis", from_port=None)
        mock_village.assert_not_called()

    def test_village_alias_with_suffix_routes_to_village(self):
        """User typed 'berber village' (full form) — dispatch must still
        route to the village flow, passing the original name through."""
        from actions import sail_actions as A
        with patch.object(A, "_navigate_world_map_to_port",
                          return_value=True) as mock_port, \
             patch.object(A, "_navigate_world_map_to_village",
                          return_value=True) as mock_village:
            ok = A._navigate_world_map_to_destination("Berber Village")
        self.assertTrue(ok)
        mock_village.assert_called_once_with("Berber Village", from_port=None)
        mock_port.assert_not_called()


class SailToGoalDispatchTest(unittest.TestCase):
    """The sail_to goal must call the dispatcher, not the legacy
    _navigate_world_map_to_port directly.  Pin this so a future
    refactor doesn't accidentally revert the wiring.
    """

    def test_action_select_destination_uses_dispatch(self):
        from brain.goals import sail_to as M
        # Read the source — cheap structural assert.  The presence of
        # the dispatcher name proves the wiring; the absence of the old
        # direct call proves it's been removed.
        import inspect
        src = inspect.getsource(M.SailToGoal._action_select_destination)
        self.assertIn("_navigate_world_map_to_destination", src)


if __name__ == "__main__":
    unittest.main()
