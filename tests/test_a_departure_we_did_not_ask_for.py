"""A notice for somewhere else is not "no notice", and a panel for somewhere else is not ours.

Live 2026-09-01, the failure both of these prevent. The world map could not find Barcelona;
taps meant for the port list opened FARO's City Info panel instead; `_on_location_info` found
the gold commit control — correctly, it is labelled "Go to City" — and pressed it on Faro. The
game asked:

    Notice
    Moving to Faro after Auto Supply. Continue?          [Cancel] [OK]

and `confirm_departure_notice("Barcelona")` was asked about it. Its one test was

    markers present AND the destination is named

so a notice naming Faro failed it and took the branch meaning "there is nothing on screen":

    return {"seen": False, "confirmed": False, "reason": "no notice"}

`_commit_departure` reads that as `not (seen and not confirmed)` -> True -> SUCCESS. The
mission was told its course was set for Barcelona, walked to the harbour, tapped Supply
Departure, and set sail with no destination at all. It reached the sea and spent the next
twenty minutes asking to enter a market that cannot exist there.

Two separate holes, either of which stops it:
  * the notice test conflated "no dialog" with "a dialog for elsewhere";
  * nothing checked whose panel the commit button belonged to.
"""
import types
import unittest
from unittest import mock

from actions.sail_actions import (_is_departure_notice, _looks_like_departure_notice,
                                  _notice_names)

FARO = ("Notice Moving to Faro after Auto Supply. Continue? Fleet will immediately set sail "
        "if Auto Supply is not possible. Cancel OK")
BARCELONA = FARO.replace("Faro", "Barcelona")
UNRELATED = "Notice Your ship has been repaired. OK"


class TheTwoQuestionsAreAsked_Separately(unittest.TestCase):
    def test_a_departure_notice_is_recognised_whatever_it_names(self):
        self.assertTrue(_is_departure_notice(FARO))
        self.assertTrue(_is_departure_notice(BARCELONA))

    def test_an_unrelated_notice_is_not_one(self):
        self.assertFalse(_is_departure_notice(UNRELATED))

    def test_naming_is_a_separate_answer(self):
        self.assertTrue(_notice_names(FARO, "Faro"))
        self.assertFalse(_notice_names(FARO, "Barcelona"))

    def test_accents_still_fold(self):
        self.assertTrue(_notice_names(FARO.replace("Faro", "Malé"), "Male"))

    def test_the_combined_test_still_answers_as_before(self):
        self.assertTrue(_looks_like_departure_notice(BARCELONA, "Barcelona"))
        self.assertFalse(_looks_like_departure_notice(FARO, "Barcelona"))


def _confirm(text, destination, *, then=None, attempts=1):
    """`text` is what the first look reads; `then` what every later look reads.

    Confirming takes TWO looks by design — tap, then see it gone — so a test that wants the
    success path must give the second look something without the notice in it.
    """
    from actions import sail_actions as SA
    seen = {"n": 0}

    def ocr(_frame, min_conf=0.3):
        seen["n"] += 1
        shown = text if seen["n"] == 1 or then is None else then
        return [(shown, 0.9, 0, 0)]

    with mock.patch.object(SA, "capture_screen", lambda: object()), \
         mock.patch.object(SA, "_ocr_frame", ocr), \
         mock.patch("actions.ui.tap_text", return_value=True), \
         mock.patch("actions.ui.settle"):
        return SA.confirm_departure_notice(destination, attempts=attempts)


