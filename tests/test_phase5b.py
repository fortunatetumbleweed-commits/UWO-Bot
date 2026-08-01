"""
Phase 5b tests — vision-led port_overworld fallback + UI elements KB.

Two artefacts:
  1. memory/knowledge/control/ui_elements.json — catalogue of recognised
     UI element types (chrome / in-world overlays / modal dialogs).
  2. _confirm_in_town_moondream + _classify_nav_state fallback wiring —
     when rule-based classification fails AND Moondream confirms 'in
     town', return port_overworld instead of falling through to
     unknown / sea_cinematic.

Coverage:
  - UI elements KB file exists, parses, has expected categories
  - _classify_nav_state reaches port_overworld via vision fallback when
    rule-based path can't classify (port_name=None, no right_panel)
  - sea_cinematic still fires when both in_town and at_sea say no/yes
  - unknown still fires when vision can't decide
  - vision module unavailable → graceful degradation
"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class UIElementsKBTests(unittest.TestCase):
    """The seed catalogue of UI element types is checked into the repo."""

    def test_kb_file_exists_and_parses(self):
        path = Path("memory/knowledge/control/ui_elements.json")
        self.assertTrue(path.exists(), "ui_elements.json must be checked in")
        data = json.loads(path.read_text())
        self.assertIn("categories", data)

    def test_three_required_categories_present(self):
        path = Path("memory/knowledge/control/ui_elements.json")
        data = json.loads(path.read_text())
        cats = data["categories"]
        self.assertIn("chrome", cats)
        self.assertIn("in_world_overlays", cats)
        self.assertIn("modal_dialogs", cats)

    def test_in_world_overlays_documents_the_three_types(self):
        path = Path("memory/knowledge/control/ui_elements.json")
        data = json.loads(path.read_text())
        elements = data["categories"]["in_world_overlays"]["elements"]
        for kind in ("building_name_plate", "npc_speech_bubble",
                     "proximity_action_button"):
            self.assertIn(kind, elements,
                          f"{kind} must be documented in ui_elements.json")

    def test_phase_5b_perceive_rules_documented(self):
        """The KB documents the new classification rules so the design
        intent is reviewable alongside the data."""
        path = Path("memory/knowledge/control/ui_elements.json")
        data = json.loads(path.read_text())
        self.assertIn("phase_5b_perceive_rules", data)
        rules = data["phase_5b_perceive_rules"]["rules"]
        self.assertTrue(any("in town" in r.lower() for r in rules))


class VisionLedFallbackTests(unittest.TestCase):
    """
    The _classify_nav_state fallback path consults Moondream's 'is in
    town?' / 'is at sea?' questions ONLY when there is at least one
    positive structural signal on screen (rich OmniParser elements, even
    if no fingerprint matched).  When the screen is bare (sparse elements
    AND no chrome AND no readable text), the classifier returns `pending`
    instead — that signals "3D world still rendering, caller should poll"
    and prevents Moondream from being asked the wrong question on a
    half-rendered post-reload frame (the 2026-05-12 cascade).

    These tests pin both branches: pending on sparse, Moondream on rich.
    """

    def _make_fake_frame(self, w=2400, h=1080):
        """Stub PIL Image with crop/copy/thumbnail/save methods used
        by _classify_nav_state.  Returns a MagicMock with width/height."""
        frame = MagicMock()
        frame.width  = w
        frame.height = h
        frame.crop.return_value   = frame
        frame.copy.return_value   = frame
        frame.thumbnail.return_value = None
        return frame

    def _make_chrome(self, has_home=False, has_back_arrow=False,
                      has_hamburger=False, has_right_panel=False):
        c = MagicMock()
        c.has_home         = has_home
        c.has_back_arrow   = has_back_arrow
        c.has_hamburger    = has_hamburger
        c.has_right_panel  = has_right_panel
        return c

    def _patch_omni_elements(self, count):
        """Patch OmniParser to be available and return `count` stub elements.
        Returns the context manager stack as a list so callers can use
        ExitStack — we instead inline this via two patches per test."""
        return count

    def test_vision_in_town_yes_recovers_port_overworld_when_rich(self):
        """
        Rule-based path fails (no chrome, no port name, no sea HUD, no
        menu tokens), but OmniParser saw rich UI (>= 5 elements) so the
        `pending` gate doesn't fire, and Moondream says 'in town' —
        classifier recovers port_overworld via vision fallback.  This is
        the Phase 5B in-world-overlay-recovery path.
        """
        from brain.perceive import _classify_nav_state

        with patch("vision.omniparser.get_omniparser") as MockOmni, \
             patch("vision.omniparser.parse_fast_cached",
                    return_value=[object()] * 8), \
             patch("vision.screen_classifier.classify_screen") as MockClassify, \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.ocr.read_port_name", return_value=None), \
             patch("actions.sail_actions._ocr_frame", return_value=[]), \
             patch("actions.sail_actions._is_loading_screen", return_value=False), \
             patch("actions.sail_actions._confirm_in_town_moondream",
                    return_value=True) as mock_in_town, \
             patch("actions.sail_actions._confirm_at_sea_moondream") as mock_at_sea:
            MockOmni.return_value.yolo_available.return_value = True
            MockClassify.return_value = MagicMock(state="unknown",
                                                    confidence="low",
                                                    signals=[], detail="no match",
                                                    port=None)
            MockChrome.return_value.detect.return_value = self._make_chrome()

            result = _classify_nav_state(self._make_fake_frame())
            self.assertEqual(result["location"], "port_overworld")
            self.assertIn("port_overworld_with_overlay", result["detail"])
            mock_in_town.assert_called_once()
            mock_at_sea.assert_not_called()  # short-circuited

    def test_sparse_elements_returns_pending_without_moondream(self):
        """
        Sparse OmniParser output (< 5 elements) AND no chrome AND no
        readable text → `pending`.  Moondream must NOT be called — the
        question is wrong for a half-rendered frame.
        """
        from brain.perceive import _classify_nav_state

        with patch("vision.omniparser.get_omniparser") as MockOmni, \
             patch("vision.omniparser.parse_fast_cached",
                    return_value=[object()]), \
             patch("vision.screen_classifier.classify_screen") as MockClassify, \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.ocr.read_port_name", return_value=None), \
             patch("actions.sail_actions._ocr_frame", return_value=[]), \
             patch("actions.sail_actions._is_loading_screen", return_value=False), \
             patch("actions.sail_actions._confirm_in_town_moondream") as mock_in_town, \
             patch("actions.sail_actions._confirm_at_sea_moondream") as mock_at_sea:
            MockOmni.return_value.yolo_available.return_value = True
            MockClassify.return_value = MagicMock(state="unknown",
                                                    confidence="low",
                                                    signals=[], detail="no match",
                                                    port=None)
            MockChrome.return_value.detect.return_value = self._make_chrome()

            result = _classify_nav_state(self._make_fake_frame())
            self.assertEqual(result["location"], "pending")
            self.assertIn("rendering", result["detail"].lower())
            mock_in_town.assert_not_called()
            mock_at_sea.assert_not_called()

    def test_rich_elements_both_no_returns_unknown(self):
        """Rich OmniParser, no chrome, no port name; Moondream both say
        no → unknown.  Preserves the prior contract for that branch."""
        from brain.perceive import _classify_nav_state

        with patch("vision.omniparser.get_omniparser") as MockOmni, \
             patch("vision.omniparser.parse_fast_cached",
                    return_value=[object()] * 8), \
             patch("vision.screen_classifier.classify_screen") as MockClassify, \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.ocr.read_port_name", return_value=None), \
             patch("actions.sail_actions._ocr_frame", return_value=[]), \
             patch("actions.sail_actions._is_loading_screen", return_value=False), \
             patch("actions.sail_actions._confirm_in_town_moondream",
                    return_value=False), \
             patch("actions.sail_actions._confirm_at_sea_moondream",
                    return_value=False):
            MockOmni.return_value.yolo_available.return_value = True
            MockClassify.return_value = MagicMock(state="unknown",
                                                    confidence="low",
                                                    signals=[], detail="no match",
                                                    port=None)
            MockChrome.return_value.detect.return_value = self._make_chrome()

            result = _classify_nav_state(self._make_fake_frame())
            self.assertEqual(result["location"], "unknown")
            self.assertIn("unrecognised layout", result["detail"])

    def test_rich_elements_at_sea_unsure_returns_sea_cinematic(self):
        """Rich OmniParser, in_town=False, at_sea=None → preserves the
        prior fallback to sea_cinematic."""
        from brain.perceive import _classify_nav_state

        with patch("vision.omniparser.get_omniparser") as MockOmni, \
             patch("vision.omniparser.parse_fast_cached",
                    return_value=[object()] * 8), \
             patch("vision.screen_classifier.classify_screen") as MockClassify, \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.ocr.read_port_name", return_value=None), \
             patch("actions.sail_actions._ocr_frame", return_value=[]), \
             patch("actions.sail_actions._is_loading_screen", return_value=False), \
             patch("actions.sail_actions._confirm_in_town_moondream",
                    return_value=False), \
             patch("actions.sail_actions._confirm_at_sea_moondream",
                    return_value=None):
            MockOmni.return_value.yolo_available.return_value = True
            MockClassify.return_value = MagicMock(state="unknown",
                                                    confidence="low",
                                                    signals=[], detail="no match",
                                                    port=None)
            MockChrome.return_value.detect.return_value = self._make_chrome()

            result = _classify_nav_state(self._make_fake_frame())
            self.assertEqual(result["location"], "sea_cinematic")

    def test_omni_unavailable_preserves_legacy_fallback(self):
        """OmniParser unavailable → element count unknown → conservative
        path (don't return pending, allow Moondream to vote)."""
        from brain.perceive import _classify_nav_state

        with patch("vision.omniparser.get_omniparser") as MockOmni, \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.ocr.read_port_name", return_value=None), \
             patch("actions.sail_actions._ocr_frame", return_value=[]), \
             patch("actions.sail_actions._is_loading_screen", return_value=False), \
             patch("actions.sail_actions._confirm_in_town_moondream",
                    return_value=None), \
             patch("actions.sail_actions._confirm_at_sea_moondream",
                    return_value=None):
            MockOmni.return_value.yolo_available.return_value = False
            MockChrome.return_value.detect.return_value = self._make_chrome()

            result = _classify_nav_state(self._make_fake_frame())
            # OmniParser disabled ⇒ omni_elements_count stays None ⇒
            # sparse_elements is False ⇒ no pending ⇒ Moondream path.
            # in_town None and at_sea None → sea_cinematic fallback.
            self.assertEqual(result["location"], "sea_cinematic")


