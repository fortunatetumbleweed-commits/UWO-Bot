# tests/test_a_dialog_is_a_window.py
#
# "THE DISPATCHER SHOULD DETECT DIALOG AND SEND TO ACTIVITY TO HANDLE" (user, 2026-09-03).
# Design: docs/dialogs_are_windows.md.
#
# The game reproduces Android's dialog mechanics: the dialog is a window above the activity's
# with FLAG_DIM_BEHIND (a measured x1.98 scrim), the activity keeps its state but loses window
# focus, and dismissal is a button the dialog registered. So: work is not dispatched into a
# screen that cannot receive input, and the activity gets first refusal because only it knows
# what the buttons mean.
#
# WHERE THE ANALOGY STOPS, and this file exists partly to pin the correction: Android's Back
# fires onCancel and the system never presses your positive button — because it must not usurp
# a HUMAN's intent. Here the bot IS the user. There is no intent to protect, and cancelling is
# not neutral, it is refusing to play. This dispatcher briefly took the "safe exit" (close X
# or Cancel) and that was wrong three ways at once (user):
#
#   * Cancel LOOPS. On the San Village frame it returns to `Insufficient Empty Space`, whose
#     `Receive` cannot succeed with a full hold, which raises the same Notice. A village has
#     no market, so there is no branch that frees space.
#   * The goods are lost EITHER WAY. "Unclaimed trade goods will be discarded" is what OK
#     acknowledges, not what it causes.
#   * The run had already taken that trade three times in the same barter — ~2,000 units
#     discarded to land 4,279. The fourth tap was not a dangerous new decision; it simply did
#     not register.
#
# And the default was already written down: "the default is OK unless it is spending red gem"
# (user, 2026-08-23), implemented in `brain.game_rules.answer_dialog`, whose docstring warns
# that the moment the state machine defaults for itself, that layer is decorative. The
# dispatcher ASKS; it does not decide.

import unittest
from unittest.mock import patch

from brain.dispatcher import BLOCKED, UNRECOGNISED, WORKING, ActivityResult


class _Dialog:
    def __init__(self, *, close=None, actions=(), kind="confirmation", body=()):
        self.close_button, self._kind, self.body_text = close, kind, body
        self.title_bar = None
        self.actions = tuple(type("A", (), {"label": l, "bbox": b})() for l, b in actions)

    def kind(self):
        return self._kind


_OK = ("Ok", (1199, 767, 1418, 846))
_CANCEL = ("Cancel", (984, 769, 1200, 846))
_UNCLAIMED = ("Complete the trade?",
              "486 Bambara Groundnut has not been claimed yet.",
              "Unclaimed trade goods will be discarded.")


class _Act:
    name = "village"
    GOALS = ()

    def __init__(self, answer=None):
        self.answer, self.worked = answer, False

    def work(self, goal, state):
        self.worked = True
        return ActivityResult(WORKING, {"did": "normal work"})

    def on_dialog(self, dialog, goal):
        return self.answer


class _Bare:
    """An activity with no `on_dialog` at all — most of them."""
    name = "market"
    GOALS = ()

    def __init__(self):
        self.worked = False

    def work(self, goal, state):
        self.worked = True
        return ActivityResult(WORKING, {"did": "normal work"})


def _dispatcher(activity):
    from brain.dispatcher import Dispatcher
    import types
    state = types.SimpleNamespace(state="village", location="village", frame=None)
    d = Dispatcher(perceive=lambda: state,
                   activities={"village": activity, "market": activity},
                   next_goal=lambda _r, _s: "a goal",
                   to_intent=lambda _g, _s: None,
                   dispatch=lambda _i: None,
                   dialog=lambda _s: None)
    d.goal = "a goal"
    return d, state


