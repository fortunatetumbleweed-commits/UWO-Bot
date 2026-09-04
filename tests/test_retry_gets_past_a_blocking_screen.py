"""Retrying while an activity is still open is not a retry.

An activity has exactly two exits, as Android's do: `finish()`, which ends it and names no
successor, and `startActivity(intent)`, which names one. In the FSM those are `"to": null` and
`"to": "<state>"`. `idle_lock` and `loading` are the states whose ONLY exit is a finish.

At Svear Village on 2026-08-26 the barter executor failed, `_run_with_retry` re-ran it, and it
failed again — both attempts against the game's standby lock, on which nothing can be done at
all. The mission then aborted with 445 Iron, 146 Matchlock Gun and 438 Candle aboard and the
barter one tap away.

No work can happen on such a screen, so an executor re-run lands on the same wall every time.

Note what this is NOT: lock handling. No state is named in the mission runner. And it is
INTERIM — under the dispatcher an activity finishing IS the loop, so this hook disappears
entirely. Naming it after the general rule rather than after the lock is what will make that
deletion obvious.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain.mission import SubTask, _run_with_retry


def _task():
    return SubTask("barter", "barter", "Svear Village")


class BetweenAttempts(unittest.TestCase):

    def _run(self, results, *, acted="swipe_up", max_retries=1):
        calls = []

        def ex(task):
            calls.append(len(calls))
            return results[min(len(calls) - 1, len(results) - 1)]

        with patch("brain.nav_step.finish_current_activity", return_value=acted) as leave:
            out = _run_with_retry(_task(), {"barter": ex}, max_retries)
        return out, calls, leave

    def test_a_blocking_screen_is_cleared_before_the_next_attempt(self):
        out, calls, leave = self._run([{"ok": False, "reason": "panel not open"},
                                       {"ok": True, "committed": 2}])
        self.assertTrue(out["ok"])
        self.assertEqual(len(calls), 2)
        leave.assert_called_once()

    def test_it_is_not_called_after_the_final_attempt(self):
        """There is no next try to prepare for, so acting on the screen would be gratuitous."""
        _out, calls, leave = self._run([{"ok": False, "reason": "nope"}], max_retries=0)
        self.assertEqual(len(calls), 1)
        leave.assert_not_called()

    def test_success_first_time_touches_nothing(self):
        _out, calls, leave = self._run([{"ok": True}])
        self.assertEqual(len(calls), 1)
        leave.assert_not_called()

    def test_an_ordinary_failure_still_retries(self):
        """When this screen's activity does not end by finishing, `finish_current_activity`
        returns None and the retry proceeds exactly as before — the failure was real."""
        out, calls, _leave = self._run([{"ok": False, "reason": "sold out"}], acted=None)
        self.assertFalse(out["ok"])
        self.assertEqual(len(calls), 2)

    def test_the_mission_runner_names_no_state(self):
        """The point of putting it here. A mission runner that knows what a lock is has taken
        on knowledge that belongs to the state machine."""
        import pathlib
        src = pathlib.Path("brain/mission.py").read_text().lower()
        for word in ("idle_lock", "slide up", "unlock", "swipe_up"):
            self.assertNotIn(word, src, f"mission.py should not mention {word!r}")
