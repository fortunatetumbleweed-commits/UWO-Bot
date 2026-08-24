"""A dialog's anchors must belong to that dialog, not to whatever else is on screen.

Live 2026-08-23 — the departure Notice on the world map, with the Village Info panel open
beside it:

    [obstruction_classifier] dialog-detector disagreement: DialogModel says clean, legacy
    heuristic says dialog (legacy signals=['cluster_size=11', "action_row=['Cancel','Ok']"],
    bbox=(826, 240, 1985, 863)). Suppressing legacy verdict.
    kind = none

`_find_x_close` returns the RIGHTMOST close-X on the frame — the side panel's at (2180,118),
not the dialog's. Averaging it with the action row put the centroid at cx=1535, widened the
band to (575,2400), and the cluster swallowed the panel: bbox (826,113)-(2237,1044), height
931 > the 864 "this is an info panel" guard. A modal blocking the bot was reported as no
obstruction at all, so the bot proceeded as though nothing was in its way — and that dialog
is the one warning "Fleet will immediately set sail if Auto Supply is not possible".

The action row is the reliable anchor: unambiguous, and inside the dialog by construction.
"""

from __future__ import annotations

import unittest

from vision.omniparser import DetectedElement
from vision.region_detectors.dialog import detect_dialog

FW, FH = 2400, 1080


def _el(label, cx, cy, w=140, h=40, element_type="button"):
    return DetectedElement(label=label, element_type=element_type,
                           x1=cx - w // 2, y1=cy - h // 2,
                           x2=cx + w // 2, y2=cy + h // 2, confidence=0.9)


# Measured on the live frame.
DIALOG = [
    _el("Notice", 1202, 471, element_type="button"),
    _el("Moving to Melanesian Village after Auto Supply:", 1198, 371, w=600, element_type="text"),
    _el("Fleet will immediately set sail if Auto Supply is not", 1202, 497, w=620, element_type="text"),
    _el("Cancel", 1091, 826),
    _el("Ok", 1306, 826),
]
# The Village Info side panel that sits beside it — including ITS close-X.
SIDE_PANEL = [
    _el("X", 2207, 145, w=55, h=55, element_type="icon"),
    _el("Melanesian Village", 1961, 210),
    _el("Trusting", 1961, 345, element_type="text"),
    _el("Daily Barter Progress", 1843, 455, w=300, element_type="text"),
    _el("Weekly Reward", 1961, 770),
]


class AnchorsBelongToOneWidget(unittest.TestCase):

    def test_the_dialog_is_detected_beside_an_open_side_panel(self):
        d = detect_dialog(DIALOG + SIDE_PANEL, FW, FH)
        self.assertIsNotNone(d, "a modal with Ok/Cancel must not read as 'no obstruction'")

    def test_the_action_row_is_reported(self):
        d = detect_dialog(DIALOG + SIDE_PANEL, FW, FH)
        labels = {a.label.lower() for a in d.actions}
        self.assertIn("ok", labels)
        self.assertIn("cancel", labels)

    def test_the_positive_action_is_identified(self):
        d = detect_dialog(DIALOG + SIDE_PANEL, FW, FH)
        positives = [a.label for a in d.actions if a.is_positive]
        self.assertEqual(positives, ["Ok"])

    def test_the_bbox_does_not_swallow_the_side_panel(self):
        """The panel starts around x=1690; a dialog bbox reaching it is the bug."""
        d = detect_dialog(DIALOG + SIDE_PANEL, FW, FH)
        self.assertLess(d.bbox[2], 1690)

    def test_it_still_detects_with_no_side_panel(self):
        self.assertIsNotNone(detect_dialog(DIALOG, FW, FH))

    def test_a_lone_side_panel_is_still_not_a_dialog(self):
        """The guard's original job — the 2026-05-23 Berber info panel — must survive."""
        self.assertIsNone(detect_dialog(SIDE_PANEL, FW, FH))


if __name__ == "__main__":
    unittest.main()