class TheActivityGetsFirstRefusal(unittest.TestCase):
    def test_an_owned_dialog_is_not_second_guessed(self):
        answer = ActivityResult(BLOCKED, {"dialog": "mine"}, detail="I own this")
        act = _Act(answer=answer)
        d, state = _dispatcher(act)
        with patch("actions.ui.tap_at") as tap:
            d._offer_dialog(act, _Dialog(actions=[_OK, _CANCEL]), state)
        tap.assert_not_called()

    def test_normal_work_is_not_dispatched_into_a_covered_screen(self):
        act = _Act(answer=ActivityResult(BLOCKED, {}, detail="mine"))
        d, state = _dispatcher(act)
        with patch("actions.ui.tap_at"):
            d._offer_dialog(act, _Dialog(actions=[_OK]), state)
        self.assertFalse(act.worked)


class TheDefaultIsThePositiveOption(unittest.TestCase):
    """"The default is OK unless it is spending red gem" (user, 2026-08-23)."""

    def _press(self, dialog, activity=None):
        act = activity or _Bare()
        d, state = _dispatcher(act)
        with patch("actions.ui.tap_at") as tap, patch("actions.ui.back") as back:
            rec = d._offer_dialog(act, dialog, state)
        return tap, back, rec

    def test_ok_is_pressed_not_cancel(self):
        tap, _back, _rec = self._press(_Dialog(actions=[_OK, _CANCEL], body=_UNCLAIMED))
        tap.assert_called_once()
        self.assertEqual(tap.call_args[0][:2], (1308, 806),
                         "pressed something other than the Ok this run tapped three times")

    def test_an_activity_that_disowns_it_gets_the_same_default(self):
        act = _Act(answer=ActivityResult(UNRECOGNISED, {}, detail="not mine"))
        tap, _b, _r = self._press(_Dialog(actions=[_OK, _CANCEL]), activity=act)
        tap.assert_called_once()

    def test_a_close_X_does_not_tempt_it_away_from_the_positive_option(self):
        """The close X is still on the model; it must not become a second decider."""
        tap, _b, _r = self._press(_Dialog(close=(1610, 230, 1662, 285),
                                          actions=[_OK, _CANCEL]))
        self.assertEqual(tap.call_args[0][:2], (1308, 806))


class RedGemsAreRealMoney(unittest.TestCase):
    def test_nothing_is_pressed_and_the_run_is_told(self):
        act = _Bare()
        d, state = _dispatcher(act)
        dlg = _Dialog(actions=[_OK, _CANCEL], body=("Spend 50 red gems to refresh?",))
        with patch("actions.ui.tap_at") as tap, patch("actions.ui.back") as back:
            rec = d._offer_dialog(act, dlg, state)
        tap.assert_not_called()
        back.assert_not_called()
        self.assertIsNotNone(rec, "a dialog nobody will answer must be reported")


class ASwallowedTapIsRetriedByTheLoop(unittest.TestCase):
    """"The bot actually has been closing that by tapping ok, the problem is just that tap did
    not register" (user). The re-check is the NEXT TICK finding the dialog still there — never
    a sub-loop waiting in place for its own effect."""

    def test_it_presses_again_while_the_dialog_is_still_there(self):
        act = _Bare()
        d, state = _dispatcher(act)
        dlg = _Dialog(actions=[_OK, _CANCEL])
        with patch("actions.ui.tap_at") as tap:
            for _ in range(3):
                d._dialog_looks += 1
                d._offer_dialog(act, dlg, state)
        self.assertEqual(tap.call_count, 3)

    def test_but_not_for_ever(self):
        act = _Bare()
        d, state = _dispatcher(act)
        d._dialog_looks = d._MAX_DIALOG_LOOKS + 1
        with patch("actions.ui.tap_at") as tap:
            rec = d._offer_dialog(act, _Dialog(actions=[_OK]), state)
        tap.assert_not_called()
        self.assertIsNotNone(rec)


