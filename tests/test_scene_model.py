"""Slice 1 — SceneModel substrate + top_left/bottom_chrome detectors.

These tests use a mix of:
  - Synthetic DetectedElement fixtures (fast, deterministic, isolated)
  - End-to-end runs against captured frames (slower; gated behind
    SCENE_MODEL_E2E=1 env var so CI doesn't have to load OmniParser)

The synthetic fixtures pin the detector logic.  The e2e tests, when
enabled, validate the pipeline against real captured screen content.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Optional

from PIL import Image

from vision.omniparser import DetectedElement
from vision.scene_model import (
    BottomChromeRegion,
    SceneModel,
    TopLeftRegion,
    detect_scene,
)


# ── Synthetic element factory ─────────────────────────────────────────────


def _el(
    label: str = "",
    element_type: str = "icon",
    cx: int = 0, cy: int = 0,
    w: int = 40, h: int = 40,
    confidence: float = 0.9,
) -> DetectedElement:
    """Build a DetectedElement centred at (cx, cy) with given size."""
    half_w, half_h = w // 2, h // 2
    return DetectedElement(
        label=label,
        element_type=element_type,
        x1=cx - half_w, y1=cy - half_h,
        x2=cx + half_w, y2=cy + half_h,
        confidence=confidence,
    )


FRAME_W = 2400
FRAME_H = 1080


# ── Synthetic fixtures ────────────────────────────────────────────────────


def _port_overworld_elements() -> list:
    """Approximate port_overworld top-left layout based on captured
    frame 0001 (clean Amsterdam): lighthouse big icon + Dutch flag + title."""
    return [
        # Lighthouse big icon at top-left
        _el(element_type="icon", cx=80, cy=120, w=120, h=160),
        # Dutch national flag — small icon to the right of the lighthouse, at title row
        _el(element_type="icon", cx=200, cy=60, w=50, h=28),
        # Title text "Amsterdam"
        _el(label="Amsterdam", element_type="text",
            cx=430, cy=55, w=200, h=40),
        # Bottom chrome row — wifi/uid/server
        _el(label="Wi-Fi", element_type="text", cx=80, cy=1050, w=60, h=22),
        _el(label="4.0501.041.281 Atlantic Ocean",
            element_type="text", cx=1900, cy=1050, w=400, h=22),
    ]


def _port_overworld_bubble_fused_elements() -> list:
    """port_overworld with bubble fragment fused into title — like
    captured frame 0000 ('Amsterdamads!')."""
    return [
        _el(element_type="icon", cx=190, cy=95, w=130, h=150),
        _el(element_type="icon", cx=255, cy=60, w=50, h=28),    # flag
        _el(label="Amsterdamads!", element_type="text",
            cx=460, cy=54, w=254, h=40),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _sea_elements() -> list:
    """Approximate sea overworld: ship icon + waters name (no flag)."""
    return [
        # Ship icon / compound region (sea returns one wide button)
        _el(label="Lauless Watters", element_type="button",
            cx=420, cy=110, w=580, h=200),
        _el(label="411 Days of Sailing Left", element_type="text",
            cx=310, cy=195, w=320, h=44),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _building_inn_elements() -> list:
    """Inn building interior: back-arrow + title 'Inn' + '?' tutorial icon."""
    return [
        # Back arrow at very top-left
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        # Title 'Inn'
        _el(label="Inn", element_type="text", cx=120, cy=50, w=100, h=40),
        # '?' tutorial icon just to the right of the title
        _el(element_type="icon", cx=240, cy=50, w=38, h=38),
        # Bottom chrome
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _sub_menu_purchase_elements() -> list:
    """Market's Purchase sub-menu: back-arrow + title 'Purchase' + '?'"""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Purchase", element_type="text",
            cx=140, cy=50, w=180, h=40),
        _el(element_type="icon", cx=300, cy=50, w=40, h=40),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _world_map_elements() -> list:
    """World map screen: back-arrow + 'World Map' title."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="World Map", element_type="text",
            cx=200, cy=50, w=240, h=40),
        _el(element_type="icon", cx=380, cy=50, w=40, h=40),  # tutorial ?
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _unknown_elements() -> list:
    """Nothing recognisable in the top-left — e.g. a loading screen."""
    return [
        _el(label="Loading…", element_type="text",
            cx=1200, cy=540, w=200, h=40),
    ]


# ── TopLeftRegion detector tests ──────────────────────────────────────────


class TopLeftDetectorTests(unittest.TestCase):
    """Synthetic-element tests for the top_left detector."""

    def test_port_overworld_clean_lighthouse(self):
        from vision.region_detectors.top_left import detect_top_left
        tl = detect_top_left(_port_overworld_elements(), FRAME_W, FRAME_H)
        self.assertEqual(tl.family, "overworld")
        self.assertEqual(tl.big_icon, "lighthouse")
        self.assertTrue(tl.flag_present)
        self.assertIsNotNone(tl.title)
        self.assertEqual(tl.title.text, "Amsterdam")
        self.assertEqual(tl.title.raw_ocr, "Amsterdam")
        self.assertFalse(tl.back_arrow)

    def test_port_overworld_bubble_fusion_corrected_via_text_correction(self):
        """The fuzzy-match correction kicks in within the title builder."""
        from vision.region_detectors.top_left import detect_top_left
        tl = detect_top_left(
            _port_overworld_bubble_fused_elements(), FRAME_W, FRAME_H,
        )
        self.assertEqual(tl.family, "overworld")
        self.assertEqual(tl.big_icon, "lighthouse")
        self.assertIsNotNone(tl.title)
        # Raw is the corrupted form; text is the corrected canonical
        self.assertEqual(tl.title.raw_ocr, "Amsterdamads!")
        self.assertEqual(tl.title.text, "Amsterdam")

    def test_sea_ship_no_flag(self):
        from vision.region_detectors.top_left import detect_top_left
        tl = detect_top_left(_sea_elements(), FRAME_W, FRAME_H)
        self.assertEqual(tl.family, "overworld")
        self.assertEqual(tl.big_icon, "ship")
        self.assertFalse(tl.flag_present)
        # The compound region is detected as a button labelled with the
        # waters name (OCR-corrupted) — gets fuzzy-corrected to canonical
        # 'Lawless Waters'.
        self.assertIsNotNone(tl.title)
        self.assertEqual(tl.title.text, "Lawless Waters")

    def test_building_inn_back_arrow_plus_title(self):
        from vision.region_detectors.top_left import detect_top_left
        tl = detect_top_left(_building_inn_elements(), FRAME_W, FRAME_H)
        self.assertEqual(tl.family, "chromed")
        self.assertTrue(tl.back_arrow)
        self.assertTrue(tl.tutorial_q)
        self.assertIsNone(tl.big_icon)
        self.assertIsNotNone(tl.title)
        self.assertEqual(tl.title.text.lower(), "inn")

    def test_sub_menu_purchase_back_arrow(self):
        from vision.region_detectors.top_left import detect_top_left
        tl = detect_top_left(
            _sub_menu_purchase_elements(), FRAME_W, FRAME_H,
        )
        self.assertEqual(tl.family, "chromed")
        self.assertTrue(tl.back_arrow)
        self.assertTrue(tl.tutorial_q)
        self.assertEqual(tl.title.text.lower(), "purchase")

    def test_world_map_recognised_via_title(self):
        from vision.region_detectors.top_left import detect_top_left
        tl = detect_top_left(_world_map_elements(), FRAME_W, FRAME_H)
        self.assertEqual(tl.family, "world_map")
        self.assertTrue(tl.back_arrow)
        self.assertEqual(tl.title.text.lower(), "world map")

    def test_unknown_returns_unknown_family(self):
        from vision.region_detectors.top_left import detect_top_left
        tl = detect_top_left(_unknown_elements(), FRAME_W, FRAME_H)
        self.assertEqual(tl.family, "unknown")
        self.assertIsNone(tl.big_icon)
        self.assertFalse(tl.back_arrow)


# ── BottomChromeRegion detector tests ─────────────────────────────────────


class BottomChromeDetectorTests(unittest.TestCase):

    def test_detects_server_name(self):
        from vision.region_detectors.bottom_chrome import detect_bottom_chrome
        bc = detect_bottom_chrome(
            _port_overworld_elements(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(bc)
        self.assertTrue(bc.present)
        self.assertEqual(bc.server_name, "Atlantic Ocean")

    def test_returns_none_when_no_bottom_strip(self):
        from vision.region_detectors.bottom_chrome import detect_bottom_chrome
        # Loading-screen style frame: no elements at the bottom.
        bc = detect_bottom_chrome(
            _unknown_elements(), FRAME_W, FRAME_H,
        )
        self.assertIsNone(bc)


# ── Scene classifier (SceneModel composition) tests ───────────────────────


class SceneClassifierTests(unittest.TestCase):

    def test_port_overworld_classified_at_high_confidence(self):
        sm = detect_scene(FRAME_W, FRAME_H, _port_overworld_elements())
        self.assertEqual(sm.scene_family, "overworld")
        self.assertEqual(sm.scene_kind, "port_overworld")
        self.assertEqual(sm.confidence, "high")
        self.assertIn("top_left_lighthouse", sm.signals_matched)
        self.assertIn("top_left_flag", sm.signals_matched)
        self.assertEqual(sm.signals_disagreed, [])
        self.assertIsNotNone(sm.top_left)
        self.assertIsNotNone(sm.bottom_chrome)

    def test_sea_classified_at_high_confidence(self):
        sm = detect_scene(FRAME_W, FRAME_H, _sea_elements())
        self.assertEqual(sm.scene_family, "overworld")
        self.assertEqual(sm.scene_kind, "sea")
        self.assertEqual(sm.confidence, "high")
        self.assertIn("top_left_ship", sm.signals_matched)

    def test_building_inn_classified(self):
        sm = detect_scene(FRAME_W, FRAME_H, _building_inn_elements())
        self.assertEqual(sm.scene_family, "chromed")
        self.assertEqual(sm.scene_kind, "building:inn")
        self.assertEqual(sm.confidence, "high")
        self.assertIn("top_left_back_arrow", sm.signals_matched)
        self.assertIn("top_left_tutorial_q", sm.signals_matched)

    def test_sub_menu_purchase_classified(self):
        sm = detect_scene(FRAME_W, FRAME_H, _sub_menu_purchase_elements())
        self.assertEqual(sm.scene_family, "chromed")
        self.assertEqual(sm.scene_kind, "sub_menu:purchase")
        self.assertEqual(sm.confidence, "high")

    def test_world_map_classified(self):
        sm = detect_scene(FRAME_W, FRAME_H, _world_map_elements())
        self.assertEqual(sm.scene_family, "world_map")
        self.assertEqual(sm.scene_kind, "world_map")
        self.assertEqual(sm.confidence, "high")

    def test_unknown_low_confidence(self):
        sm = detect_scene(FRAME_W, FRAME_H, _unknown_elements())
        self.assertEqual(sm.scene_family, "unknown")
        self.assertEqual(sm.scene_kind, "unknown")
        self.assertEqual(sm.confidence, "low")
        self.assertEqual(sm.signals_matched, [])

    def test_summary_format(self):
        sm = detect_scene(FRAME_W, FRAME_H, _port_overworld_elements())
        s = sm.summary()
        self.assertIn("family=overworld", s)
        self.assertIn("kind=port_overworld", s)
        self.assertIn("conf=high", s)


# ── End-to-end tests against captured frames ──────────────────────────────


# Gated behind env var because they need OmniParser loaded (slow).  Run
# with: SCENE_MODEL_E2E=1 python -m pytest tests/test_scene_model.py
@unittest.skipUnless(
    os.environ.get("SCENE_MODEL_E2E") == "1",
    "End-to-end tests require SCENE_MODEL_E2E=1 (loads OmniParser)",
)
class EndToEndCapturedFrameTests(unittest.TestCase):
    """Run the actual detector pipeline on captured screen content."""

    @classmethod
    def setUpClass(cls):
        from vision.omniparser import get_omniparser
        cls.parser = get_omniparser()
        cls.parser.yolo_available()

    def _run(self, path: str):
        frame = Image.open(path)
        elements = self.parser.parse_fast(frame)
        return detect_scene(frame.width, frame.height, elements)

    def test_clean_port_overworld_frame(self):
        sm = self._run("data/sessions/2026-05-16_22-36-41/frames/0001_2240492999.png")
        self.assertEqual(sm.scene_family, "overworld")
        self.assertEqual(sm.scene_kind, "port_overworld")
        self.assertIsNotNone(sm.top_left)
        self.assertEqual(sm.top_left.big_icon, "lighthouse")
        self.assertIsNotNone(sm.top_left.title)
        # Should canonicalise to 'Amsterdam' (clean read already matches)
        self.assertEqual(sm.top_left.title.text, "Amsterdam")

    def test_bubble_occluded_port_frame_corrects_title(self):
        sm = self._run("data/sessions/2026-05-16_22-36-41/frames/0000_2236494579.png")
        self.assertEqual(sm.scene_family, "overworld")
        self.assertEqual(sm.scene_kind, "port_overworld")
        self.assertIsNotNone(sm.top_left.title)
        # Raw OCR was 'Amsterdamads!' — fuzzy correction recovers 'Amsterdam'
        self.assertEqual(sm.top_left.title.text, "Amsterdam")

    def test_sea_frame(self):
        sm = self._run("data/sessions/2026-04-14_21-49-12/frames/0009_2150014834.png")
        self.assertEqual(sm.scene_family, "overworld")
        self.assertEqual(sm.scene_kind, "sea")
        self.assertIsNotNone(sm.top_left)
        self.assertEqual(sm.top_left.big_icon, "ship")


class SceneModelConsumerHelpersTests(unittest.TestCase):
    """Slice 5 helpers — consumers ask SceneModel structured questions
    instead of reading the family/scene_kind strings themselves."""

    def _scene_at_port(self, port_name, confidence="high"):
        from vision.scene_model import (
            SceneModel, TitleField, TopLeftRegion,
        )
        return SceneModel(
            scene_family="overworld",
            scene_kind="port_overworld",
            confidence=confidence,
            top_left=TopLeftRegion(
                family="overworld",
                big_icon="lighthouse",
                title=TitleField(
                    text=port_name, raw_ocr=port_name, bbox=(0, 0, 100, 40),
                ),
            ),
        )

    def test_is_at_port_overworld_no_name(self):
        sm = self._scene_at_port("Amsterdam")
        self.assertTrue(sm.is_at_port_overworld())

    def test_is_at_port_overworld_matching_name(self):
        sm = self._scene_at_port("Amsterdam")
        self.assertTrue(sm.is_at_port_overworld("Amsterdam"))
        self.assertTrue(sm.is_at_port_overworld("amsterdam"))
        self.assertTrue(sm.is_at_port_overworld("AMSTERDAM"))

    def test_is_at_port_overworld_mismatched_name(self):
        sm = self._scene_at_port("Amsterdam")
        self.assertFalse(sm.is_at_port_overworld("London"))

    def test_is_at_port_overworld_rejects_low_confidence(self):
        sm = self._scene_at_port("Amsterdam", confidence="low")
        self.assertFalse(sm.is_at_port_overworld())
        self.assertFalse(sm.is_at_port_overworld("Amsterdam"))

    def test_is_at_port_overworld_rejects_non_overworld(self):
        from vision.scene_model import SceneModel
        sm = SceneModel(
            scene_family="chromed", scene_kind="building:inn", confidence="high",
        )
        self.assertFalse(sm.is_at_port_overworld())
        self.assertFalse(sm.is_at_port_overworld("Amsterdam"))

    def test_is_at_sea(self):
        from vision.scene_model import SceneModel
        sm = SceneModel(scene_family="overworld", scene_kind="sea",
                        confidence="high")
        self.assertTrue(sm.is_at_sea())

    def test_is_inside_building(self):
        from vision.scene_model import SceneModel
        sm = SceneModel(scene_family="chromed", scene_kind="building:inn",
                        confidence="high")
        self.assertTrue(sm.is_inside_building())
        self.assertFalse(sm.is_inside_sub_menu())
        self.assertFalse(sm.is_at_port_overworld())

    def test_is_inside_sub_menu(self):
        from vision.scene_model import SceneModel
        sm = SceneModel(scene_family="chromed",
                        scene_kind="sub_menu:purchase", confidence="high")
        self.assertTrue(sm.is_inside_sub_menu())
        self.assertFalse(sm.is_inside_building())

    def test_is_at_world_map(self):
        from vision.scene_model import SceneModel
        sm = SceneModel(scene_family="world_map", scene_kind="world_map",
                        confidence="high")
        self.assertTrue(sm.is_at_world_map())


class GetSceneModelTests(unittest.TestCase):
    """Slice 5 — get_scene_model() captures (or accepts) a frame, runs
    OmniParser, and returns a SceneModel.  Convenience for consumers
    that don't already have an element list."""

    def test_get_scene_model_with_provided_frame(self):
        from unittest.mock import patch
        from PIL import Image
        from vision.scene_model import get_scene_model

        frame = Image.new("RGB", (2400, 1080), color=(0, 0, 0))
        with patch("vision.omniparser.parse_fast_cached", return_value=[]):
            sm = get_scene_model(frame=frame)
        # Empty elements → unknown scene
        self.assertEqual(sm.scene_family, "unknown")
        self.assertEqual(sm.confidence, "low")

    def test_get_scene_model_captures_when_no_frame(self):
        from unittest.mock import patch
        from PIL import Image
        from vision.scene_model import get_scene_model

        frame = Image.new("RGB", (2400, 1080), color=(0, 0, 0))
        with patch("capture.adb_capture.capture_screen",
                   return_value=frame), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]):
            sm = get_scene_model()   # no frame passed
        self.assertEqual(sm.scene_family, "unknown")


if __name__ == "__main__":
    unittest.main()