class ConfirmInTownHelperTests(unittest.TestCase):
    """Direct test of the new helper in actions.sail_actions."""

    def test_returns_none_when_vision_unavailable(self):
        from actions.sail_actions import _confirm_in_town_moondream
        fake_frame = MagicMock(width=2400, height=1080)
        fake_frame.copy.return_value = fake_frame
        with patch("vision.local_vision.get_vision") as MockVision:
            MockVision.return_value.check_available.return_value = False
            self.assertIsNone(_confirm_in_town_moondream(fake_frame))

    def test_yes_response_returns_true(self):
        from actions.sail_actions import _confirm_in_town_moondream
        fake_frame = MagicMock(width=2400, height=1080)
        fake_frame.copy.return_value = fake_frame
        with patch("vision.local_vision.get_vision") as MockVision:
            MockVision.return_value.check_available.return_value = True
            MockVision.return_value.ask.return_value = "Yes, the player is in a port town."
            self.assertTrue(_confirm_in_town_moondream(fake_frame))

    def test_no_response_returns_false(self):
        from actions.sail_actions import _confirm_in_town_moondream
        fake_frame = MagicMock(width=2400, height=1080)
        fake_frame.copy.return_value = fake_frame
        with patch("vision.local_vision.get_vision") as MockVision:
            MockVision.return_value.check_available.return_value = True
            MockVision.return_value.ask.return_value = "No, this is a building interior."
            self.assertFalse(_confirm_in_town_moondream(fake_frame))


if __name__ == "__main__":
    unittest.main()
