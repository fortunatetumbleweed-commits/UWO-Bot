"""The orientation guard must not lock the sensor off before the display is correct.

The game is landscape-locked but follows the SENSOR, so Android's rotation setting cannot
force it — `adb shell settings put system user_rotation 3` plus a full
overworld->world-map->overworld round trip left the display at ROTATION_90 (live
2026-08-25). Only the sensor moves it.

So a guard that locks auto-rotate off FIRST and checks afterwards disables the one mechanism
that could fix a mismatch, then asks for a physical rotation that can no longer take effect.
The phone had been in the right position the whole time; enabling the sensor moved it to 270
at once. Lock only once canonical.
"""

from __future__ import annotations

import unittest
from unittest import mock

from actions import orientation


class TheGuardLocksOnlyWhenItIsSafeTo(unittest.TestCase):

    def _run(self, rotations, autorotate_on):
        """Drive the guard with a scripted sequence of rotation readings."""
        seen = {"unlocked": False, "locked": False}
        rots = list(rotations)

        def _get_rotation():
            return rots.pop(0) if len(rots) > 1 else rots[0]

        def _unlock():
            seen["unlocked"] = True

        def _lock():
            seen["locked"] = True

        with mock.patch.object(orientation, "get_display_rotation", _get_rotation), \
             mock.patch.object(orientation, "is_autorotate_on", lambda: autorotate_on), \
             mock.patch.object(orientation, "unlock_autorotate", _unlock), \
             mock.patch.object(orientation, "lock_autorotate", _lock), \
             mock.patch.object(orientation.time, "sleep", lambda _s: None):
            ok = orientation.ensure_canonical_orientation(raise_on_mismatch=False)
        return ok, seen

    def test_a_wrong_rotation_with_the_sensor_off_hands_it_back(self):
        """The display cannot move while the sensor is disabled — give it back first."""
        ok, seen = self._run([90, 90], autorotate_on=False)
        self.assertTrue(seen["unlocked"], "the sensor was never re-enabled, so a physical "
                                          "rotation could not have taken effect")
        self.assertFalse(ok)

    def test_re_enabling_the_sensor_can_resolve_it(self):
        """Exactly what happened live: the phone was already in the right position."""
        ok, seen = self._run([90, orientation.CANONICAL_ROTATION], autorotate_on=False)
        self.assertTrue(seen["unlocked"])
        self.assertTrue(ok, "the sensor reported the canonical rotation once asked")

    def test_it_locks_once_canonical(self):
        """Freezing the CORRECT orientation is the point of the lock."""
        ok, seen = self._run([orientation.CANONICAL_ROTATION], autorotate_on=True)
        self.assertTrue(ok)
        self.assertTrue(seen["locked"], "a correct orientation should be pinned")

    def test_it_does_not_lock_a_wrong_orientation(self):
        ok, seen = self._run([90, 90], autorotate_on=True)
        self.assertFalse(ok)
        self.assertFalse(seen["locked"], "locking here freezes the WRONG rotation")


if __name__ == "__main__":
    unittest.main()