class ANoticeForSomewhereElseIsNotNoNotice(unittest.TestCase):
    def test_it_is_seen(self):
        res = _confirm(FARO, "Barcelona")
        self.assertTrue(res["seen"], "'no notice' is what made this look like success")

    def test_it_is_not_confirmed(self):
        res = _confirm(FARO, "Barcelona")
        self.assertFalse(res["confirmed"], "tapping OK would sail to a port nobody chose")

    def test_the_reason_says_which_way_it_is_wrong(self):
        self.assertIn("somewhere other than", _confirm(FARO, "Barcelona")["reason"])

    def test_the_caller_reads_it_as_failure(self):
        # `_commit_departure` computes `not (seen and not confirmed)`.
        res = _confirm(FARO, "Barcelona")
        self.assertFalse(not (res["seen"] and not res["confirmed"]),
                         "this is the expression that returned True and set the mission going")

    def test_a_genuinely_absent_notice_is_still_success(self):
        res = _confirm(UNRELATED, "Barcelona")
        self.assertFalse(res["seen"])
        self.assertTrue(not (res["seen"] and not res["confirmed"]),
                        "the game only raises this dialog sometimes; absence is normal")

    def test_the_right_notice_is_confirmed(self):
        res = _confirm(BARCELONA, "Barcelona", then=UNRELATED, attempts=3)
        self.assertTrue(res["seen"] and res["confirmed"])

    def test_the_wrong_notice_is_refused_before_any_tap_is_spent(self):
        """It never reaches the OK, however many attempts it is given."""
        res = _confirm(FARO, "Barcelona", then=FARO, attempts=3)
        self.assertFalse(res["confirmed"])
        self.assertIn("somewhere other than", res["reason"])


class TheCommitButtonSaysWhatItDoesNotWhere(unittest.TestCase):
    """`_on_location_info` must ask whose panel is open before pressing its button."""

    def _run(self, panel_for, commit=True):
        from brain.activities.world_map import WorldMapActivity
        from brain.mission import Opportunity  # noqa: F401  (import parity with the module)
        committed = []
        a = WorldMapActivity(commit_fn=lambda w: committed.append(w) or commit,
                             panel_for_fn=lambda w: panel_for)
        goal = types.SimpleNamespace(where="Barcelona", kind="port")
        return a._on_location_info(goal), committed

    def test_another_citys_panel_is_not_committed(self):
        """The subject — no departure nobody asked for — is unchanged.

        The status is not BLOCKED any more: since 2026-09-06 the activity CLOSES the wrong
        panel and hands back WORKING, because refusing alone left it covering the map and the
        retry met the identical screen. See `tests/test_the_wrong_panel_is_closed.py`."""
        from brain.dispatcher import WORKING
        res, committed = self._run(panel_for=False)
        self.assertEqual(res.status, WORKING)
        self.assertEqual(committed, [], "Faro's panel is not Barcelona's course")

    def test_our_own_panel_is_committed(self):
        from brain.dispatcher import FINISHED
        res, committed = self._run(panel_for=True)
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(committed, ["Barcelona"])

    def test_an_unreadable_panel_does_not_block_a_correct_departure(self):
        """Unknown is not "no" — refusing here would strand a departure that was fine."""
        from brain.dispatcher import FINISHED
        res, committed = self._run(panel_for=None)
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(committed, ["Barcelona"])


class ThePanelIsReadNotTheMapBehindIt(unittest.TestCase):
    def test_a_port_name_on_the_map_does_not_answer_for_the_panel(self):
        """The failing frame showed Porto, Azores, Madeira, Valencia and Tunis on the map.

        A whole-frame search for the destination answers yes almost anywhere, which is why
        the question is asked of the PANEL REGION only.
        """
        from actions.sail_actions import location_panel_is_for

        class _Frame:
            def __init__(self): self.box = None
            def crop(self, box):
                self.box = box
                return self

        fr = _Frame()
        with mock.patch("actions.sail_actions._ocr_frame",
                        lambda f, min_conf=0.3: [("City Info Faro Territory", 0.9, 0, 0)]):
            self.assertFalse(location_panel_is_for(fr, "Barcelona"))
            self.assertTrue(location_panel_is_for(fr, "Faro"))
        self.assertIsNotNone(fr.box, "it must crop to the panel, not read the whole frame")
        self.assertGreater(fr.box[0], 1000, "the panel is the right-hand region")


if __name__ == "__main__":
    unittest.main()
