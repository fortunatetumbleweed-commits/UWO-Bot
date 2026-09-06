"""A handler does ONE thing and returns — FC-3, and the fix for it.

Live 2026-09-05 at San Village. `VillageActivity._on_confirm` called
`commit_via_positive_taps`, which presses positive buttons until the cycle closes:

    [commit] iter 1: tap 'Ok'
    [commit] iter 2: tap 'Receive'        <- the OVERFLOW card, which had just appeared
             ...     tap 'Ok'            <- "Unclaimed trade goods will be discarded"

Three screens inside ONE handler call. The dispatcher never ticked, so the village's own
`_on_overflow` — which owns that card and holds the dumping policy — was never reachable,
and 360 units were discarded in silence. Nothing in the loop was careless; holding control
across a capture is what swallows.

The looping form is still right for an escalation path that owns the screen until it
settles. It is wrong inside the dispatcher's cycle.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import brain.village_context as C
from brain.activities.village import Barter, VillageActivity
from brain.dispatcher import WORKING

GOAL = Barter("Bambara Groundnut", "San Village")


def _state():
    return types.SimpleNamespace(state="village", port="San Village")


def _activity(context):
    return VillageActivity(open_panel_fn=lambda: True,
                           overflow_fn=lambda: 0,
                           saw_fn=lambda: {"screen": "Village interior", "submenu": None},
                           select_fn=lambda g, r: True,
                           read_panel_fn=lambda: None,
                           context_fn=lambda _st: context,
                           commit_fn=lambda: {"ok": True},
                           exchange_live_fn=lambda: True,
                           recipe_fn=lambda g: {})


class OneTapPerTick(unittest.TestCase):

    def _taps_for(self, context):
        taps = []
        act = _activity(context)
        with mock.patch("brain.commit_actions.commit_via_positive_taps",
                        side_effect=lambda **kw: taps.append(kw) or [("ok", 0.5, 0.9)]):
            res = act.work(GOAL, _state())
        return taps, res

    def test_the_confirm_handler_taps_once(self):
        taps, res = self._taps_for(C.EXCHANGE_CONFIRM)
        self.assertEqual(len(taps), 1, "one call")
        self.assertEqual(taps[0].get("max_taps"), 1,
                         "and that call must press ONE button, not until the cycle closes")
        self.assertEqual(res.status, WORKING)

    def test_the_result_handler_taps_once(self):
        taps, res = self._taps_for(C.BARTER_RESULT)
        self.assertEqual(len(taps), 1)
        self.assertEqual(taps[0].get("max_taps"), 1)
        self.assertEqual(res.status, WORKING)

    def test_each_reports_what_it_did(self):
        """The recorded intent is how the NEXT tick tells a landed tap from a swallowed one."""
        _, confirm = self._taps_for(C.EXCHANGE_CONFIRM)
        _, result = self._taps_for(C.BARTER_RESULT)
        self.assertEqual(confirm.observed["did"], "confirmed the exchange")
        self.assertEqual(result.observed["did"], "cleared the result dialog")


class TheLoopingFormSurvivesForItsOwnCallers(unittest.TestCase):
    """`commit_via_positive_taps` is shared infrastructure — verified_recruit,
    human_escalation, plan_actions and barter_executor all use it, and an escalation path
    that owns the screen until it settles genuinely wants the loop."""

    def test_it_still_exists_and_still_loops_by_default(self):
        import inspect

        from brain.commit_actions import commit_via_positive_taps

        sig = inspect.signature(commit_via_positive_taps)
        self.assertGreater(sig.parameters["max_taps"].default, 1)

    def test_a_single_tap_is_not_reported_as_a_failure_to_settle(self):
        """max_taps=1 is a deliberate choice, so it must not warn about an unsettled cycle."""
        import inspect

        from brain.commit_actions import commit_via_positive_taps

        src = inspect.getsource(commit_via_positive_taps)
        self.assertIn("if max_taps > 1:", src)


if __name__ == "__main__":
    unittest.main()
