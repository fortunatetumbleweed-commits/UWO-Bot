"""Perception may observe anything; it may not act.

Until 2026-08-26 `dismiss_interruptors` fired learned recoveries proactively:

    [perceive] Learned recovery matched proactively: ... — executing 5 action(s)

A learned recovery targets the screen UNDERNEATH — it taps the harbour, opens Recruit
Crew, confirms a dialog — so firing it from inside perceive made PERCEIVING cause a
transition. That is the one act reserved for the dispatcher
(docs/architecture_DRAFT.md, "Only the dispatcher causes transitions"), and it is why a
call that reads the screen could move the fleet.

Dismissing an interruptor is NOT the same act and stays: an obstruction is a film over a
world the bot is still in, and clearing it restores what was already there.

The distinction is "is the world underneath still there afterwards?" — a popup's close-X
leaves it; a recovery's five taps do not.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain import perceive as _p


def _tokens(*words):
    return [(w, 0.9, (0, 0, 10, 10), 0) for w in words]


def _plan(sid="recruit_crew", n=5):
    return types.SimpleNamespace(
        scenario_id=sid,
        actions=[types.SimpleNamespace(action="tap") for _ in range(n)],
    )


class PerceiveDoesNotExecuteRecoveries(unittest.TestCase):

    def _dismiss(self, *, found, recoveries):
        """Run one dismiss_interruptors with no interruptor and a matching recovery."""
        with patch.object(_p, "_cap_for_test", create=True):
            with patch("capture.adb_capture.capture_screen", return_value=object()), \
                 patch("actions.sail_actions._ocr_frame", return_value=_tokens("Not", "Enough", "Crew")), \
                 patch.object(_p, "_detect_interruptors", return_value=(found, None)), \
                 patch.object(_p, "_match_learned_recoveries", return_value=recoveries), \
                 patch("brain.human_escalation._execute_plan") as ex:
                _p.dismiss_interruptors(object())
        return ex

    def test_a_matching_recovery_is_not_executed(self):
        ex = self._dismiss(found=[], recoveries=[_plan()])
        ex.assert_not_called()

    def test_a_matching_recovery_is_still_reported(self):
        """Knowing is free — the run analysis wants to see what matched."""
        from loguru import logger          # the project logs through loguru, not stdlib
        seen = []
        sink = logger.add(seen.append, level="INFO")
        try:
            self._dismiss(found=[], recoveries=[_plan("recruit_crew")])
        finally:
            logger.remove(sink)
        self.assertTrue(any("recruit_crew" in m for m in seen),
                        "the match must still be logged, just not fired")

    def test_the_loop_does_not_spin_on_a_match(self):
        """A match used to be a reason to go round again; now only an interruptor is."""
        calls = []
        with patch("capture.adb_capture.capture_screen",
                   side_effect=lambda *a, **k: calls.append(1) or object()), \
             patch("actions.sail_actions._ocr_frame", return_value=_tokens("x")), \
             patch.object(_p, "_detect_interruptors", return_value=([], None)), \
             patch.object(_p, "_match_learned_recoveries", return_value=[_plan()]), \
             patch("brain.human_escalation._execute_plan"):
            _p.dismiss_interruptors(object())
        self.assertLessEqual(len(calls), 1, "must not run all 5 rounds over a match it will not fire")


class TheRuleIsStatedInTheCode(unittest.TestCase):

    def test_perceive_holds_no_call_to_execute_plan(self):
        """The grep that would catch this coming back by another route."""
        src = open("brain/perceive.py").read()
        # The name may appear in prose; a CALL is `_execute_plan(`.
        self.assertNotIn("_execute_plan(", src,
                         "perceive must not execute an escalation plan — that is a transition")


if __name__ == "__main__":
    unittest.main()
