"""Tests for vision.region_detectors.building_npc_overlay."""
import unittest

from vision.region_detectors.building_npc_overlay import (
    detect_building_npc_overlay,
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


class BuildingNpcOverlayTests(unittest.TestCase):

    def test_big_icon_plus_speech_fires(self):
        """Quoted dialogue case (frame 0050 family)."""
        els = [
            el("icon", 600, 350, 1700, 950, "icon"),     # big NPC art
            el("Market Owner", 850, 680, 1130, 730, "button"),
            el("Hmm; never mind.", 870, 750, 1180, 790, "text"),
            el("Let's not bring this up again.", 870, 800, 1340, 830, "text"),
        ]
        ov = detect_building_npc_overlay(els, FW, FH)
        self.assertIsNotNone(ov)
        self.assertEqual(ov.dismiss_action(), "tap_continue")
        self.assertIsNotNone(ov.npc_art_bbox)
        self.assertIn("big_icon", ov.anchors_fired)
        self.assertIn("speech", ov.anchors_fired)
        self.assertEqual(ov.speaker, "Market Owner")

    def test_big_icon_plus_bare_speaker_fires(self):
        """Bare-short-cap speaker near big icon fires the big_icon+speaker combo."""
        els = [
            el("icon", 600, 350, 1700, 950, "icon"),     # big NPC art
            el("Crew", 850, 680, 1000, 730, "button"),
        ]
        ov = detect_building_npc_overlay(els, FW, FH)
        self.assertIsNotNone(ov)
        self.assertIn("big_icon", ov.anchors_fired)
        self.assertEqual(ov.speaker, "Crew")

    def test_strong_speaker_plus_speech_no_big_icon(self):
        """Frame 0063 case: 'Welcome:' colon greeting + speech text."""
        els = [
            el("Welcome:", 1100, 800, 1300, 850, "button"),
            el("Do you know about discoveries?", 900, 850, 1300, 900, "text"),
        ]
        ov = detect_building_npc_overlay(els, FW, FH)
        self.assertIsNotNone(ov)
        self.assertIn("speaker", ov.anchors_fired)
        self.assertIn("speech", ov.anchors_fired)
        self.assertIsNone(ov.npc_art_bbox)

    def test_suffix_speaker_plus_speech_no_big_icon(self):
        els = [
            el("Harbor Official", 900, 600, 1180, 650, "button"),
            el("The crew is ready.", 900, 700, 1300, 740, "text"),
        ]
        ov = detect_building_npc_overlay(els, FW, FH)
        self.assertIsNotNone(ov)
        self.assertEqual(ov.speaker, "Harbor Official")

    def test_action_only_does_not_fire(self):
        """A modal dialog (Confirm/Cancel + body) should NOT match NPC overlay."""
        els = [
            el("Are you sure?", 900, 300, 1500, 360, "text"),
            el("Confirm", 1050, 800, 1250, 870, "button"),
            el("Cancel",  1300, 800, 1500, 870, "button"),
        ]
        self.assertIsNone(detect_building_npc_overlay(els, FW, FH))

    def test_empty_scene_no_fire(self):
        self.assertIsNone(detect_building_npc_overlay([], FW, FH))


if __name__ == "__main__":
    unittest.main()
