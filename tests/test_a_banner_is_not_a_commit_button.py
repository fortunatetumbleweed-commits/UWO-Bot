"""A goods tile holds no buttons — its gold banner is a LABEL.

User, 2026-09-10: *"the yellow banner is not a yellow button, and the wording should exclude
it; mistaking a label as button is a big issue, there are no buttons in the good tiles."*

In this game a positive button is identified by its GOLD BACKGROUND, not its wording, which
is the whole basis of `detect_commit_buttons`. A port's "Specialties" banner is painted in
that same gold across a goods tile. The shape test was the guard and it is defeated here: the
module docstring measures the banner as part of a whole tile — squarish, ~0.21 gold — but
OmniParser boxes the BANNER ALONE, 508x46 and strongly gold, which reads as a wide pill.

Live at Lisboa. The hold held 1,841 Almond, and Almond is a LISBOA SPECIALTY, so its Sell
tile carried the banner. With the basket empty the real Sell button is greyed and
undetectable, leaving the banner as the only gold thing on screen; `_find_sell_commit`'s bare
`commits[0]` returned it as the Sell button; the tap landed inside the tile, which with Put
In Bulk staged the whole stack; and the next tick sold all 1,841 — during a trim whose keep
list named Almond.

It had never fired before because the trim runs BEFORE gathering, so the hold normally
carries goods with no relationship to the port being trimmed at. Measured on the real frames:
Lisboa/Almond yields one such false commit, San/Pig+Raisin yields none.

Frame: `data/sessions/trace_barter_cmd_2026-09-10T17-46-08/frame_0046.png`.
Related: `a-fallback-fires-when-guessing-is-worst`, and CLAUDE.md's "Expected screens are
multi-anchored; positive-button search is for the UNEXPECTED".
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import numpy as np
from PIL import Image

from actions.sell_goods import _find_sell_commit
from vision.region_detectors.commit_button import detect_commit_buttons


def _el(label, x1, y1, x2, y2, kind="button"):
    return types.SimpleNamespace(label=label, element_type=kind,
                                 x1=x1, y1=y1, x2=x2, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


def _gold_frame(w=2400, h=1080):
    """All gold, so colour can never be the thing that rejects a candidate here."""
    return Image.fromarray(np.full((h, w, 3), (230, 190, 60), dtype=np.uint8))


# The Lisboa banner: wide and strongly gold, so it passes `looks_like_commit_button`.
BANNER = _el("Specialties", 432, 403, 940, 449)
# A real commit pill, for contrast.
SELL = _el("Sell", 1700, 980, 2240, 1055)


class ABannerIsNotAButton(unittest.TestCase):

    def _verbs(self, els, verb_for):
        with mock.patch("vision.region_detectors.commit_button.read_text", side_effect=verb_for):
            return [c.verb for c in detect_commit_buttons(els, _gold_frame())]

    def test_THE_SPECIALTIES_BANNER_IS_NOT_A_COMMIT(self):
        self.assertEqual(self._verbs([BANNER], lambda _img: "Specialties"), [])

    def test_a_real_sell_button_still_is(self):
        """The exclusion must be the banner alone, not gold buttons in general."""
        self.assertEqual(self._verbs([SELL], lambda _img: "Sell"), ["Sell"])

    def test_the_banner_is_dropped_from_a_mixed_screen(self):
        seen = {"n": 0}

        def _verb(_img):
            seen["n"] += 1
            return "Specialties" if seen["n"] == 1 else "Sell"

        self.assertEqual(self._verbs([BANNER, SELL], _verb), ["Sell"])

    def test_every_known_banner_word(self):
        for word in ("Specialties", "Specialty", "On Sale", "Recommended", "Favorites"):
            with self.subTest(word=word):
                self.assertEqual(self._verbs([BANNER], lambda _i, w=word: w), [])


class AnEmptyBasketHasNoCommitButton(unittest.TestCase):
    """`_find_sell_commit` must REFUSE, not fall back to whatever looked positive."""

    def test_NO_BARE_FIRST_COMMIT(self):
        other = types.SimpleNamespace(verb="Purchase", cx=1, cy=2)
        with mock.patch("vision.region_detectors.commit_button.detect_commit_buttons",
                        return_value=[other]):
            self.assertIsNone(_find_sell_commit(_gold_frame(), []))

    def test_the_sell_button_is_still_found(self):
        sell = types.SimpleNamespace(verb="Sell", cx=2100, cy=1010)
        with mock.patch("vision.region_detectors.commit_button.detect_commit_buttons",
                        return_value=[sell]):
            self.assertIs(_find_sell_commit(_gold_frame(), []), sell)

    def test_nothing_detected_is_nothing_returned(self):
        with mock.patch("vision.region_detectors.commit_button.detect_commit_buttons",
                        return_value=[]):
            self.assertIsNone(_find_sell_commit(_gold_frame(), []))


if __name__ == "__main__":
    unittest.main()
