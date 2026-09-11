"""The positive control is found by COLOUR when it carries no word.

Live 2026-09-06, Barcelona. A purchase Result card — Purchase Cost 341,001, Total Amount
275,175, Balance 68,606,484,238 — whose OK button OmniParser returned as a bare `icon`:

    icon (1092,676)-(1309,749)      217x73, gold, bottom centre

`_find_action_buttons` matches action VERBS among `button`/`text` elements, so it was excluded
twice over — by type and by label — and the dialog reported `actions=[]`. Every layer
downstream then concluded the card had no button: `tap_one_positive` found nothing five times
running and the task stopped with NOTHING CHANGED for 3 ticks.

The X in the corner had been closing these cards instead, which is backwards — a dismissal
standing in for a completion (user: "using the Ok button would be the preferred method for
all the confirmation dialogs"; CLAUDE.md: *Back / Home = Cancel, not progress*).
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from vision.region_detectors.dialog import _find_action_buttons


def _el(label, box, kind="icon"):
    x1, y1, x2, y2 = box
    return types.SimpleNamespace(label=label, element_type=kind, x1=x1, y1=y1, x2=x2, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2,
                                 width=x2 - x1, height=y2 - y1)


FW, FH = 2400, 1080
OK_BOX = (1092, 676, 1309, 749)          # the real one, measured


class AnUnlabelledGoldPillIsTheOkButton(unittest.TestCase):

    def _find(self, els, *, frac=0.324, currency="ducat"):
        with mock.patch("vision.region_detectors.commit_button.yellow_fraction",
                        return_value=frac), \
             mock.patch("vision.region_detectors.commit_button.cost_currency",
                        return_value=currency), \
             mock.patch("numpy.asarray", return_value=object()):
            return _find_action_buttons(els, FW, FH, frame=mock.MagicMock())

    def test_the_gold_pill_becomes_a_positive_action(self):
        out = self._find([_el("icon", OK_BOX)])
        self.assertEqual([(a.label, a.is_positive, a.bbox) for a in out],
                         [("Ok", True, OK_BOX)])

    def test_a_READABLE_verb_still_wins_and_the_colour_pass_never_runs(self):
        """The colour pass is a fallback, not a replacement — it must not outrank a word."""
        out = self._find([_el("Cancel", (600, 700, 800, 760), kind="text"),
                          _el("icon", OK_BOX)])
        self.assertEqual([a.label for a in out], ["Cancel"])

    def test_a_RED_GEM_button_is_never_offered(self):
        """Red gems are real money. A control we cannot price is not an option at all."""
        self.assertEqual(self._find([_el("icon", OK_BOX)], currency="red_gem"), [])

    def test_a_pill_too_dull_to_be_gold_is_not_a_button(self):
        self.assertEqual(self._find([_el("icon", OK_BOX)], frac=0.05), [])

    def test_an_icon_outside_the_action_band_is_ignored(self):
        """The close X is gold-ish chrome in the TOP corner; the action band is the bottom."""
        self.assertEqual(self._find([_el("icon", (1528, 327, 1579, 380))]), [])
