"""FLEET_CHECK answers a blocker with a GOAL, not with a ladder.

Before 2026-08-26 a fleet that could not depart went to `resolve_fleet_blocker`, which
climbed four rungs — KB script, learned recovery, Claude Vision, wake the human — with a
six-step hard-coded UI walk to the inn at the top of it.

Now the named blocker becomes a goal and the harbour activity serves it. One rung, then the
phase re-checks on its next tick and reads the game's own answer.

The ladder remains ONLY for blockers nothing serves yet — an unnamed one, or "not enough
supply", whose remedy is at the market. That fallback is what keeps this a safe swap rather
than a capability loss, and it is why these tests check both directions.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain.activities.harbor import RecruitCrew
from brain.goals.sail_to import SailToGoal, SailPhase


def _goal():
    g = SailToGoal(destination="Amsterdam")
    g.phase = SailPhase.FLEET_CHECK
    return g


def _reading(ready=False, text="not enough crew", on_panel=True):
    return {"ready": ready, "on_departure_panel": on_panel,
            "blocker": {"text": text} if text else None,
            "detail": f"{text} — blocked", "text": "", "frame": None}


class ACrewBlockerBecomesAGoal(unittest.TestCase):

    def _run(self, reading):
        served, laddered = [], []
        with patch("actions.sail_actions.read_fleet_readiness", return_value=reading), \
             patch("brain.activities.harbor.HarborActivity.work",
                   side_effect=lambda goal, state: served.append(goal) or _ok()), \
             patch("actions.sail_actions.resolve_fleet_blocker",
                   side_effect=lambda r: laddered.append(r) or {"via": "kb", "resolved": True}):
            _goal()._action_fleet_check()
        return served, laddered

    def test_the_harbour_activity_is_asked_to_recruit(self):
        served, laddered = self._run(_reading())
        self.assertEqual(served, [RecruitCrew()])

    def test_the_ladder_is_not_climbed(self):
        served, laddered = self._run(_reading())
        self.assertEqual(laddered, [], "four rungs of guessing, for a situation with a goal")

    def test_a_blocker_with_no_goal_still_falls_back(self):
        """Supply is the market's remedy. Nothing serves it here YET — so nothing is lost."""
        served, laddered = self._run(_reading(text="not enough supply"))
        self.assertEqual(served, [])
        self.assertEqual(len(laddered), 1)

    def test_an_unnamed_blocker_still_falls_back(self):
        served, laddered = self._run(_reading(text=""))
        self.assertEqual(served, [])
        self.assertEqual(len(laddered), 1)

    def test_a_ready_fleet_asks_for_nothing(self):
        served, laddered = self._run(_reading(ready=True, text=""))
        self.assertEqual((served, laddered), ([], []))

    def test_it_does_not_decide_whether_the_crew_is_now_enough(self):
        """The phase re-checks next tick; the panel is the answer, not a count kept here."""
        with patch("actions.sail_actions.read_fleet_readiness", return_value=_reading()), \
             patch("brain.activities.harbor.HarborActivity.work", return_value=_ok()):
            g = _goal()
            res = g._action_fleet_check()
        self.assertFalse(g._fleet_checked)
        self.assertFalse(res.ok)


def _ok():
    from brain.dispatcher import ActivityResult, FINISHED
    return ActivityResult(FINISHED, {"recruited": True}, detail="recruit crew")


if __name__ == "__main__":
    unittest.main()
