"""The game's amount keypad, recognised by the keys themselves.

The same card opens wherever the game wants a number, so it is not the market's
(user, 2026-09-06: "keypad dialog is used through out this game, I am considering to train a
model to recognize it, or maybe it is deterministic enough so no need using a model, just
recognize it by the keypads").

It is deterministic enough. OmniParser emits the keys as unlabelled `icon` elements of a very
consistent size, and a keypad has twelve — 0-9, `Max`, enter. Measured on
`trace_barter_cmd_2026-09-05T21-38-09`: 12 key-sized icons on frames 66 and 68, ZERO on 214
and 255. Nothing sits between the two classes.

No model, and no pixel template either — the keypad IS pixel-identical between appearances
(mean abs diff 0.03-1.30, against 43-144 for any other screen), so a template would work too,
but it would need a stored crop, a threshold and a region, and this needs none of them.
"""

from __future__ import annotations

import types
import unittest

from vision.region_detectors.keypad import keypad_bbox, keypad_is_up, keypad_keys


def _icon(x1, y1, w, h, label="icon"):
    return types.SimpleNamespace(label=label, x1=x1, y1=y1, x2=x1 + w, y2=y1 + h)


def _pad(n=12, w=120, h=105):
    """A keypad's worth of key-sized icons, laid out 3 to a row."""
    return [_icon(940 + 130 * (i % 3), 330 + 110 * (i // 3), w, h) for i in range(n)]


class AKeypadIsTwelveKeySizedIcons(unittest.TestCase):

    def test_a_full_pad_is_recognised(self):
        self.assertTrue(keypad_is_up(_pad()))

    def test_the_measured_sizes_all_count(self):
        """116-121 wide, 105-110 tall — every size seen across two staging sequences."""
        for w, h in ((116, 109), (117, 110), (119, 106), (120, 105), (121, 105)):
            self.assertTrue(keypad_is_up(_pad(w=w, h=h)), f"{w}x{h}")

    def test_a_key_lost_to_a_poor_parse_does_not_hide_the_pad(self):
        self.assertTrue(keypad_is_up(_pad(n=8)))

    def test_a_screen_with_no_keys_is_not_a_keypad(self):
        self.assertFalse(keypad_is_up([]))
        self.assertFalse(keypad_is_up([_icon(0, 0, 400, 300)]))

    def test_a_few_key_sized_icons_are_not_a_keypad(self):
        """Frames 214 and 255 carry 18 icons each and none of them key-sized; the floor is
        what stops a handful of coincidences reading as a pad."""
        self.assertFalse(keypad_is_up(_pad(n=4)))


class ItIsTheKEYS_NotTheirCaption(unittest.TestCase):

    def test_labelled_elements_are_not_keys(self):
        """A caption is the weakest evidence here (FC-3). Only unlabelled icons count."""
        labelled = [_icon(940 + 130 * i, 330, 120, 105, label="Max") for i in range(12)]
        self.assertFalse(keypad_is_up(labelled))

    def test_elements_without_geometry_are_skipped_not_crashed(self):
        broken = [types.SimpleNamespace(label="icon")] * 12
        self.assertFalse(keypad_is_up(broken))


class TheBboxIsForAimingInside(unittest.TestCase):

    def test_it_spans_the_keys(self):
        box = keypad_bbox(_pad())
        self.assertIsNotNone(box)
        x1, y1, x2, y2 = box
        self.assertLess(x1, x2)
        self.assertLess(y1, y2)

    def test_no_pad_no_box(self):
        self.assertIsNone(keypad_bbox(_pad(n=3)))
        self.assertIsNone(keypad_bbox([]))


if __name__ == "__main__":
    unittest.main()