class TheVillageNamesTheCostAndHandsBack(unittest.TestCase):
    def test_it_does_not_block_a_mission_over_a_decision_already_made(self):
        from brain.activities.village import VillageActivity
        d = _Dialog(actions=[_OK, _CANCEL], body=_UNCLAIMED)
        self.assertIsNone(VillageActivity().on_dialog(d, goal=None),
                          "blocked on a dialog whose answer is simply Ok")

    def test_an_unrelated_dialog_is_also_disowned(self):
        from brain.activities.village import VillageActivity
        self.assertIsNone(
            VillageActivity().on_dialog(_Dialog(body=("A new season has begun!",)), goal=None))


if __name__ == "__main__":
    unittest.main()


class AnUnnameableScreenIsTheStrongestReasonToLook(unittest.TestCase):
    """"Looks like it ran into the same issue that the tap of OK did not work but it did not
    retry" (user, 2026-09-03) — on a `Replenish Stock?` prompt at a market.

    THE CHECK USED TO SIT AFTER THE ACTIVITY WAS PICKED. A blocking dialog is precisely what
    stops a screen being recognised: the prompt classified as `unknown`, `_pick` returned
    None, and the dispatcher handed back at "no activity for state 'unknown'" without ever
    looking for a dialog. So the dialog machinery ran only when the screen was already
    legible — which is when a dialog is least in the way.

    Same shape as the KIND_NONE crash: sound in the tested path, unreachable in the live one.
    An unnameable screen is not a reason to skip the dialog; it is the reason to look."""

    def _dispatcher_with_no_activity(self, dialog):
        from brain.dispatcher import Dispatcher
        import types
        state = types.SimpleNamespace(state="unknown", location="unknown", frame=None)
        d = Dispatcher(perceive=lambda: state,
                       activities={},                      # nothing serves 'unknown'
                       next_goal=lambda _r, _s: "a goal",
                       to_intent=lambda _g, _s: None,
                       dispatch=lambda _i: None,
                       dialog=lambda _s: dialog)
        d.goal = "a goal"
        return d, state

    def test_a_dialog_is_answered_even_with_no_activity_for_the_screen(self):
        dlg = _Dialog(actions=[_OK, _CANCEL], body=("Replenish Stock?",))
        d, state = self._dispatcher_with_no_activity(dlg)
        with patch("actions.ui.tap_at") as tap:
            d._dialog_looks += 1
            rec = d._offer_dialog(None, dlg, state)
        tap.assert_called_once()
        self.assertEqual(tap.call_args[0][:2], (1308, 806))
        self.assertIsNotNone(rec)

    def test_the_check_runs_before_the_pick(self):
        """Pinned in the source, because the ordering IS the fix — a later refactor that
        moves it back below `_pick` restores the hole silently."""
        import inspect
        from brain.dispatcher import Dispatcher
        src = inspect.getsource(Dispatcher.step)
        self.assertLess(src.index("dialog = self._dialog_on(state)"),
                        src.index("if activity is None:"),
                        "the dialog check sank back below the activity pick")

    def test_red_gems_still_refuse_with_no_activity(self):
        dlg = _Dialog(actions=[_OK, _CANCEL], body=("Spend 50 red gems?",))
        d, state = self._dispatcher_with_no_activity(dlg)
        with patch("actions.ui.tap_at") as tap:
            rec = d._offer_dialog(None, dlg, state)
        tap.assert_not_called()
        self.assertIsNotNone(rec)


