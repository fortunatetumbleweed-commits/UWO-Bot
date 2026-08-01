"""Tests for DialogModel structural detection."""
import unittest

from vision.region_detectors.dialog import (
    detect_dialog,
    DialogModel,
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


class ConfirmationDialogTests(unittest.TestCase):

    def test_confirm_cancel_pair_detects(self):
        els = [
            el("Are you sure?", 900, 300, 1500, 360, "text"),
            el("Details",       900, 400, 1500, 460, "text"),
            el("More details",  900, 500, 1500, 560, "text"),
            el("Confirm",      1050, 800, 1250, 870, "button"),
            el("Cancel",       1300, 800, 1500, 870, "button"),
        ]
        d = detect_dialog(els, FW, FH)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind(), "confirmation")
        self.assertEqual(d.dismiss_action(), "caller_decides")
        self.assertIn("actions", d.anchors_fired)


class InformationalDialogTests(unittest.TestCase):

    def test_x_close_with_no_actions_detects_informational(self):
        """0005-style: dialog with X close, body text, but no actions."""
        els = [
            el("Recruit Crew",  900, 260, 1300, 300, "text"),
            el("icon",         1540, 230,  1590, 285, "icon"),    # X close
            el("Body line 1",  700, 400, 1700, 440, "text"),
            el("Body line 2",  700, 460, 1700, 500, "text"),
            el("Body line 3",  700, 520, 1700, 560, "text"),
        ]
        d = detect_dialog(els, FW, FH)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind(), "informational")
        self.assertEqual(d.dismiss_action(), "tap_close")
        self.assertIn("close", d.anchors_fired)
        self.assertIsNotNone(d.close_button)


class RewardDialogTests(unittest.TestCase):

    def test_claim_action_detects_reward(self):
        els = [
            el("Reward!",     900, 400, 1500, 460, "text"),
            el("Blue Gem x5", 900, 500, 1500, 560, "text"),
            el("Treasure",    900, 600, 1500, 660, "text"),
            el("Claim",      1100, 800, 1300, 870, "button"),
        ]
        d = detect_dialog(els, FW, FH)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind(), "reward")
        self.assertEqual(d.dismiss_action(), "tap_claim")


class SystemDialogTests(unittest.TestCase):

    def test_ok_only_detects_system(self):
        els = [
            el("Notice",       900, 400, 1500, 460, "text"),
            el("Message body", 900, 500, 1500, 560, "text"),
            el("More body",    900, 600, 1500, 660, "text"),
            el("OK",          1100, 800, 1300, 870, "button"),
        ]
        d = detect_dialog(els, FW, FH)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind(), "system")
        self.assertEqual(d.dismiss_action(), "tap_ok")


class NoDialogTests(unittest.TestCase):

    def test_sparse_scene_returns_none(self):
        """Building list or sub_menu screens shouldn't fire."""
        # Action verbs on the right side (outside centre band) — no fire.
        els = [
            el("Harbor",  2200, 200, 2400, 260, "text"),
            el("Market",  2200, 300, 2400, 360, "text"),
            el("Bank",    2200, 400, 2400, 460, "text"),
        ]
        self.assertIsNone(detect_dialog(els, FW, FH))

    def test_chrome_only_returns_none(self):
        """Top-row chrome icons must not be mistaken for X close."""
        els = [
            el("icon", 2335, 48, 2390, 100, "icon"),   # top chrome
            el("icon", 2244, 47, 2316, 122, "icon"),
        ]
        self.assertIsNone(detect_dialog(els, FW, FH))


class DialogDoesNotFireOnNpcOverlay(unittest.TestCase):
    """Dialog is strictly defined: title bar + body + optional X + optional
    actions.  Building NPC overlays (large NPC art + speech) are NOT a
    Dialog and live in vision.region_detectors.building_npc_overlay."""

    def test_npc_overlay_shape_does_not_fire_dialog(self):
        els = [
            el("icon", 600, 350, 1700, 950, "icon"),
            el("Market Owner", 850, 680, 1130, 730, "button"),
            el("Hmm; never mind.", 870, 750, 1180, 790, "text"),
            el("Let's not bring this up again.", 870, 800, 1340, 830, "text"),
        ]
        # Should NOT trigger DialogModel — no title bar, no X close,
        # no action verbs.
        self.assertIsNone(detect_dialog(els, FW, FH))


if __name__ == "__main__":
    unittest.main()
