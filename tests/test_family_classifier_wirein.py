"""Tests for the Phase 4a family-classifier wire-in inside
_classify_nav_state.

Covers:
  • High-confidence sea family → short-circuit, no OmniParser call.
  • High-confidence world_map family → short-circuit.
  • port_overworld family → fall through (still need port name OCR).
  • chromed family → fall through (still need fingerprint).
  • Low-confidence family → fall through.
  • Tie-breaker: legacy returns 'building' but family says port_overworld
    with high confidence → override to port_overworld.
"""
import unittest
from unittest.mock import MagicMock, patch

from vision.family_classifier import FamilyVerdict


def _frame():
    f = MagicMock()
    f.width = 2400
    f.height = 1080
    f.crop.return_value = f
    return f


class FamilyShortCircuitTests(unittest.TestCase):

    def _classify(self, family, confidence,
                  omni_result=None, omni_available=True):
        """Run _classify_nav_state with the family classifier mocked to
        return (family, confidence), and the OmniParser-primary
        classifier mocked to return omni_result (or 'unknown' if None)."""
        from brain.perceive import _classify_nav_state

        verdict = FamilyVerdict(family=family, confidence=confidence)

        # Build a minimal omniparser ScreenClassification mock.
        sc = MagicMock()
        if omni_result is None:
            sc.state = "unknown"
            sc.detail = "no match"
            sc.confidence = "low"
            sc.port = None
            sc.signals = []
        else:
            sc.state = omni_result["state"]
            sc.detail = omni_result.get("detail", "")
            sc.confidence = omni_result.get("confidence", "high")
            sc.port = omni_result.get("port")
            sc.signals = omni_result.get("signals", [])

        omni_yolo = MagicMock()
        omni_yolo.yolo_available.return_value = omni_available

        # Everything below the OmniParser branch we patch to keep them
        # idle — we don't exercise the legacy chrome / OCR path here.
        chrome = MagicMock()
        chrome.has_home = False
        chrome.has_back_arrow = False
        chrome.has_hamburger = False
        chrome.has_right_panel = False

        with patch("vision.family_classifier.classify_family",
                   return_value=verdict), \
             patch("vision.omniparser.get_omniparser",
                   return_value=omni_yolo), \
             patch("vision.omniparser.parse_fast_cached",
                   return_value=[]), \
             patch("vision.screen_classifier.classify_screen",
                   return_value=sc), \
             patch("vision.chrome_detector.get_chrome_detector",
                   return_value=MagicMock(detect=MagicMock(return_value=chrome))), \
             patch("vision.world_map_parser.load_port_catalogue",
                   return_value={}), \
             patch("vision.ocr.read_port_name", return_value=""), \
             patch("actions.sail_actions._ocr_frame", return_value=[]):
            return _classify_nav_state(_frame())

    def test_sea_high_confidence_short_circuits(self):
        result = self._classify(family="sea", confidence=0.95)
        self.assertEqual(result["location"], "sea")
        self.assertIn("family-classifier", result["detail"])

    def test_world_map_high_confidence_short_circuits(self):
        result = self._classify(family="world_map", confidence=0.99)
        self.assertEqual(result["location"], "world_map")

    def test_low_confidence_falls_through(self):
        """conf < 0.7 → defer to OmniParser cascade."""
        # OmniParser returns building → expect building (fallthrough used)
        result = self._classify(
            family="sea", confidence=0.4,
            omni_result={"state": "building", "detail": "building: harbor"},
        )
        self.assertEqual(result["location"], "building")

    def test_chromed_family_falls_through(self):
        """chromed needs Stage 2 fingerprint to determine building vs
        sub_menu vs main_menu — never short-circuited."""
        result = self._classify(
            family="chromed", confidence=0.95,
            omni_result={"state": "sub_menu", "detail": "sub_menu: recruit crew"},
        )
        self.assertEqual(result["location"], "sub_menu")

    def test_port_overworld_short_circuits_with_ocr_port_name(self):
        """High-conf port_overworld now also short-circuits — after
        a cheap OCR call to populate the port name.  The full cascade
        is skipped.
        """
        from brain.perceive import _classify_nav_state
        verdict = FamilyVerdict(family="port_overworld", confidence=0.92)
        chrome = MagicMock()
        chrome.has_home = False; chrome.has_back_arrow = False
        chrome.has_hamburger = False; chrome.has_right_panel = False

        # Stub OCR to return a raw port name, and the corrector to
        # canonicalise it.
        with patch("vision.family_classifier.classify_family",
                   return_value=verdict), \
             patch("vision.ocr.read_port_name", return_value="Lisboa"), \
             patch("vision.text_correction.correct_port_name",
                   return_value=("Lisboa", 0.97)), \
             patch("vision.chrome_detector.get_chrome_detector",
                   return_value=MagicMock(detect=MagicMock(return_value=chrome))), \
             patch("vision.omniparser.get_omniparser") as mock_omni, \
             patch("vision.screen_classifier.classify_screen") as mock_cls:
            result = _classify_nav_state(_frame())

        self.assertEqual(result["location"], "port_overworld")
        self.assertEqual(result["port"], "Lisboa")
        self.assertIn("family-classifier", result["detail"])
        # Critical: short-circuit means OmniParser was never asked.
        mock_omni.assert_not_called()
        mock_cls.assert_not_called()

    def test_transient_family_falls_through(self):
        """transient family needs DialogModel/overlay detectors that
        live downstream — short-circuit would lose context."""
        result = self._classify(
            family="transient", confidence=0.95,
            omni_result={"state": "sub_menu", "detail": "fallback"},
        )
        self.assertEqual(result["location"], "sub_menu")


