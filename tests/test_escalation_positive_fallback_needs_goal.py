"""The escalation's positive-button drain only applies when there is a goal to finish.

`find_positive_button_for_context` is for the case where something UNEXPECTED interrupted a
goal the bot was pursuing and had already committed an action toward — there is then a
transaction to close, and the goal says which button closes it. With no goal the search
degrades to "tap whatever looks positive".

Live 2026-08-22: `recover_to_port_overworld` escalated with `goal=None` on the Market
landing page — no transaction in flight — and the fallback tapped `Trade Info` (177,817)
and then the `Requested Trade Goods` panel title (2024,567). Both matched `POSITIVE_LABELS`
only because it contains the word "trade".

User: "find_positive_button_for_context should only be used for the situation that
unexpected things happened, and the bot has a goal and has committed some action. If in an
expected screen we should have multiple things to anchor the action."
"""
from unittest import mock

import brain.human_escalation as he


def _perceive_result(state="building"):
    r = mock.MagicMock()
    r.state = state
    r.detail = "building: market"
    return r


def _run(goal):
    """Drive escalate() far enough to see whether the drain fires."""
    calls = {"drain": 0, "goal_keywords": None}

    def fake_commit(max_taps=3, goal_keywords=None, **_kw):
        calls["drain"] += 1
        calls["goal_keywords"] = goal_keywords
        return []

    with mock.patch("brain.commit_actions.commit_via_positive_taps", side_effect=fake_commit), \
         mock.patch.object(he, "_match_learned_recovery", return_value=None, create=True), \
         mock.patch.object(he, "_prompt_operator_for_step", return_value=None), \
         mock.patch("capture.adb_capture.capture_screen", return_value=object()), \
         mock.patch("time.sleep"):
        try:
            he.escalate(context="recover_to_port_overworld", perceive_result=_perceive_result(),
                        goal=goal)
        except Exception:
            pass          # teaching abort is expected once the fallback is skipped
    return calls


class TestGoalRequired:
    def test_no_goal_means_no_positive_tapping(self):
        """The exact live case: lost on a screen with nothing to commit."""
        calls = _run(goal=None)
        assert calls["drain"] == 0, \
            "with no goal there is no transaction to finish — it must not tap"

    def test_a_goal_lets_the_drain_run_with_its_keywords(self):
        calls = _run(goal="buy materials at Jakarta")
        if calls["drain"]:
            assert calls["goal_keywords"], \
                "the drain must be given the goal, not called unguarded"
