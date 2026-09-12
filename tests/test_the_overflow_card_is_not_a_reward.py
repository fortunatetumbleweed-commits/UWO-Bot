"""The overflow card is named by what it CONTAINS, and a village behind a modal is still a village.

Hutu Village, 2026-09-12, and it ended the mission. Three barter rounds in, the hold at
4,952/4,952, the game raised its overflow card:

    Insufficient Empty Space
    Cannot receive item due to insufficient space. Please organize your Cargo Hold.
    Unreceived trade goods will be discarded.
      Received Trade Goods :  147
      Cargo                :  4,952/4,952 (100%)
                         [ Receive ]

TWO INDEPENDENT WEAK LINKS HAD TO LINE UP, which is why this looked like a regression and was
not. Either one alone would have saved the run.

  1. `DialogModel.kind()` matched its ONE BUTTON against a word list — `receive` is in the
     reward set — so the card came back `reward`. `game_rules` then refused to answer a reward
     whose only option is Receive, which was RIGHT: accepting is the branch that DISCARDS the
     overflow. The title and body both say what the card is; nothing read them.

  2. The frame classified as `unknown`, so no activity was resolved and the card was offered to
     nobody. The family CNN read the modal as `transient@1.00`; the gate that rescues exactly
     that case asks `_has_village_menu`, which asks `detect_left_menu`, which fails on a DIMMED
     menu — the very condition a modal creates. OmniParser had already read all five menu words
     on that same frame.

WHY IT HAD NEVER BITTEN: `kind()` is only consulted when nobody owns the dialog. At San on
2026-09-10 the state resolved to `village`, `VillageActivity` claimed the card, used its own
structural test and dumped Water and Food. The label was wrong that day too.
"""
from __future__ import annotations

import types
import unittest
import unittest.mock

import brain.perceive as P
from vision.region_detectors.dialog import DialogModel


def _card(body, *labels):
    """A DialogModel with just the fields `kind()` reads. `bbox` is required and unused."""
    return DialogModel(bbox=(543, 110, 1857, 972), body_text=body,
                       actions=tuple(_action(l) for l in labels))


def _action(label):
    from vision.region_detectors.dialog import DialogAction
    try:
        return DialogAction(label=label)
    except TypeError:                      # positional-only or extra fields
        return types.SimpleNamespace(label=label)


# The card exactly as `detect_dialog` read it off frame_0396.
OVERFLOW_BODY = ("Cannot receive item due to insufficient space: Please organize your Cargo Hold.",
                 "Unreceived trade goods will be discarded.",
                 "Received Trade Goods")


class TheCardIsNamedByItsContents(unittest.TestCase):

    def test_the_live_card_is_an_overflow_not_a_reward(self):
        d = _card(OVERFLOW_BODY, "Receive")
        self.assertEqual(d.kind(), "overflow")

    def test_NOBODY_DISMISSES_IT_GENERICALLY(self):
        """Its button accepts goods and discards what will not fit, so only the activity that
        knows what is expendable may press it."""
        d = _card(OVERFLOW_BODY, "Receive")
        self.assertEqual(d.dismiss_action(), "caller_decides")
        self.assertNotEqual(d.dismiss_action(), "tap_claim")

    def test_a_REAL_reward_still_reads_as_one(self):
        """The word list is not removed, only demoted — structure is asked first."""
        d = _card(("You have received a daily login bonus.",), "Claim")
        self.assertEqual(d.kind(), "reward")

    def test_the_discard_notice_wins_over_the_card_beneath_it(self):
        """It opens OVER the overflow card, so both sets of furniture are on screen at once
        and the innermost is the live one."""
        body = OVERFLOW_BODY + ("Complete the trade without them?",)
        d = _card(body, "Receive")
        self.assertEqual(d.kind(), "discard_notice")

    def test_a_card_with_no_body_falls_through_to_the_words(self):
        d = _card((), "Ok")
        self.assertEqual(d.kind(), "system")


class AVillageBehindAModalIsStillAVillage(unittest.TestCase):

    def _frame(self, labels, width=2400):
        els = [types.SimpleNamespace(label=l, element_type="text",
                                     x1=x, y1=200 + 60 * i, x2=x + 180, y2=240 + 60 * i)
               for i, (l, x) in enumerate(labels)]
        frame = types.SimpleNamespace(width=width, height=1080)
        return frame, els

    def test_the_parse_answers_when_the_detector_cannot(self):
        """The rescue's evidence was in hand and it was asking the one reader that could not
        see it. `detect_left_menu` fails on a dimmed menu, which is what a modal creates."""
        frame, els = self._frame([("Barter", 40), ("Explore", 40), ("Gifting", 40),
                                  ("Loot", 40), ("Recruit Crew", 40)])
        with unittest.mock.patch("vision.omniparser.parse_fast_cached", return_value=els), \
             unittest.mock.patch("vision.region_detectors.left_menu.detect_left_menu",
                                 return_value=None):
            self.assertTrue(P._has_village_menu(frame))

    def test_A_FULL_SCREEN_NOTICE_COVERS_THE_MENU_AND_STILL_READS_FALSE(self):
        """What widens is only WHERE the words may be found, never the vocabulary — so a
        genuine transient, which hides the menu, is untouched."""
        frame, els = self._frame([("Effect Unlocked", 900), ("Other Mates (1)", 1800)])
        with unittest.mock.patch("vision.omniparser.parse_fast_cached", return_value=els), \
             unittest.mock.patch("vision.region_detectors.left_menu.detect_left_menu",
                                 return_value=None):
            self.assertFalse(P._has_village_menu(frame))

    def test_the_words_must_be_ON_THE_LEFT(self):
        """A centred card's own text is not a menu."""
        frame, els = self._frame([("Barter", 1100), ("Gifting", 1100)])
        with unittest.mock.patch("vision.omniparser.parse_fast_cached", return_value=els), \
             unittest.mock.patch("vision.region_detectors.left_menu.detect_left_menu",
                                 return_value=None):
            self.assertFalse(P._has_village_menu(frame))

    def test_barter_alone_is_not_a_village_menu(self):
        """The vocabulary stays exact: barter AND gifting."""
        frame, els = self._frame([("Barter", 40), ("Bulk Exch.", 40)])
        with unittest.mock.patch("vision.omniparser.parse_fast_cached", return_value=els), \
             unittest.mock.patch("vision.region_detectors.left_menu.detect_left_menu",
                                 return_value=None):
            self.assertFalse(P._has_village_menu(frame))


if __name__ == "__main__":
    unittest.main()