class PalmaShipyardRegressionTests(unittest.TestCase):
    """The 2026-05-22 Palma shipyard failure pattern: bot was actually on
    port_overworld, but OmniParser tagged a nameplate near the top-left
    as 'shipyard' and the fingerprint matched 'building: shipyard',
    sending sail_to into a 22-min EXIT_BUILDING loop.

    With the family-classifier short-circuit, a high-confidence
    port_overworld verdict returns BEFORE OmniParser even runs — the
    misclassification can't happen because we never invoke the matcher
    that produced it.
    """

    def test_high_conf_port_overworld_skips_omniparser_entirely(self):
        from brain.perceive import _classify_nav_state
        verdict = FamilyVerdict(family="port_overworld", confidence=0.92)
        with patch("vision.family_classifier.classify_family",
                   return_value=verdict), \
             patch("vision.ocr.read_port_name", return_value="Las Palmas"), \
             patch("vision.text_correction.correct_port_name",
                   return_value=("Las Palmas", 1.0)), \
             patch("vision.omniparser.get_omniparser") as mock_omni, \
             patch("vision.screen_classifier.classify_screen") as mock_cls:
            result = _classify_nav_state(_frame())
        self.assertEqual(result["location"], "port_overworld")
        self.assertEqual(result["port"], "Las Palmas")
        mock_omni.assert_not_called()
        mock_cls.assert_not_called()

    def test_low_conf_port_overworld_lets_omniparser_decide(self):
        """At family conf < 0.7 we don't trust the family verdict —
        defer to the (possibly-misclassifying) fingerprint cascade.
        This is the same risk as today's pre-Phase-4a behaviour.  Phase
        4a v2 will mitigate by retraining on more village + edge-case
        frames to push confidence up."""
        from brain.perceive import _classify_nav_state
        verdict = FamilyVerdict(family="port_overworld", confidence=0.55)
        sc = MagicMock()
        sc.state = "building"
        sc.detail = "building: shipyard"
        sc.confidence = "high"
        sc.port = "Las Palmas"
        sc.signals = []
        chrome = MagicMock()
        chrome.has_home = False; chrome.has_back_arrow = False
        chrome.has_hamburger = False; chrome.has_right_panel = False
        omni_yolo = MagicMock()
        omni_yolo.yolo_available.return_value = True
        with patch("vision.family_classifier.classify_family",
                   return_value=verdict), \
             patch("vision.omniparser.get_omniparser", return_value=omni_yolo), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.screen_classifier.classify_screen", return_value=sc), \
             patch("vision.chrome_detector.get_chrome_detector",
                   return_value=MagicMock(detect=MagicMock(return_value=chrome))), \
             patch("vision.world_map_parser.load_port_catalogue", return_value={}), \
             patch("vision.ocr.read_port_name", return_value=""), \
             patch("actions.sail_actions._ocr_frame", return_value=[]):
            result = _classify_nav_state(_frame())
        # Low-confidence family doesn't short-circuit; legacy result wins.
        self.assertEqual(result["location"], "building")


if __name__ == "__main__":
    unittest.main()
