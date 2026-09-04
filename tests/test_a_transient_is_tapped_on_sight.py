"""A transient is tapped as soon as it is seen, and waiting is never better.

The CNN groups two screens under `transient` and cannot separate them:

  * a NOTICE — promotion, level-up, reward — which waits for a gesture;
  * a TRANSITION — departure, arrival — which resolves itself.

That looks like a reason to hesitate, and on 2026-09-01 a "look again before tapping" was
built here on the theory that a transition has nothing to dismiss. The user corrected it, and
the correction is the useful thing to write down:

    a tap will not do harm on a transient screen, the arrival transient can be sped up if
    it taps, but if it does nothing it still transits to port world

So neither answer favours waiting. On a notice the tap IS the job; on the arrival transition
the tap makes it quicker, and not tapping still arrives. Waiting only spends time on a
question whose two answers end the same way — which is why it was removed again the same day.

What the earlier run DID show is separate and lives in the dispatcher: while a notice covered
the world, `SailRunner` spent a departure attempt on a tick where nothing could have worked
("departing for 'Svear Village' (2/2)"). The fix for that was to stop judging a covering
screen as if it were a world, not to stop tapping it.
"""
import types
import unittest

from brain.activities.transient import TransientActivity


def _state():
    return types.SimpleNamespace(frame=types.SimpleNamespace(size=(2400, 1080)))


def _act():
    taps = []
    return TransientActivity(tap=lambda x, y: taps.append((x, y)),
                             settle=lambda k: None), taps


class ItTapsImmediately(unittest.TestCase):
    def test_the_first_sighting_is_tapped(self):
        a, taps = _act()
        res = a.work(None, _state())
        self.assertEqual(len(taps), 1, "waiting gains nothing on either kind of transient")
        self.assertEqual(res.observed.get("gesture"), "tap")

    def test_it_does_not_ask_to_be_looked_at_again(self):
        a, _taps = _act()
        self.assertNotIn("checkback_s", a.work(None, _state()).observed,
                         "a pause here only delays a tap that is coming anyway")


class ButNotWhereverItLikes(unittest.TestCase):
    def test_the_dead_zone_not_the_centre(self):
        """Reward tiles and portraits sit in the middle, and some of them navigate."""
        a, taps = _act()
        a.work(None, _state())
        x, y = taps[0]
        self.assertLess(x, 2400 * 0.35, "upper-left quadrant")
        self.assertLess(y, 1080 * 0.35)


class ItClearsAScreenRatherThanFillingAnOrder(unittest.TestCase):
    def test_the_marker_is_declared(self):
        """`CLEARS_SCREEN` is what stops its FINISHED retiring somebody's work order, and
        what the dispatcher now reads to know this screen must not be judged as a world."""
        self.assertTrue(TransientActivity.CLEARS_SCREEN)


if __name__ == "__main__":
    unittest.main()
