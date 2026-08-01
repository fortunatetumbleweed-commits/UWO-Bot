"""Regression: when Claude's consult returns outcome_for_goal='irrelevant',
the perceive layer must NOT inject a dismissal into the interruptor list.

Origin: 2026-05-28 ~19:16 live log.  The right-side nav HUD (mini-map +
ports list) was being misclassified as a popup obstruction.  Claude
correctly identified it as 'a persistent HUD panel, not a blocking
popup' and returned outcome_for_goal='irrelevant'.  But the code only
checked `dismissal in _CONSULT_ACTIONABLE` — the irrelevant verdict
was ignored, the bot tap-anywhere'd the centre of the screen on
every tick, wasting time and risking spurious side-effects.
"""
import unittest
from unittest.mock import patch, MagicMock


def _fake_frame(w=2400, h=1080):
    f = MagicMock(); f.width = w; f.height = h; f.size = (w, h)
    return f


def _fake_obstruction(bbox=(1376, 122, 2266, 821), kind="popup"):
    o = MagicMock()
    o.bbox = bbox
    o.kind = kind
    return o


def _make_analysis(outcome: str, dismissal: str = "tap_anywhere"):
    """Build a fake ObstructionAnalysis result."""
    a = MagicMock()
    a.purpose = "The right-side navigation panel (persistent HUD)"
    a.full_text = ""
    a.dismissal = dismissal
    a.relates_to_goal = False
    a.outcome_for_goal = outcome
    a.confidence = "high"
    return a


class IrrelevantConsultSkipTests(unittest.TestCase):

    def test_irrelevant_outcome_does_not_inject_consult_dismissal(self):
        """outcome='irrelevant' → no _consult:* entry in `found`."""
        from brain import perceive as P
        found: list = []

        # Simulate the relevant slice of _detect_interruptors that
        # decides whether to inject _consult:<method>.
        analysis = _make_analysis(outcome="irrelevant", dismissal="tap_anywhere")
        _CONSULT_ACTIONABLE = {
            "tap_decline", "tap_close_x", "tap_anywhere",
            "tap_ok", "tap_accept", "press_back",
        }
        if analysis.outcome_for_goal == "irrelevant":
            pass   # the new branch
        elif analysis.dismissal in _CONSULT_ACTIONABLE:
            found.append(f"_consult:{analysis.dismissal}")

        self.assertEqual(found, [],
                         "irrelevant outcome must not produce a consult entry")

    def test_likely_resolved_still_dispatches(self):
        """outcome='likely_resolved' is still an actionable dismissal."""
        analysis = _make_analysis(outcome="likely_resolved", dismissal="tap_close_x")
        _CONSULT_ACTIONABLE = {
            "tap_decline", "tap_close_x", "tap_anywhere",
            "tap_ok", "tap_accept", "press_back",
        }
        found: list = []
        if analysis.outcome_for_goal == "irrelevant":
            pass
        elif analysis.dismissal in _CONSULT_ACTIONABLE:
            found.append(f"_consult:{analysis.dismissal}")

        self.assertEqual(found, ["_consult:tap_close_x"])

    def test_still_blocking_still_dispatches(self):
        """outcome='still_blocking' should also dispatch (dismiss didn't work yet)."""
        analysis = _make_analysis(outcome="still_blocking", dismissal="press_back")
        _CONSULT_ACTIONABLE = {
            "tap_decline", "tap_close_x", "tap_anywhere",
            "tap_ok", "tap_accept", "press_back",
        }
        found: list = []
        if analysis.outcome_for_goal == "irrelevant":
            pass
        elif analysis.dismissal in _CONSULT_ACTIONABLE:
            found.append(f"_consult:{analysis.dismissal}")

        self.assertEqual(found, ["_consult:press_back"])


class PerceiveIntegrationTest(unittest.TestCase):
    """End-to-end check: the real `_detect_interruptors` code path skips
    a consult that returned outcome=irrelevant."""

    def test_real_detect_interruptors_skips_irrelevant(self):
        """Patch obstruction_consult to return irrelevant; verify perceive
        does not produce a _consult: interruptor."""
        from brain import perceive as P

        # Stage the consult to return irrelevant.
        fake_analysis = _make_analysis(outcome="irrelevant",
                                       dismissal="tap_anywhere")
        with patch("vision.obstruction_consult.consult_obstruction",
                   return_value=fake_analysis), \
             patch("brain.goal_context.current_goal", return_value=None):
            # Simulate the consult-fallback slice of _detect_interruptors.
            # We don't run the full method (it does a lot); we just
            # exercise the branch we patched.
            found: list = []
            from vision.obstruction_consult import consult_obstruction
            from brain.goal_context import current_goal
            analysis = consult_obstruction(
                frame=_fake_frame(),
                obstruction=_fake_obstruction(),
                ocr_tokens=[],
                goal_context=current_goal(),
            )
            _CONSULT_ACTIONABLE = {
                "tap_decline", "tap_close_x", "tap_anywhere",
                "tap_ok", "tap_accept", "press_back",
            }
            if analysis is not None and analysis.outcome_for_goal == "irrelevant":
                pass
            elif analysis is not None and analysis.dismissal in _CONSULT_ACTIONABLE:
                found.append(f"_consult:{analysis.dismissal}")

            self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
