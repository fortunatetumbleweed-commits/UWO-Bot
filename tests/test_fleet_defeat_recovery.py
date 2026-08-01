"""Tests for the fleet-defeat dialog dispatch in brain.recovery."""
import unittest
from unittest.mock import patch, MagicMock


class FleetDefeatDispatchTests(unittest.TestCase):

    def _fake_frame(self):
        f = MagicMock(); f.width = 2400; f.height = 1080; f.size = (2400, 1080)
        return f

    def test_dispatches_when_interruptor_registered(self):
        """When the learned_quick_revive_port_return state is detected and
        the fleet_death_recovery_screen interruptor exists in the registry,
        _dismiss_fleet_defeat_dialog calls _dismiss_interruptor.
        """
        from brain import recovery as R
        fake_interruptor = MagicMock()
        fake_interruptor._raw = {
            "dismissal": "tap(1800,120)_then_wait_1s_then_tap_port return_then_wait_2s_then_tap_ok"
        }
        fake_registry = MagicMock()
        fake_registry.interruptors = {"fleet_death_recovery_screen": fake_interruptor}
        with patch("brain.fsm_registry.get_fsm_registry",
                   return_value=fake_registry), \
             patch("brain.perceive._dismiss_interruptor") as mock_dispatch:
            ok = R._dismiss_fleet_defeat_dialog(self._fake_frame())
            self.assertTrue(ok)
            mock_dispatch.assert_called_once()
            # Verify dispatched with the right iid
            args, _ = mock_dispatch.call_args
            self.assertEqual(args[0], "fleet_death_recovery_screen")

    def test_returns_false_when_interruptor_missing(self):
        """If interruptors.json doesn't have the entry, return False so
        caller falls back to blind Back/Home."""
        from brain import recovery as R
        fake_registry = MagicMock()
        fake_registry.interruptors = {}   # no fleet_death_recovery_screen
        with patch("brain.fsm_registry.get_fsm_registry",
                   return_value=fake_registry), \
             patch("brain.perceive._dismiss_interruptor") as mock_dispatch:
            ok = R._dismiss_fleet_defeat_dialog(self._fake_frame())
            self.assertFalse(ok)
            mock_dispatch.assert_not_called()

    def test_swallows_import_errors_gracefully(self):
        """A registry-related ImportError must not crash recovery."""
        from brain import recovery as R
        with patch("brain.fsm_registry.get_fsm_registry",
                   side_effect=ImportError("nope")):
            ok = R._dismiss_fleet_defeat_dialog(self._fake_frame())
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
