"""A wake-timer sleep invalidates the look that preceded it.

Live 2026-09-04, sailing to San Village (trace_barter_cmd_2026-09-04T17-35-01):

    18:28:50  supply=15d eta=11d, 1 reading  -> next look in 0.5 min
    18:29:31  supply=15d eta=7d,  2 readings -> next look in 0.3 min
    18:29:59  supply=15d eta=6d,  3 readings -> next look in 9.0 min
    18:38:17  supply=415d eta=6d, 4 readings -> next look in 9.0 min

The ETA did not move across 8.3 minutes of sailing. It could not: the fourth reading WAS
the third. `_fresh` is captured at the END of a step, BEFORE the wait, and the next step
reused it rather than looking again — so the frame behind the 18:38 line was captured at
18:30:00.

Two consequences, and the second is why it never settled:
  * the fleet is a whole sleep behind, so it notices arrival a sleep late;
  * the NEXT sleep is computed from that same stale ETA, which therefore never falls, so it
    re-books the identical 9 minutes indefinitely.

At sea it could not self-correct. `_fresh` was dropped only after an INTENT was dispatched
and a sea tick dispatches none, so the one path that cleared it was the one path sea never
took. CLAUDE.md carried this as STILL OPEN.
"""
from __future__ import annotations

import unittest
from unittest import mock

from brain.dispatcher import Dispatcher


class ASleepSpendsTheLookBeforeIt(unittest.TestCase):

    def _dispatcher(self):
        d = Dispatcher.__new__(Dispatcher)     # no device, no activities
        d._wake_at = 0.0
        d._wake_why = "at sea"
        d._fresh = "THE PRE-SLEEP LOOK"
        return d

    def test_a_wait_that_actually_slept_reports_True(self):
        import time as _t
        d = self._dispatcher()
        d._wake_at = _t.monotonic() + 30.0
        with mock.patch("brain.run_goal._sleep_jittered") as slept:
            self.assertIs(d._wait_out_the_wake_timer(), True)
        slept.assert_called_once()

    def test_no_timer_set_is_not_a_sleep(self):
        d = self._dispatcher()
        d._wake_at = 0.0
        self.assertIs(d._wait_out_the_wake_timer(), False)

    def test_an_ELAPSED_timer_is_not_a_sleep(self):
        """Woken by something else, or late to our own appointment: nothing was waited out,
        so the look in hand is still current and must NOT be thrown away."""
        import time as _t
        d = self._dispatcher()
        d._wake_at = _t.monotonic() - 5.0
        with mock.patch("brain.run_goal._sleep_jittered") as slept:
            self.assertIs(d._wait_out_the_wake_timer(), False)
        slept.assert_not_called()

    def test_the_timer_is_cleared_so_it_cannot_be_waited_twice(self):
        import time as _t
        d = self._dispatcher()
        d._wake_at = _t.monotonic() + 30.0
        with mock.patch("brain.run_goal._sleep_jittered"):
            d._wait_out_the_wake_timer()
        self.assertEqual(d._wake_at, 0.0)
        with mock.patch("brain.run_goal._sleep_jittered") as again:
            self.assertIs(d._wait_out_the_wake_timer(), False)
        again.assert_not_called()

    def test_THE_BUG_the_stale_look_is_dropped_after_a_sleep(self):
        """`step` drops `_fresh` when the wait returns True. Asserted here on the contract
        the two sides share, since `step` itself needs a live screen to run."""
        import time as _t
        d = self._dispatcher()
        d._wake_at = _t.monotonic() + 540.0          # the 9 minutes from the live run
        with mock.patch("brain.run_goal._sleep_jittered"):
            slept = d._wait_out_the_wake_timer()
        if slept:
            d._fresh = None
        self.assertIsNone(d._fresh, "the step after a sleep would decide a 9-minute-old world")

    def test_a_look_SURVIVES_a_step_that_did_not_sleep(self):
        """The reuse is deliberate and pays for itself — one capture per step, not two. Only
        a real wait spends it."""
        d = self._dispatcher()
        slept = d._wait_out_the_wake_timer()
        if slept:
            d._fresh = None
        self.assertEqual(d._fresh, "THE PRE-SLEEP LOOK")


if __name__ == "__main__":
    unittest.main()
