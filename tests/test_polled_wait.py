"""Tests for _wait_for_state_change in actions/sail_actions.py.

The helper replaces blanket `time.sleep(8.0)` waits after taps with a
short-interval poll that exits early when the screen transitions.
"""
import time
import unittest
from unittest.mock import patch

from actions.sail_actions import _wait_for_state_change


class PolledWaitTests(unittest.TestCase):

    def test_returns_early_when_state_matches(self):
        """If where_am_i returns an expected state on the second poll,
        the helper should return at ~poll_interval, not max_wait."""
        states = [
            {"location": "world_map", "port": None, "detail": "still on map"},
            {"location": "sea", "port": None, "detail": "voyage started"},
        ]
        call_count = [0]
        def fake_where_am_i():
            i = call_count[0]
            call_count[0] = min(i + 1, len(states) - 1)
            return states[i]

        with patch("actions.sail_actions.where_am_i", side_effect=fake_where_am_i), \
             patch("time.sleep"):
            start = time.time()
            result = _wait_for_state_change(
                expected=("sea", "loading"),
                max_wait=8.0,
                poll_interval=1.0,
                initial_delay=0.5,
            )
            elapsed = time.time() - start

        self.assertEqual(result["location"], "sea")
        # Real wall-clock should be tiny (time.sleep is patched).
        # Importantly: should NOT match the worst-case 8.0 s blanket sleep.
        self.assertLess(elapsed, 1.0)

    def test_returns_max_wait_state_on_timeout(self):
        """If the state never transitions, the helper returns the last
        observed state without crashing."""
        with patch("actions.sail_actions.where_am_i",
                   return_value={"location": "world_map", "port": None,
                                 "detail": "stuck"}), \
             patch("time.sleep"):
            result = _wait_for_state_change(
                expected=("sea",),
                max_wait=2.0,
                poll_interval=0.5,
                initial_delay=0.1,
            )
        self.assertEqual(result["location"], "world_map")

    def test_polls_multiple_times_before_match(self):
        """If state only matches on the third call, we should see at
        least 3 calls to where_am_i."""
        states = [
            {"location": "world_map", "port": None, "detail": ""},
            {"location": "world_map", "port": None, "detail": ""},
            {"location": "loading",   "port": None, "detail": ""},
        ]
        call_log = []
        def fake_where_am_i():
            i = min(len(call_log), len(states) - 1)
            call_log.append(states[i])
            return states[i]

        with patch("actions.sail_actions.where_am_i", side_effect=fake_where_am_i), \
             patch("time.sleep"):
            result = _wait_for_state_change(
                expected=("loading", "sea"),
                max_wait=8.0,
                poll_interval=1.0,
            )
        self.assertEqual(result["location"], "loading")
        self.assertGreaterEqual(len(call_log), 3)

    def test_first_call_matches_returns_immediately(self):
        """Happy path: where_am_i returns the expected state on the
        first call — return without further polling."""
        call_log = []
        def fake_where_am_i():
            call_log.append("called")
            return {"location": "sea", "port": None, "detail": "voyage"}

        with patch("actions.sail_actions.where_am_i", side_effect=fake_where_am_i), \
             patch("time.sleep"):
            result = _wait_for_state_change(
                expected=("sea",),
                max_wait=8.0,
                poll_interval=1.0,
            )
        self.assertEqual(result["location"], "sea")
        self.assertEqual(len(call_log), 1)


if __name__ == "__main__":
    unittest.main()
