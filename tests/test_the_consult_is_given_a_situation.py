"""A goal LABEL cannot answer "is this dialog yours?". A situation can.

Live 2026-09-08 at Barcelona, five times over four minutes: the market tapped its own
restock control because Iron was sold out and the goal wanted 709, and the perception layer
tapped Cancel on the prompt that tap raised. The consult had been handed the dialog and the
string `Hold`, and asked whether they were related.

Re-asked against the same frame with the situation added — where it was, what it had bought,
and that its own last action was the restock tap — Claude went from `tap_decline` to
`tap_ok`, and its purpose read "opened by the bot's own tap on the restock button". With the
goal changed to selling, the same dialog came back `relates_to_goal=false` again, so the
situation is doing the work rather than nudging every answer toward OK.

Everything that situation needed was already travelling in the activity's result.
"""

from __future__ import annotations

import unittest
import unittest.mock as mock


class TheSituationIsNarrated(unittest.TestCase):

    def _ctx(self, **over):
        from brain.goal_context import GoalContext
        target = {"goal": "hold Iron (~709)", "doing": "tapped the restock",
                  "where": "sub_menu:purchase", "port": "Barcelona",
                  "progress": {"Iron": {"have": 356, "want": 709, "state": "short"}}}
        target.update(over)
        return GoalContext(intent="Hold", target=target)

    def test_it_says_where_what_and_how_far(self):
        from vision.obstruction_consult import _situation
        out = _situation(self._ctx())
        self.assertIn("Barcelona", out)
        self.assertIn("hold Iron (~709)", out)
        self.assertIn("Iron 356 of 709", out, "have against want, not just the order")
        self.assertIn("tapped the restock", out)

    def test_the_last_action_is_offered_as_the_cause(self):
        """The whole point: this dialog may be the bot's own doing."""
        from vision.obstruction_consult import _situation
        self.assertIn("may be what put this on screen", _situation(self._ctx()))

    def test_no_goal_says_so_plainly(self):
        from vision.obstruction_consult import _situation
        out = _situation(None)
        self.assertIn("No goal is active", out)

    def test_a_sparse_goal_still_narrates(self):
        from vision.obstruction_consult import _situation
        out = _situation(self._ctx(progress={}, doing=None, port=None))
        self.assertIn("hold Iron (~709)", out)


class ThePromptDoesNotCarryTheAnswer(unittest.TestCase):
    """The first version of this rule named the very dialog it was meant to generalise.

    That made every variant answer correctly, including the one with no situation at all —
    an A/B that proves nothing. It is the same failure as the Qwen chrome glossary, which
    warned about a phrase and thereby supplied it.
    """

    def test_no_worked_example_names_a_specific_dialog(self):
        from vision.obstruction_consult import _CONSULT_PROMPT
        for leaked in ("restock", "Replenish", "Iron", "Barcelona"):
            self.assertNotIn(leaked, _CONSULT_PROMPT,
                             f"{leaked!r} in the prompt teaches this case, not the rule")

    def test_it_no_longer_opens_by_calling_it_an_obstruction(self):
        from vision.obstruction_consult import _CONSULT_PROMPT
        self.assertIn("MAY BE THE BOT'S OWN DOING", _CONSULT_PROMPT)


class TheActionBeatsTheOutcomeVerb(unittest.TestCase):

    def test_acted_wins_over_did(self):
        from brain.dispatcher import ActivityResult, WORKING, _did
        seen = {"did": "refreshed", "acted": "tapped the restock"}
        self.assertEqual("tapped the restock", _did(ActivityResult(WORKING, seen)),
                         "'refreshed' is the outcome; the tap is what raised the dialog")

    def test_did_still_serves_when_nothing_acted(self):
        from brain.dispatcher import ActivityResult, WORKING, _did
        self.assertEqual("scrolled", _did(ActivityResult(WORKING, {"did": "scrolled"})))

    def test_progress_and_port_travel_too(self):
        from brain.dispatcher import ActivityResult, WORKING, _progress, _where_we_are
        r = ActivityResult(WORKING, {"port": "Barcelona",
                                     "materials": {"Iron": {"have": 356, "want": 709}}})
        self.assertEqual("Barcelona", _where_we_are(r))
        self.assertEqual(356, _progress(r)["Iron"]["have"])


class PerceptionKeepsItsHandsOffTheActivitysDialog(unittest.TestCase):
    """`dismissal` is "if you HAD to get rid of this" — the code said so and tapped anyway."""

    def _found_for(self, relates, dismissal="tap_decline", outcome="still_blocking"):
        import brain.perceive as bp
        analysis = mock.Mock(relates_to_goal=relates, dismissal=dismissal,
                             outcome_for_goal=outcome, purpose="a prompt")
        obstruction = mock.Mock(kind="dialog", bbox=(826, 215, 1574, 868), confidence="high")
        # everything is imported INSIDE _detect_interruptors, so patch it at its source
        with mock.patch("vision.obstruction_consult.consult_obstruction",
                        lambda **k: analysis), \
             mock.patch("brain.fsm_registry.get_fsm_registry",
                        lambda: mock.Mock(interruptors={})), \
             mock.patch("vision.obstruction_classifier.classify_obstruction",
                        lambda *a, **k: obstruction), \
             mock.patch("vision.screen_perception.parse_screen", lambda *a, **k: mock.Mock()), \
             mock.patch.object(bp, "_no_obstruction_cache_check", lambda f: False), \
             mock.patch.object(bp, "_has_daily_news_close_x", lambda f: False):
            found, _ = bp._detect_interruptors(mock.Mock(width=2400, height=1080),
                                               [("Replenish Stock?", 0.9, 1201, 437)])
        return found

    def test_a_dialog_about_the_goal_is_left_alone(self):
        self.assertEqual([], [f for f in self._found_for(True) if f.startswith("_consult:")],
                         "the activity pursuing the goal owns its own dialog")

    def test_something_unrelated_and_in_the_way_is_still_dismissed(self):
        """The case this path was built for: the Amsterdam `Exit Game?` confirmation."""
        self.assertIn("_consult:tap_decline", self._found_for(False))


if __name__ == "__main__":
    unittest.main()
