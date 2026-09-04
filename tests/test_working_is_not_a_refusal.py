# tests/test_working_is_not_a_refusal.py
#
# WORKING MEANS "STILL DOING IT", NOT "CANNOT" (live 2026-09-04, Svear).
#
# `BarterTaskRunner._absorb` treated any ReadBarterPanel result that was not FINISHED as a
# refusal. But an activity does ONE action per tick and reports WORKING while it is still in
# the context — the dispatcher's own contract. The barter panel opens with NOTHING selected,
# so the first tick selects the good and says `{'did': 'selected Birch Tree'}`; the panel is
# readable on the NEXT one.
#
# What the first in-progress tick cost:
#
#   09:34:03  Barter panel: good=Birch Tree out=564 materials=[('Wares'...)]   <- it ANSWERED
#   09:34:03  selected 'Birch Tree' via the 'Wares' tile
#   09:34:11  BarterTaskRunner has nothing more to ask for after 2 step(s) — status failed
#   09:34:11  the panel did not answer here (read the barter panel for 'Birch Tree')
#   09:36:26  graph: ['trim_before_gather', 'gather:Amsterdam', 'gather:Barcelona', ...]
#   FAILED at step mission: trim_before_gather: sea cannot start ENTER_BUILDING
#
# It abandoned a working barter, replanned the WHOLE mission — re-gathering three ports whose
# 962 Iron, 324 Matchlock Gun and 920 Candle were already in the hold — and died trying to
# enter a market at a village, which has none.
#
# Why it had never shown: this probe path runs only when the bot is ALREADY at the village at
# startup. That resume case was unreachable until the village-classification gate landed
# (18da694), so the first successful resume was also the first time this ran.

import unittest

from brain.dispatcher import BLOCKED, FINISHED, UNRECOGNISED, WORKING, ActivityResult


def _runner():
    from brain.barter_runner import BarterTaskRunner, HAVE_RECIPE
    r = BarterTaskRunner(village="Svear Village", good="Birch Tree")
    r.status = HAVE_RECIPE
    return r


def _pend(r):
    """Put a ReadBarterPanel in flight, as `next_goal` does."""
    from brain.activities.village import ReadBarterPanel
    r._pending = ReadBarterPanel(good="Birch Tree", village="Svear Village")
    return r


class TheFirstTickIsNotAVerdict(unittest.TestCase):
    def test_working_keeps_the_order_pending(self):
        r = _pend(_runner())
        before = r.status
        r._absorb(ActivityResult(WORKING, {"did": "selected Birch Tree"}))
        self.assertIsNotNone(r._pending, "dropped an order that was still being served")
        self.assertEqual(r.status, before, "called an in-progress tick a failure")

    def test_the_answer_on_the_NEXT_tick_is_taken(self):
        """The sequence that actually happens at a village: select, then read."""
        from brain.barter_runner import HAVE_PANEL
        r = _pend(_runner())
        r._absorb(ActivityResult(WORKING, {"did": "selected Birch Tree"}))
        r._absorb(ActivityResult(FINISHED, {"panel": {"good": "Birch Tree", "out": 564}}))
        self.assertEqual(r.status, HAVE_PANEL)
        self.assertEqual(r.panel["out"], 564)
        self.assertIsNone(r._pending)


class ATerminalStatusStillDecides(unittest.TestCase):
    """The fall-through the probe exists for must survive — a panel that genuinely will not
    read here means take the ordinary check-and-plan path."""

    def test_unrecognised_is_still_a_refusal(self):
        from brain.barter_runner import FAILED
        r = _pend(_runner())
        r._absorb(ActivityResult(UNRECOGNISED, {}, detail="no barter panel here"))
        self.assertEqual(r.status, FAILED)
        self.assertIsNone(r._pending)

    def test_blocked_is_still_a_refusal(self):
        from brain.barter_runner import FAILED
        r = _pend(_runner())
        r._absorb(ActivityResult(BLOCKED, {}, detail="the panel would not open"))
        self.assertEqual(r.status, FAILED)


if __name__ == "__main__":
    unittest.main()


class EveryRunnerAgreesOnWhatWorkingMeans(unittest.TestCase):
    """"Why do they go through different path? By observing and then action, they should go
    through the same path." (user, 2026-09-04)

    The ACTIVITY layer already does: `Barter` and `ReadBarterPanel` share every handler and
    differ only in the last step ("a read ends where a barter would act"). The split was in
    the TASK layer, where four runners re-derived what an ActivityResult status means at
    seven separate sites over a vocabulary the dispatcher defines:

        mission_runner   correct, and documented in a comment
        barter_runner    wrong for eight days; failed the first time its path was reachable
        sail_runner      logged "the course did not take" on every in-progress tick
        voyage_runner    safe by omission

    `brain.dispatcher.still_working` is now the one statement of it. Deliberately narrow:
    what FINISHED means, and whether UNRECOGNISED is "still routing" or "not here", really do
    differ per runner and per goal, and those stay put."""

    def test_only_working_counts_as_still_being_served(self):
        from brain.dispatcher import still_working
        self.assertTrue(still_working(ActivityResult(WORKING, {})))
        for terminal in (FINISHED, BLOCKED, UNRECOGNISED):
            with self.subTest(status=terminal):
                self.assertFalse(still_working(ActivityResult(terminal, {})))

    def test_it_tolerates_a_result_that_is_not_one(self):
        """Runners call this on whatever the dispatcher handed back, including None on the
        first consult."""
        from brain.dispatcher import still_working
        self.assertFalse(still_working(None))
        self.assertFalse(still_working(object()))

    def test_the_runners_ask_it_rather_than_re_deriving(self):
        """Pinned in the source: a runner that goes back to comparing the enum itself is how
        this diverged in the first place."""
        import inspect
        from brain import barter_runner, sail_runner
        for mod in (barter_runner, sail_runner):
            with self.subTest(module=mod.__name__):
                self.assertIn("still_working", inspect.getsource(mod))

    def test_a_course_being_set_is_not_a_course_refused(self):
        """sail_runner's version: the world map takes several actions to set a course and
        says WORKING through all of them."""
        import inspect
        from brain import sail_runner
        src = inspect.getsource(sail_runner)
        i_guard = src.index("if still_working(result):")
        i_verdict = src.index("self._course_set = status == FINISHED")
        self.assertLess(i_guard, i_verdict,
                        "the in-progress guard must come before the verdict")
