"""One barter round, one capture per action — not three reads of an unchanged panel.

MEASURED 2026-08-30 at Hutu Village: `read_barter_panel` was called 17 times and produced 7
distinct readings. Per round the panel was read three times with identical values — once by
`_on_ready`, once as the commit's `before`, once as its `after` — each a capture (1.70 s) plus
a parse (2.90 s), to re-learn a screen nobody had touched.

The repository fixes this by OWNERSHIP rather than by discipline: a reader takes the frame the
repository holds, and only an ACTION makes it capture again. So the count of captures in a
round is the count of things that changed the screen, which is what it should always have been.
"""
import unittest

from actions import adb_actions
from actions.perception import reset_for_tests, screen

_real_tap = adb_actions.tap          # bound before conftest swaps it for a recorder


class _Frame:
    def __init__(self, n): self.n = n
    def crop(self, box): return f"crop{self.n}"


class ReadingDoesNotCapture(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self.addCleanup(reset_for_tests)
        self.captures = {"n": 0}

        def capture():
            self.captures["n"] += 1
            return _Frame(self.captures["n"])

        # Stand in for the device, leaving the real invalidation wiring intact.
        screen()._capture_fn = capture
        screen()._parse_fn = lambda f: [f"el{f.n}"]
        screen()._ocr_fn = lambda img: str(img)
        screen()._sleep = lambda s: None

    def test_many_readers_one_capture(self):
        screen().get(); screen().get()
        screen().elements(); screen().elements()
        screen().read_region((0, 0, 1, 1))
        self.assertEqual(self.captures["n"], 1,
                         "five reads of an unchanged screen cost one capture")

    def test_an_action_is_what_forces_the_next_one(self):
        from unittest.mock import patch
        screen().get()
        self.assertEqual(self.captures["n"], 1)
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real_tap(10, 10)
        screen().get()
        self.assertEqual(self.captures["n"], 2, "the tap moved the screen, so we look again")
        screen().get(); screen().get()
        self.assertEqual(self.captures["n"], 2, "and nothing else does")

    def test_a_round_costs_one_capture_per_action(self):
        """Two taps in a round — Exchange, then the confirm — so two captures, not six."""
        from unittest.mock import patch
        screen().get()                                   # read the panel
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real_tap(2100, 996)                         # Exchange
            screen().get()                               # read back
            _real_tap(1313, 832)                         # the confirm
            screen().get()                               # read back
        screen().elements()                              # the shortfall log, the next check
        screen().read_region((0, 0, 5, 5))
        self.assertEqual(self.captures["n"], 3,
                         "one to look, one per tap — the rest are served from what is held")


if __name__ == "__main__":
    unittest.main()


class TheCallerExpectsTheChange(unittest.TestCase):
    """The repository does not model the world; the CALLER states its expectation.

    Our own actions are a FACT the repository owns — we moved the screen, and the action layer
    says so without anyone remembering to. Whether the GAME moved is not knowable here: an
    arrival, a notice, a reward dialog, an idle lock all happen with nobody tapping.

    So exactly two callers hold that expectation and say so at the point they hold it: a
    sub-loop that has just acted, and the dispatcher regaining control to take a fresh look.
    Anything else asking would be guessing at the world on the repository's behalf.
    """

    def setUp(self):
        reset_for_tests()
        self.addCleanup(reset_for_tests)
        self.captures = {"n": 0}
        screen()._capture_fn = lambda: _Frame(self.captures.__setitem__("n", self.captures["n"] + 1) or self.captures["n"])
        screen()._parse_fn = lambda f: []
        screen()._ocr_fn = lambda i: ""
        screen()._sleep = lambda s: None

    def test_a_caller_can_ask_for_a_fresh_look(self):
        screen().get()
        self.assertEqual(self.captures["n"], 1)
        screen().expect_changed("the dispatcher is taking a fresh look")
        screen().get()
        self.assertEqual(self.captures["n"], 2, "it stated the expectation, so we look again")

    def test_every_step_takes_a_fresh_look(self):
        """The dispatcher regaining control IS the caller that expects change — and that is
        EVERY step, not only the ones after a wait.

        Narrowing it to "after a wait" broke a live run within minutes (2026-08-31): the
        in-flight guard declined to re-dispatch because "the screen has not changed", so no
        action was taken, so nothing invalidated, so the next look returned the same held
        frame — and "unchanged" became self-fulfilling. A daily-news popup sat on screen that
        the bot could not see.

        It costs nothing extra: `perceive` reads THROUGH the repository, so a step captures
        once and every reader in that step shares it.
        """
        import inspect
        from brain import dispatcher
        self.assertIn("expect_changed", inspect.getsource(dispatcher.Dispatcher.step))

    def test_the_wait_only_waits(self):
        """The waiting and the expectation are separate jobs: the wake timer decides WHEN the
        next step happens, and the step itself decides that it wants a fresh look."""
        import inspect
        from brain import dispatcher
        waiting = inspect.getsource(dispatcher.Dispatcher._wait_out_the_wake_timer)
        self.assertIn("_sleep_jittered", waiting)
        self.assertNotIn("expect_changed", waiting, "stated once, in step()")