class ATallDialogIsStillADialog(unittest.TestCase):
    """THE HEIGHT GUARD WAS FOR A CONTAMINATED CLUSTER, NOT FOR A CARD.

    It was written when the "cluster" was a full-height vertical stripe that swallowed
    whatever shared its columns, so height stood in for "this is not really one widget". Once
    the card is segmented off its own title bar, height means what it says — and real dialogs
    are tall. `ff60c83`'s commit message predicted this ("it would be rejecting genuinely tall
    dialogs") and deferred the change; the deferral cost a mission on 2026-09-04, when a
    `Gear Info` modal measured 880px against an 864px limit, was reported CLEAN, and nothing
    could close a dialog the bot had itself opened.

    The guard's original case — the 2026-05-23 world-map nearby-ports panel at
    (1921,123)-(2399,1028) — is rejected TWICE OVER without it: centre x=2160 fails centring,
    left edge 1921 fails the edge test. A panel HUGS AN EDGE; a modal is centred and inset.
    Height never separated them, it only correlated."""

    _SESSION = "data/sessions/trace_barter_cmd_2026-09-04T10-33-18"

    def _detect(self, path):
        import os
        if not os.path.exists(path):
            self.skipTest("session frames not in this checkout")
        from PIL import Image
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        f = Image.open(path)
        return detect_dialog(list(parse_fast_cached(f)), f.width, f.height, frame=f)

    def test_the_880px_gear_info_is_seen(self):
        d = self._detect(f"{self._SESSION}/frame_0355.png")
        self.assertIsNotNone(d, "a modal the bot opened was invisible to it")
        self.assertGreater(d.bbox[3] - d.bbox[1], 864, "this is the frame that was rejected")

    def test_a_side_panel_is_still_rejected_by_the_guards_that_matter(self):
        """The nearby-ports panel, by its geometry. Centring and edge do the work."""
        fw, fh = 2400, 1080
        bbox = (1921, 123, 2399, 1028)
        cx = (bbox[0] + bbox[2]) / 2
        self.assertFalse(0.30 * fw <= cx <= 0.70 * fw, "centring guard would not reject it")
        self.assertTrue(bbox[0] > 0.70 * fw, "edge guard would not reject it")

    def test_a_clean_village_screen_is_still_clean(self):
        self.assertIsNone(self._detect(f"{self._SESSION}/frame_0354.png"))


class ADialogWithNoButtonsIsClosedNotReported(unittest.TestCase):
    """`Gear Info` offers only an X. `answer_dialog` rightly returns None for an empty option
    list, which would report BLOCKED on a dialog with an obvious, harmless exit. Closing
    something that commits nothing is not the system pressing a positive button — there is no
    positive button to press."""

    def test_the_close_x_is_used(self):
        d = _Dialog(close=(1448, 96, 1505, 157), actions=(), kind="informational")
        act = _Bare()
        disp, state = _dispatcher(act)
        with patch("actions.ui.tap_at") as tap:
            rec = disp._offer_dialog(act, d, state)
        tap.assert_called_once()
        self.assertEqual(tap.call_args[0][:2], (1476, 126))
        self.assertIsNotNone(rec)

    def test_a_dialog_with_buttons_still_goes_to_the_game_rules(self):
        """The X must not become a shortcut past a real decision."""
        d = _Dialog(close=(1610, 230, 1662, 285), actions=[_OK, _CANCEL])
        act = _Bare()
        disp, state = _dispatcher(act)
        with patch("actions.ui.tap_at") as tap:
            disp._offer_dialog(act, d, state)
        self.assertEqual(tap.call_args[0][:2], (1308, 806), "closed instead of answering")


class ADispatchedIntentSpendsTheLookThatPrecededIt(unittest.TestCase):
    """Live 2026-09-04: EXIT_BUILDING pressed Back at 11:42:22, dismissing a notice. The next
    tick reused the PRE-Back look, still saw that notice, and tapped its OK at 11:42:25 —
    three seconds before the next perceive at 11:42:30. The notice was gone; the tap landed on
    the village beneath, opening a Gear Info modal nothing had asked for."""

    def test_fresh_is_dropped_when_an_intent_is_dispatched(self):
        import inspect
        from brain.dispatcher import Dispatcher
        src = inspect.getsource(Dispatcher._advance)
        i_dispatch = src.index("self._dispatch(intent)      # the ONE place")
        after = src[i_dispatch:]
        self.assertIn("self._fresh = None", after,
                      "the look taken before the intent is still handed to the next tick")
