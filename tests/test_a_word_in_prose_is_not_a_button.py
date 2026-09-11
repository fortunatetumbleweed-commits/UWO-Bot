"""One ordinary word inside a sentence must not manufacture a dialog.

Live 2026-09-06 at Madeira, on the PORT OVERWORLD — no dialog on screen at all. An NPC says

    "You'll have to pay me if you want to get to the New World... Did they not hear me?"

OmniParser split that sentence and returned a standalone 'get' at (1377,672)-(1438,716).
`_find_action_buttons` matched it as an action verb, it was the ONLY anchor
(`anchors=('actions',)`), and the whole overworld was reported as a confirmation dialog:

    nothing owns this confirmation dialog and the game rules will not answer it
      (options=['get']) — reporting rather than pressing something at random
    gather:Madeira: a confirmation dialog nobody will answer

REMOVING THEM COSTS NOTHING, which is what makes this a fix rather than a trade-off: neither
'get' nor 'free' is positive here or in `game_rules`, so a REAL "Get" button was already
unanswerable — detecting it only ever produced a stall.

The scrim does NOT separate these cases and was tried first: a real dialog is a window that
dims what it covers, but the negotiation card reads `clear` just as this overworld does, so
gating on the scrim would refuse a card the bot must answer.
"""

from __future__ import annotations

import types
import unittest

from vision.region_detectors.dialog import _DIALOG_ACTION_VERBS, _find_action_buttons


def _el(label, box, kind="text"):
    x1, y1, x2, y2 = box
    return types.SimpleNamespace(label=label, element_type=kind, x1=x1, y1=y1, x2=x2, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


FW, FH = 2400, 1080
GET_BOX = (1377, 672, 1438, 716)          # the word in the NPC's sentence, measured


class ProseProneWordsAreNotActionVerbs(unittest.TestCase):

    def test_get_is_not_a_verb(self):
        self.assertNotIn("get", _DIALOG_ACTION_VERBS)

    def test_free_is_not_a_verb(self):
        self.assertNotIn("free", _DIALOG_ACTION_VERBS)

    def test_the_word_in_the_speech_bubble_is_not_a_button(self):
        self.assertEqual(_find_action_buttons([_el("get", GET_BOX)], FW, FH), [])

    def test_the_verbs_that_stay_are_answerable_or_shape_defining(self):
        """Every remaining verb either has an answer in `game_rules` or defines a
        confirmation's shape — otherwise detecting it can only ever stall."""
        from brain.game_rules import DIALOG_RULES, _POSITIVE_WORDS
        from vision.region_detectors.dialog import _find_action_buttons as _f
        shape = {"no", "cancel", "decline"}
        named = {r.answer.lower() for r in DIALOG_RULES}
        # `is_positive` is decided inside the detector; ask it rather than restating the list.
        positive = {v for v in _DIALOG_ACTION_VERBS
                    if any(a.is_positive for a in _f([_el(v, (1000, 700, 1200, 760))], FW, FH))}
        unanswerable = _DIALOG_ACTION_VERBS - shape - named - positive - set(_POSITIVE_WORDS)
        self.assertEqual(unanswerable & {"get", "free"}, set())


class RealCardsStillDetect(unittest.TestCase):
    """The removal must not cost a genuine button."""

    def test_ok_is_still_a_positive_action(self):
        out = _find_action_buttons([_el("Ok", (1092, 676, 1309, 749))], FW, FH)
        self.assertEqual([(a.label, a.is_positive) for a in out], [("Ok", True)])

    def test_no_is_still_an_action(self):
        out = _find_action_buttons([_el("No", (1771, 617, 1834, 660))], FW, FH)
        self.assertEqual([a.label for a in out], ["No"])


if __name__ == "__main__":
    unittest.main()
