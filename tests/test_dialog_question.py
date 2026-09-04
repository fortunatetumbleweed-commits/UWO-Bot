"""The buttons are the answer space — so only a button can come out.

The ask the user asked for (2026-08-26): "I see a dialog, it has this title, this body
text, and an OK button, and a Cancel button, what should I do."

These tests care about two things, and they are the same thing from both ends:

  * the QUESTION carries title, body and the enumerated buttons, plus where the bot is and
    what it already tried;
  * the ANSWER can only ever be one of those buttons — a reply that names none of them, or
    names two, or discusses the choice, produces None rather than a guess.
"""

from __future__ import annotations

import unittest

from brain.dialog_question import (GAME, Situation, choose, decider, describe,
                                   match, question)
from vision.region_detectors.dialog import DialogAction, DialogModel, TitleBar


def _dialog(title="Notice", body=("Recruit Crew? 181,224 ducats will be spent.",),
            buttons=(("Cancel", False), ("OK", True))):
    return DialogModel(
        bbox=(726, 0, 1673, 844),
        title_bar=TitleBar(bbox=(726, 0, 1673, 90), text=title),
        body_text=tuple(body),
        actions=tuple(DialogAction(label=l, bbox=(0, 0, 10, 10), is_positive=p)
                      for l, p in buttons),
    )


# The live case, 2026-08-26 at Lisboa.
CREW = Situation(place="the HARBOR at Lisboa",
                 goal="set sail for Amsterdam",
                 tried=("The Depart button was inactive, so I tapped Recruit Crew.",))


def _question(d):
    return question(d.title_bar.text if d.title_bar else None,
                    d.body_text, [a.label for a in d.actions], CREW)


class TheQuestionDescribesTheDialog(unittest.TestCase):

    def test_it_carries_title_body_and_buttons(self):
        q = _question(_dialog())
        self.assertIn('"Notice"', q)
        self.assertIn("181,224 ducats", q)
        self.assertIn("[Cancel]", q)
        self.assertIn("[OK]", q)

    def test_it_says_where_the_bot_is_and_what_it_wants(self):
        q = _question(_dialog())
        self.assertIn("HARBOR at Lisboa", q)
        self.assertIn("set sail for Amsterdam", q)

    def test_it_says_what_was_already_tried(self):
        self.assertIn("Depart button was inactive", _question(_dialog()))

    def test_it_explains_the_game(self):
        """Without it, 181,224 ducats reads as an alarming purchase rather than a fare."""
        q = _question(_dialog())
        self.assertIn("Uncharted Waters", q)
        self.assertIn("cannot depart without enough crew", q)

    def test_it_sends_no_screenshot(self):
        """A description survives a re-skin and can key what was learned; a picture cannot."""
        self.assertIsInstance(_question(_dialog()), str)

    def test_a_dialog_with_no_buttons_says_so(self):
        self.assertIn("only a close X", describe("Notice", ("body",), ()))


class OnlyAButtonCanComeOut(unittest.TestCase):

    def _choose(self, reply, dialog=None):
        return choose(dialog or _dialog(), CREW, ask=lambda _p: reply)

    def test_an_exact_label_is_taken(self):
        self.assertEqual(self._choose("OK").label, "OK")

    def test_a_label_inside_a_sentence_is_taken(self):
        self.assertEqual(self._choose("You should tap OK to hire the crew.").label, "OK")

    def test_a_reply_naming_neither_button_is_refused(self):
        self.assertIsNone(self._choose("Go back to the port and try later."))

    def test_a_reply_naming_both_buttons_is_refused(self):
        """Discussing the choice is not making it."""
        self.assertIsNone(self._choose("You could press OK, or Cancel if funds are short."))

    def test_an_empty_reply_is_refused(self):
        self.assertIsNone(self._choose(""))

    def test_a_failed_ask_is_refused_not_guessed(self):
        def boom(_p):
            raise RuntimeError("no network")
        self.assertIsNone(choose(_dialog(), CREW, ask=boom))

    def test_the_returned_button_carries_its_bbox(self):
        """The answer round-trips to a tap without a second lookup."""
        d = DialogModel(bbox=(0, 0, 100, 100),
                        actions=(DialogAction(label="OK", bbox=(60, 80, 90, 95),
                                              is_positive=True),))
        self.assertEqual(choose(d, CREW, ask=lambda _p: "OK").bbox, (60, 80, 90, 95))


