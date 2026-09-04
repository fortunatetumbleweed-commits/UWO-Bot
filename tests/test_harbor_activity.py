"""A blocker is not a recovery — it is the next goal.

What this replaces: `memory/knowledge/fsm/flows.json` answers "not enough crew" with a
six-step UI script (exit to the overworld, navigate to the inn, tap, exit, navigate back),
and when that missed, `resolve_fleet_blocker` climbed a four-rung ladder — KB, then a learned
recovery, then Claude Vision, then waking the human. Eleven of the eighteen entries in
learned_recoveries.json are this one situation taught over and over.

Under the model the harbour REPORTS what blocks it and stops. The next goal follows from the
name, the same activity serves it, and there is no ladder because there is no exception —
hiring crew is ordinary work.
"""

from __future__ import annotations

import types
import unittest

from brain.activities.harbor import (Depart, HarborActivity, RecruitCrew, goal_for_blocker)
from brain.dispatcher import BLOCKED, FINISHED, UNRECOGNISED


def _state(where="building:harbor"):
    return types.SimpleNamespace(state=where, port="Lisboa")


def _ready():
    return {"ready": True, "blocker": None, "on_departure_panel": True, "detail": "all clear"}


def _blocked(text="not enough crew"):
    return {"ready": False, "on_departure_panel": True,
            "blocker": {"text": text, "description": "Fleet crew below departure minimum"},
            "detail": f"{text!r} — Fleet crew below departure minimum"}


class DepartingReportsWhatStopsIt(unittest.TestCase):

    def test_a_ready_fleet_departs(self):
        a = HarborActivity(readiness_fn=_ready, depart_fn=lambda: "supply_depart")
        r = a.work(Depart("Amsterdam"), _state())
        self.assertEqual(r.status, FINISHED)
        self.assertTrue(r.observed["departed"])

    def test_a_blocked_fleet_names_the_blocker(self):
        a = HarborActivity(readiness_fn=_blocked)
        r = a.work(Depart("Amsterdam"), _state())
        self.assertEqual(r.status, BLOCKED)
        self.assertEqual(r.observed["blocker"], "not enough crew")

    def test_the_blocker_carries_the_goal_that_clears_it(self):
        """The whole point: what comes back is work to do, not an error to recover from."""
        a = HarborActivity(readiness_fn=_blocked)
        self.assertEqual(a.work(Depart(), _state()).observed["next_goal"], RecruitCrew())

    def test_a_blocked_fleet_does_not_tap_depart(self):
        taps = []
        a = HarborActivity(readiness_fn=_blocked, depart_fn=lambda: taps.append(1) or "x")
        a.work(Depart(), _state())
        self.assertEqual(taps, [])

    def test_the_wrong_screen_is_being_lost_not_being_blocked(self):
        """Looking at the wrong panel cannot be fixed by looking again — hand back."""
        a = HarborActivity(readiness_fn=lambda: {"ready": False, "on_departure_panel": False,
                                                 "detail": "not on the panel"})
        self.assertEqual(a.work(Depart(), _state()).status, UNRECOGNISED)

    def test_ready_but_no_button_hands_back_rather_than_guessing(self):
        a = HarborActivity(readiness_fn=_ready, depart_fn=lambda: "not_found")
        self.assertEqual(a.work(Depart(), _state()).status, UNRECOGNISED)

    def test_it_refuses_a_goal_that_is_not_its_own(self):
        a = HarborActivity(readiness_fn=_ready)
        self.assertEqual(a.work("sell the hold", _state()).status, BLOCKED)

    def test_it_hands_back_when_it_is_not_in_a_harbour(self):
        a = HarborActivity(readiness_fn=_ready)
        r = a.work(Depart(), _state("building:market"))
        self.assertEqual(r.status, UNRECOGNISED)


class TheBlockerBecomesAGoal(unittest.TestCase):

    def test_crew(self):
        self.assertEqual(goal_for_blocker("not enough crew"), RecruitCrew())

    def test_a_blocker_this_building_cannot_serve_returns_nothing(self):
        """Supply is the market's, ship life is the shipyard's. Both are goals; neither is
        this activity's, and saying so beats inventing a recovery."""
        self.assertIsNone(goal_for_blocker("not enough supply"))

    def test_no_blocker_is_no_goal(self):
        self.assertIsNone(goal_for_blocker(None))
        self.assertIsNone(goal_for_blocker(""))


class RecruitingAnswersItsConfirmation(unittest.TestCase):

    def _activity(self, *, reply="OK", case="action_dialog", taps=None):
        from brain.unexpected import ACTION_DIALOG, CLEAR, Unexpected
        u = Unexpected(case=ACTION_DIALOG if case == "action_dialog" else CLEAR,
                       state="building", title="Notice",
                       text=("Recruit Crew? 181,224 ducats will be spent.",),
                       options=("Cancel", "OK"), positive="OK")

        def resolve(unexpected, decide=None, **kw):
            label = decide(unexpected) if decide else None
            if label and taps is not None:
                taps.append(label)
            return {"handled": bool(label), "action": f"tap:{label}" if label else None,
                    "reason": "x"}

        return HarborActivity(recruit_fn=lambda: True, look_fn=lambda: u,
                              resolve_fn=resolve, ask=lambda _p: reply)

    def test_the_confirmation_is_answered(self):
        taps = []
        r = self._activity(taps=taps).work(RecruitCrew(), _state())
        self.assertEqual(r.status, FINISHED)
        self.assertEqual(taps, ["OK"])

    def test_an_unanswerable_reply_taps_nothing(self):
        taps = []
        self._activity(reply="I am not sure", taps=taps).work(RecruitCrew(), _state())
        self.assertEqual(taps, [])

    def test_no_dialog_is_not_a_problem(self):
        r = self._activity(case="clear").work(RecruitCrew(), _state())
        self.assertEqual(r.status, FINISHED)
        self.assertIsNone(r.observed["dialog"])

    def test_a_missing_recruit_control_is_reported_not_worked_around(self):
        a = HarborActivity(recruit_fn=lambda: False)
        r = a.work(RecruitCrew(), _state())
        self.assertEqual(r.status, BLOCKED)

    def test_it_does_not_check_whether_the_crew_is_now_enough(self):
        """The game's panel answers that on the next Depart, and it outranks any count
        this could keep. One rung, not a ladder."""
        readings = []
        a = self._activity()
        a._readiness = lambda: readings.append(1) or _ready()
        a.work(RecruitCrew(), _state())
        self.assertEqual(readings, [])


if __name__ == "__main__":
    unittest.main()
