"""Every action tells the observers the screen has changed. Nobody has to remember to.

This is the second of the perceive repository's two hooks, and without it the repository is
WORSE than what it replaces: a caller that reads without invalidating gets a stale frame,
where today it would at least have captured a fresh one. That would trade a visible cost for
an invisible correctness bug.

It works because `actions/adb_actions.py` is a genuine choke point — every tap, swipe, back,
wake and keystroke goes through it, and "never raw `input tap`" is already enforced by
convention across the codebase. So invalidation can be structural rather than voluntary,
which is the difference between this and the 215 capture sites that grew when the discipline
was left to each call site.
"""
import unittest
from unittest.mock import patch

from actions import adb_actions

# Bound at IMPORT, before conftest's autouse fixture swaps these nine primitives for
# recorders. That stubbing is right — the suite once drove a real `press_back()` into the
# "Exit Game?" dialog — but it means `adb_actions.tap` resolves to a recorder at call time,
# and these tests are about what the REAL primitive does. Their `_adb` is still conftest's,
# so nothing reaches a device.
#
# NOTE for the migration: the recorders do NOT call `_acted`, so a test driving a goal
# through them will not invalidate the repository. Once callers read from the repository,
# either the recorders report too or those tests will see a stale observation.
_real = {name: getattr(adb_actions, name)
         for name in ("tap", "press_back", "wake", "swipe")}


class EveryActionReports(unittest.TestCase):
    def setUp(self):
        self.seen = []
        adb_actions.add_action_sink(self._note)
        self.addCleanup(adb_actions.remove_action_sink, self._note)

    def _note(self, kind, settle_s):
        self.seen.append((kind, settle_s))

    def test_a_tap_reports(self):
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real["tap"](100, 200)
        self.assertEqual([k for k, _ in self.seen], ["tap"])

    def test_back_and_wake_report(self):
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real["press_back"]()
            _real["wake"]()
        self.assertEqual([k for k, _ in self.seen], ["press_back", "wake"])

    def test_a_swipe_reports(self):
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real["swipe"](0, 0, 10, 10)
        self.assertEqual([k for k, _ in self.seen], ["swipe"])

    def test_each_carries_a_settle(self):
        """A capture straight after a tap is mid-animation — worse than none. The primitive
        knows its own dwell, which is what the settle waits scattered through actions/ are
        really expressing."""
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real["tap"](1, 1)
        self.assertGreater(self.seen[0][1], 0.0)


class TheHookIsSafe(unittest.TestCase):
    def test_several_observers_are_all_told(self):
        """A LIST, not one slot: a tracer and the repository both want to know, and a single
        global would have them fight over it."""
        a, b = [], []
        fa, fb = (lambda k, s: a.append(k)), (lambda k, s: b.append(k))
        adb_actions.add_action_sink(fa); adb_actions.add_action_sink(fb)
        self.addCleanup(adb_actions.remove_action_sink, fa)
        self.addCleanup(adb_actions.remove_action_sink, fb)
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real["tap"](1, 1)
        self.assertEqual((a, b), (["tap"], ["tap"]))

    def test_a_broken_observer_never_costs_the_action(self):
        def boom(kind, settle_s):
            raise RuntimeError("observer exploded")
        adb_actions.add_action_sink(boom)
        self.addCleanup(adb_actions.remove_action_sink, boom)
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real["tap"](1, 1)      # must not raise

    def test_registering_twice_reports_once(self):
        seen = []
        fn = lambda k, s: seen.append(k)
        adb_actions.add_action_sink(fn); adb_actions.add_action_sink(fn)
        self.addCleanup(adb_actions.remove_action_sink, fn)
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real["tap"](1, 1)
        self.assertEqual(seen, ["tap"])


class ItInvalidatesTheRepository(unittest.TestCase):
    def test_a_tap_supersedes_what_was_observed(self):
        from vision.perceive_repository import PerceiveRepository, Stale

        made = {"n": 0}
        def capture():
            made["n"] += 1
            class _F:
                def crop(self, box): return "crop"
            return _F()

        repo = PerceiveRepository(capture_fn=capture, parse_fn=lambda f: [],
                                  ocr_fn=lambda i: "", clock=lambda: 0.0,
                                  sleep_fn=lambda s: None)
        sink = lambda kind, settle_s: repo.invalidate(kind, settle_s=settle_s)
        adb_actions.add_action_sink(sink)
        self.addCleanup(adb_actions.remove_action_sink, sink)

        repo.get()
        self.assertEqual(repo.generation(), 1)
        with patch.object(adb_actions, "_adb"), patch.object(adb_actions, "_human_delay"):
            _real["tap"](5, 5)
        # The screen moved because WE moved it: reading a region now is a caller that acted
        # and did not look again.
        with self.assertRaises(Stale):
            repo.read_region((0, 0, 1, 1))
        self.assertEqual(repo.get().generation, 2, "and get() takes the new one")


if __name__ == "__main__":
    unittest.main()