class RedGemsAreNeverAuthorised(unittest.TestCase):
    """The one spend ruled out in advance (user, 2026-08-25).

    Blue gems are earned through investment and ordinary play, so they are not special.
    Red gems are bought with real money. This is enforced where the choice is MADE, not by
    asking the model to respect it — a rule a model can be talked past is not a rule.
    """

    def test_a_red_gem_spend_is_not_answered(self):
        d = _dialog(body=("Purchase with 300 Red Gems?",))
        self.assertIsNone(choose(d, CREW, ask=lambda _p: "OK"))

    def test_the_model_is_not_even_asked(self):
        asked = []
        d = _dialog(body=("Spend 300 red gems to refill?",))
        choose(d, CREW, ask=lambda p: asked.append(p) or "OK")
        self.assertEqual(asked, [], "the veto must come before the ask, not after it")

    def test_an_ordinary_ducat_spend_is_unaffected(self):
        self.assertEqual(self._ok(), "OK")

    def _ok(self):
        return choose(_dialog(), CREW, ask=lambda _p: "OK").label


class MatchIsTheBoundary(unittest.TestCase):
    """Named separately in the module because everything rests on it."""

    LABELS = ("Cancel", "OK")

    def test_case_and_space_do_not_matter(self):
        self.assertEqual(match("  ok  ", self.LABELS), "OK")

    def test_nothing_matches_nothing(self):
        self.assertIsNone(match("Confirm", self.LABELS))


if __name__ == "__main__":
    unittest.main()


class ItPlugsIntoUnexpected(unittest.TestCase):
    """`brain.unexpected` classifies an ACTION_DIALOG and then refuses to answer it —
    "the whole point of case 2 is that the decision is not the state machine's to make".
    `decider()` is who makes it, so the two halves must actually meet.
    """

    def _unexpected(self, title="Notice", body=("Recruit Crew? 181,224 ducats will be spent.",),
                    options=("Cancel", "OK")):
        from brain.unexpected import ACTION_DIALOG, Unexpected
        return Unexpected(case=ACTION_DIALOG, state="building", title=title,
                          text=tuple(body), options=tuple(options), positive="OK")

    def test_resolve_taps_the_button_the_decider_names(self):
        from brain.unexpected import resolve
        tapped = []
        res = resolve(self._unexpected(),
                      decide=decider(CREW, ask=lambda _p: "OK"),
                      tap_label=lambda l: tapped.append(l) or True)
        self.assertTrue(res["handled"])
        self.assertEqual(tapped, ["OK"])

    def test_the_title_reaches_the_question(self):
        """It was dropped by `look()` until 2026-08-26, leaving two thirds of a dialog."""
        seen = []
        decider(CREW, ask=lambda p: seen.append(p) or "OK")(
            self._unexpected(title="Insufficient Funds"))
        self.assertIn("Insufficient Funds", seen[0])

    def test_an_unanswerable_dialog_leaves_the_screen_alone(self):
        from brain.unexpected import resolve
        tapped = []
        res = resolve(self._unexpected(),
                      decide=decider(CREW, ask=lambda _p: "hmm, hard to say"),
                      tap_label=lambda l: tapped.append(l) or True)
        self.assertFalse(res["handled"])
        self.assertEqual(tapped, [], "nothing may be tapped on an answer that named no button")

    def test_look_carries_the_title_out_of_the_frame(self):
        """The field exists AND is populated — a default of None would pass the test above."""
        import types
        from unittest.mock import patch
        from brain import unexpected as _u
        d = _dialog(title="Notice")
        with patch.object(_u, "_read_dialog", return_value=d), \
             patch("actions.sail_actions.where_am_i", return_value={"location": "building"}):
            u = _u.look(frame=types.SimpleNamespace(width=1920, height=1080))
        self.assertEqual(u.title, "Notice")
