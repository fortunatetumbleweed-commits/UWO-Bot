"""The idle lock is a state with one action and an unknown destination.

At Svear Village on 2026-08-26, with 445 Iron, 146 Matchlock Gun and 438 Candle aboard and the
barter one tap away, the game locked while the panel was opening. Perception named the screen
correctly on every single look — as `learned_on_standby_at_sea_slide_up_to_unlock`, a LEARNED
fingerprint the FSM had no node for — so nothing could route out of it. The mission
re-perceived a screen it could not act through for 16 minutes, retried, re-perceived for
another 17, and aborted at the destination it had sailed an hour to reach.

The fix is not a wake call wired into the barter path. It is: the lock is a STATE, with an
edge, and an activity whose entire repertoire is the gesture the screen names.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from brain.activities.idle_lock import IdleLockActivity
from brain.dispatcher import BLOCKED, FINISHED


class _State:
    def __init__(self, w=2400, h=1080):
        class _F:
            width, height = w, h
        self.state = "idle_lock"
        self.frame = _F()


class ItIsAFirstClassState(unittest.TestCase):

    def setUp(self):
        from brain.fsm_registry import get_fsm_registry
        self.fsm = get_fsm_registry()

    def test_the_fsm_has_a_node_for_it(self):
        """A learned fingerprint has no node, which is why nothing could route out."""
        self.assertIn("idle_lock", self.fsm.states)

    def test_it_has_an_exit(self):
        raw = json.loads(Path("memory/knowledge/fsm/states.json").read_text())
        lock = next(e for e in raw if e["id"] == "idle_lock")
        self.assertEqual([x["action"] for x in lock["exits"]], ["swipe_up"])

    def test_its_destination_is_declared_unknown(self):
        """`to: null`, the same shape as `loading` — the swipe returns you to wherever you
        were, and that is discovered by looking, never remembered."""
        raw = json.loads(Path("memory/knowledge/fsm/states.json").read_text())
        lock = next(e for e in raw if e["id"] == "idle_lock")
        self.assertIsNone(lock["exits"][0]["to"])


class TheActivityDoesOneThing(unittest.TestCase):

    def _run(self, *, swipe=None):
        calls = []
        act = IdleLockActivity(swipe=swipe or (lambda *a: calls.append(a)),
                               settle=lambda *_: None)
        return act.work(goal=None, state=_State()), calls

    def test_it_swipes_up(self):
        result, calls = self._run()
        self.assertEqual(result.status, FINISHED)
        (x1, y1, x2, y2, _ms), = calls
        self.assertLess(y2, y1, "the gesture is UP — the prompt says 'slide up'")
        self.assertEqual(x1, x2, "and vertical")

    def test_it_does_not_report_where_the_bot_landed(self):
        """It has no idea, and guessing would hand the task runner a belief dressed as an
        observation. The dispatcher re-perceives after every activity; that is where the
        destination comes from."""
        result, _calls = self._run()
        self.assertNotIn("state", result.observed)
        self.assertNotIn("location", result.observed)
        self.assertNotIn("port", result.observed)

    def test_the_goal_does_not_change_what_it_does(self):
        """One action means nothing to choose: no goal makes a different gesture correct."""
        seen = []
        act = IdleLockActivity(swipe=lambda *a: seen.append(a), settle=lambda *_: None)
        for goal in (None, "barter(Birch Tree)", "sail_to(Lisboa)", "buy(Iron, 500)"):
            act.work(goal=goal, state=_State())
        self.assertEqual(len(set(seen)), 1, f"the gesture varied with the goal: {seen}")

    def test_a_failed_gesture_is_reported_not_retried(self):
        def _boom(*_a):
            raise RuntimeError("adb: device offline")
        result, _ = self._run(swipe=_boom)
        self.assertEqual(result.status, BLOCKED)
        self.assertIn("device offline", result.detail)

    def test_the_swipe_rides_the_frame_size(self):
        """Normalised coordinates, so it survives resolution and the camera-notch shift."""
        calls = []
        act = IdleLockActivity(swipe=lambda *a: calls.append(a), settle=lambda *_: None)
        act.work(goal=None, state=_State(w=1200, h=540))
        (x1, y1, _x2, _y2, _ms), = calls
        self.assertLess(x1, 1200)
        self.assertLess(y1, 540)


class NobodyElseNeedsToKnowAboutLocks(unittest.TestCase):
    """The whole point of making it a state. Proposing to 'wire the wake into the barter path'
    was the per-path habit this architecture exists to delete."""

    def test_the_barter_mission_gains_no_lock_handling(self):
        src = Path("brain/barter_mission_live.py").read_text().lower()
        for word in ("slide up", "unlock", "idle_lock", "_wake_if_locked"):
            self.assertNotIn(word, src, f"barter code should know nothing of {word!r}")


class UnknownDestinationIsNotABrokenGraph(unittest.TestCase):
    """BFS cannot build a path through an edge with no far end, and for these states that is
    the design. Reporting them as missing recovery paths reads as a defect to every future
    reader — and buries any state that IS unreachable by mistake among them."""

    def setUp(self):
        from brain.fsm_registry import get_fsm_registry
        self.fsm = get_fsm_registry()

    def test_the_lock_has_no_route_and_that_is_correct(self):
        self.assertEqual(self.fsm.recovery_path("idle_lock"), [])

    def test_it_keeps_company_with_loading(self):
        """The same shape for the same reason: act, then look to find out where you are."""
        for sid in ("loading", "idle_lock"):
            with self.subTest(state=sid):
                self.assertTrue(any(t.to is None for t in self.fsm.states[sid].exits))

    def test_every_other_state_still_routes_home(self):
        """The audit must still catch a state that is unreachable BY MISTAKE."""
        for sid, st in self.fsm.states.items():
            if any(t.to is None for t in st.exits) or sid == "port_overworld":
                continue
            with self.subTest(state=sid):
                self.assertTrue(self.fsm.recovery_path(sid),
                                f"{sid} lost its route to port_overworld")
