"""A lit screen has no dialog on it.

User, 2026-09-07: "there is no dialog, dialog needs to dim the area outside of it, and there
is no OK, it tapped at the 110% string, it is yellow, but not a button, so the detection of
the dialog and positive button are all too loose."

`detect_dialog` fires when ANY ONE anchor is found — a close-X or an action row. Ordinary
screens carry that furniture too, so one anchor cannot mean "a dialog is present". Live
2026-09-07 at Samarai, on the plain Purchase grid (frame 49 of
trace_barter_cmd_2026-09-07T23-05-09), OmniParser labelled Palm Oil's gold `110%`
price-index chip as an action named `Ok`, and DialogModel returned a dialog spanning
(356,121)-(2240,1039) — nearly the whole screen, `title_bar=None`, one anchor.

Everything downstream trusted it:

    obstruction detected: kind='dialog' bbox=(356,121,2240,1039) signals=[..., actions=['Ok']]
    Interruptor detected: 'purchase_of_items_with_confirmation_dial' — dismissing
    tapping via DialogModel 'Ok' @ (848, 643)

The learned interruptor's broad keywords (`Purchase`, `Cargo`, `Specialties`) matched because
they were "inside the dialog" — the dialog being the whole screen — and the tap landed on the
110% chip at (816,630)-(880,656), staging 816 Palm Oil in bulk. The next tick bought them for
165,648 ducats. Nothing had chosen Palm Oil.

The scrim settles it without a baseline: `chrome_is_dimmed` reads the SCREEN'S OWN TITLE
glyphs, bright when lit and dim under the game's FLAG_DIM_BEHIND (measured ×1.98 in
docs/dialogs_are_windows.md). Measured left-gutter brightness on these frames: the plain
Purchase page 87.8 before, during and after; the cart confirm 46.8; the overflow card 8.2.

Only a definite `False` rejects. `None` means the title could not be measured, and that
function's own rule is that "could not tell" is never "nothing in the way".
"""
import os
import unittest

_TRACE = "data/sessions/trace_barter_cmd_2026-09-07T23-05-09"
_STAGE = "tests/stage_suite/frames"


def _open(path):
    from PIL import Image
    if not os.path.exists(path):
        raise unittest.SkipTest(f"frame not available: {path}")
    return Image.open(path)


def _dialog_for(path):
    from vision.region_detectors.dialog import detect_dialog
    from vision.screen_perception import parse_screen
    im = _open(path)
    inv = parse_screen(im)
    return detect_dialog(list(inv.raw_elements), im.width, im.height, frame=im)


class ALitScreenIsNotADialog(unittest.TestCase):

    def test_the_purchase_grid_that_cost_165648_ducats(self):
        self.assertIsNone(_dialog_for(f"{_STAGE}/samarai_banner_is_not_a_dialog.png"))

    def test_and_the_obstruction_classifier_agrees(self):
        from vision.obstruction_classifier import classify_obstruction, KIND_NONE
        from vision.screen_perception import parse_screen
        r = classify_obstruction(parse_screen(_open(f"{_STAGE}/samarai_banner_is_not_a_dialog.png")))
        self.assertEqual(r.kind, KIND_NONE, f"still claims {r.kind!r} at {r.bbox}")

    def test_the_village_info_PANEL_is_not_a_dialog_either(self):
        """A panel is not dimmed; it sits beside the screen rather than over it."""
        self.assertIsNone(_dialog_for(f"{_STAGE}/melanesian_textiles_is_a_material.png"))


class ARealDialogStillDetects(unittest.TestCase):
    """The guard must not cost us the dialogs the bot has to answer."""

    def test_the_empty_the_cart_confirm(self):
        self.assertIsNotNone(_dialog_for(f"{_STAGE}/faro_cart_confirm_on_tab_switch.png"))

    def test_the_overflow_card(self):
        self.assertIsNotNone(_dialog_for(f"{_STAGE}/san_overflow_pig_should_be_dumped.png"))


class TheScrimIsWhatSeparatesThem(unittest.TestCase):
    """The measurement the guard rests on, asserted rather than trusted."""

    def test_dimmed_only_on_the_real_dialogs(self):
        from actions.ui import chrome_is_dimmed
        for name, expect in ((f"{_STAGE}/samarai_banner_is_not_a_dialog.png", False),
                             (f"{_STAGE}/melanesian_textiles_is_a_material.png", False),
                             (f"{_STAGE}/faro_cart_confirm_on_tab_switch.png", True),
                             (f"{_STAGE}/san_overflow_pig_should_be_dumped.png", True)):
            with self.subTest(frame=os.path.basename(name)):
                self.assertIs(chrome_is_dimmed(_open(name)), expect)


if __name__ == "__main__":
    unittest.main()
