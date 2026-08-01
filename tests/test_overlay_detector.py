"""Tests for vision.region_detectors.overlay."""
import unittest

from vision.region_detectors.overlay import (
    detect_overlay,
    elements_outside,
)


FW, FH = 2400, 1080


def el(label, x1, y1, x2, y2, etype="text"):
    return type("FakeEl", (), {
        "label": label,
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
        "width": x2 - x1, "height": y2 - y1,
        "element_type": etype,
    })()


class ModalDialogTests(unittest.TestCase):

    def test_confirm_button_in_centre_detects_modal(self):
        els = [
            el("Confirm purchase",  900, 400, 1500, 460, "text"),
            el("Quantity",          900, 500, 1100, 540, "text"),
            el("100",              1100, 500, 1200, 540, "text"),
            el("Total: 30,000",     900, 600, 1500, 640, "text"),
            el("Confirm",          1050, 800, 1350, 870, "button"),
        ]
        ov = detect_overlay(els, FW, FH)
        self.assertIsNotNone(ov)
        self.assertEqual(ov.kind, "modal_dialog")
        self.assertIn("confirm", ov.content_tokens)
        self.assertTrue(ov.is_modal)

    def test_cancel_button_detects_modal(self):
        els = [
            el("Are you sure?",    900, 400, 1500, 460, "text"),
            el("Details A",        900, 500, 1500, 560, "text"),
            el("Details B",        900, 600, 1500, 660, "text"),
            el("Cancel",          1050, 800, 1350, 870, "button"),
        ]
        ov = detect_overlay(els, FW, FH)
        self.assertIsNotNone(ov)
        self.assertEqual(ov.kind, "modal_dialog")

    def test_action_button_at_top_does_not_trigger(self):
        """Action buttons in the top half are scene chrome, not a modal."""
        els = [el("Confirm", 1050, 100, 1350, 170, "button")]
        ov = detect_overlay(els, FW, FH)
        self.assertIsNone(ov)

    def test_no_cluster_no_modal(self):
        """A single Confirm button without surrounding text is not a modal."""
        els = [el("Confirm", 1050, 800, 1350, 870, "button")]
        ov = detect_overlay(els, FW, FH)
        self.assertIsNone(ov)

    def test_action_button_on_far_left_no_modal(self):
        """Modal action buttons must sit in the centre band."""
        els = [
            el("Title",   200, 400,  600, 460, "text"),
            el("Body",    200, 500,  600, 560, "text"),
            el("Body2",   200, 600,  600, 660, "text"),
            el("Cancel",  100, 800,  300, 870, "button"),
        ]
        ov = detect_overlay(els, FW, FH)
        self.assertIsNone(ov)


class MainMenuTests(unittest.TestCase):

    def test_three_menu_labels_detect_menu(self):
        els = [
            el("Guild",      100, 300, 350, 360, "text"),
            el("Auction",    100, 400, 350, 460, "text"),
            el("Combat",     100, 500, 350, 560, "text"),
            el("Collection", 100, 600, 350, 660, "text"),
        ]
        ov = detect_overlay(els, FW, FH)
        self.assertIsNotNone(ov)
        self.assertEqual(ov.kind, "main_menu")
        self.assertGreaterEqual(len(ov.content_tokens), 3)

    def test_main_menu_on_right_side_still_detected(self):
        """Real-frame layout — menu items in right two-thirds."""
        els = [
            el("Combat",     2194, 475, 2294, 535, "text"),
            el("Lighthouse", 2067, 476, 2167, 536, "text"),
            el("Assault",    2321, 475, 2421, 535, "text"),
        ]
        ov = detect_overlay(els, FW, FH)
        self.assertIsNotNone(ov)
        self.assertEqual(ov.kind, "main_menu")

    def test_two_labels_too_few(self):
        els = [
            el("Guild",      100, 300, 350, 360, "text"),
            el("Auction",    100, 400, 350, 460, "text"),
        ]
        self.assertIsNone(detect_overlay(els, FW, FH))


class ElementsOutsideTests(unittest.TestCase):

    def test_filters_by_centre_inside_bbox(self):
        inside  = el("inside",  500, 500, 600, 540)
        outside = el("outside", 100, 100, 200, 140)
        bbox = (450, 450, 700, 700)
        out = elements_outside([inside, outside], bbox)
        self.assertEqual([e.label for e in out], ["outside"])

    def test_none_bbox_returns_all(self):
        e1 = el("a", 0, 0, 10, 10)
        out = elements_outside([e1], None)
        self.assertEqual(out, [e1])


if __name__ == "__main__":
    unittest.main()
