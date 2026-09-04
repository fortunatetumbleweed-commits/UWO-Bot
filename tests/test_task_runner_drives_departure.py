"""The task runner drives departure — the primitive taps and hands back.

Live 2026-08-25, London -> Amsterdam: `SailToGoal.tick()` called
`depart_from_port_via_world_map` ONCE at 16:34 and did not regain control until 16:41. Inside
that single call the primitive decided the departure had failed, walked to the harbour, hunted
a Depart button, tapped Supply Departure and re-selected the destination three times — while
perception was correctly reporting `port_overworld / Amsterdam` to nobody who could act on it.
The fleet had already ARRIVED; one tick of the task runner would have seen it.

The rule, from the project's own KB: one loop owns the sequence, primitives never force a
state. `commit_departure` therefore returns the moment the destination is committed, and says
nothing about whether the fleet moved — that is a fact about the world, read by the next
perceive and judged by the goal.
"""

from __future__ import annotations

import unittest
from unittest import mock

from actions import sail_actions


class CommitDepartureIsOneAction(unittest.TestCase):

    def _commit(self, *, map_ok=True, selected=True, notice=None):
        notice = notice or {"seen": False, "confirmed": True}
        calls = []
        with mock.patch.object(sail_actions, "open_world_map",
                               lambda *a, **k: calls.append("open") or map_ok), \
             mock.patch.object(sail_actions, "_navigate_world_map_to_destination",
                               lambda *a, **k: calls.append("select") or selected), \
             mock.patch.object(sail_actions, "confirm_departure_notice",
                               lambda *a, **k: calls.append("notice") or notice):
            res = sail_actions.commit_departure("Amsterdam")
        return res, calls

    def test_it_returns_as_soon_as_the_destination_is_committed(self):
        res, calls = self._commit()
        self.assertTrue(res["ok"])
        self.assertEqual(calls, ["open", "select", "notice"],
                         "it did more than commit the destination")

    def test_it_does_not_wait_for_the_sea(self):
        """`ok` means COMMITTED, never "the fleet is under way".

        `_wait_until_at_sea` no longer exists — it was deleted with the superseded departure
        path, because waiting to see whether a transition happened is the dispatcher's job.
        Its absence IS the assertion."""
        self.assertFalse(hasattr(sail_actions, "_wait_until_at_sea"),
                         "a primitive waiting out a world change has come back")

    def test_it_does_not_fall_back_to_the_harbour(self):
        """The fallback is a decision, and decisions belong to the task runner."""
        with mock.patch.object(sail_actions, "_depart_from_harbour") as harbour:
            self._commit(selected=False)
        harbour.assert_not_called()

    def test_a_failure_to_select_is_reported_not_retried(self):
        res, calls = self._commit(selected=False)
        self.assertFalse(res["ok"])
        self.assertEqual(calls.count("select"), 1, "the primitive retried on its own")

    def test_an_unconfirmed_notice_is_a_failure(self):
        res, _calls = self._commit(notice={"seen": True, "confirmed": False})
        self.assertFalse(res["ok"])
