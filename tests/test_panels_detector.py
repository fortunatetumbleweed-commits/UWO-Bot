"""Slice 4 — panels region detector (overworld + chromed right + center).

Tests cover:
  - Overworld right panel detects tab icons + list items.
  - Chromed right context panel detects title + items.
  - is_overlay=True when X close icon is detected near the title.
  - is_overlay=False on persistent panels (no X).
  - Center panel detection (no X-scan, title optional).
  - List clustering picks the largest column.
  - Convenience accessors: labels(), find().

End-to-end tests against captured frames behind SCENE_MODEL_E2E=1.
"""

from __future__ import annotations

import os
import unittest

from PIL import Image

from vision.omniparser import DetectedElement
from vision.region_detectors.panels import (
    detect_overworld_panel,
    detect_right_panel,
    detect_center_panel,
)
from vision.scene_model import detect_scene


def _el(label="", element_type="text", cx=0, cy=0, w=180, h=40):
    half_w, half_h = w // 2, h // 2
    return DetectedElement(
        label=label, element_type=element_type,
        x1=cx - half_w, y1=cy - half_h,
        x2=cx + half_w, y2=cy + half_h,
        confidence=0.9,
    )


FRAME_W = 2400
FRAME_H = 1080


# ── Synthetic fixtures ────────────────────────────────────────────────────


def _overworld_with_building_list() -> list:
    """Port_overworld with the building list tab active in the right panel."""
    return [
        # Top-left compound (overworld classification)
        _el(element_type="icon", cx=80, cy=120, w=120, h=160),
        _el(element_type="icon", cx=200, cy=60, w=50, h=28),
        _el(label="Amsterdam", element_type="text",
            cx=430, cy=55, w=200, h=40),
        # Tab icon row in right panel (~4 tabs)
        _el(element_type="icon", cx=2080, cy=150, w=50, h=50),
        _el(element_type="icon", cx=2160, cy=150, w=50, h=50),
        _el(element_type="icon", cx=2240, cy=150, w=50, h=50),
        _el(element_type="icon", cx=2320, cy=150, w=50, h=50),
        # Building list below (cx ~2090)
        _el(label="Harbor",         element_type="text",
            cx=2090, cy=460, w=120, h=30),
        _el(label="Market",         element_type="text",
            cx=2090, cy=540, w=120, h=30),
        _el(label="Shipyard",       element_type="text",
            cx=2090, cy=620, w=140, h=30),
        _el(label="Bank",           element_type="text",
            cx=2090, cy=700, w=80,  h=30),
        _el(label="Inn",            element_type="text",
            cx=2090, cy=780, w=70,  h=30),
        # Bottom chrome
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _item_shop_cart_persistent() -> list:
    """Item Shop with persistent Cart right panel.  No X icon."""
    return [
        # Chromed scene chrome
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Gear", element_type="text", cx=130, cy=50, w=100, h=40),
        _el(element_type="icon", cx=240, cy=50, w=38, h=38),    # tutorial ?
        # Cart panel title (no X icon nearby)
        _el(label="Cart", element_type="text",
            cx=2150, cy=140, w=140, h=44),
        # Cart body — empty prompt or items
        _el(label="Please select an item to purchase.", element_type="text",
            cx=2100, cy=500, w=400, h=30),
        # Bottom chrome
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _world_map_city_info_overlay() -> list:
    """World map with City Info overlay panel.  HAS X close icon."""
    return [
        # World map chrome
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),    # back
        _el(label="World Map", element_type="text",
            cx=200, cy=50, w=240, h=40),
        _el(element_type="icon", cx=380, cy=50, w=38, h=38),   # tutorial ?
        # City Info panel: title + X close icon
        _el(label="City Info", element_type="text",
            cx=2080, cy=140, w=180, h=44),
        # X close icon — small icon to the right of the title
        _el(element_type="icon", cx=2330, cy=140, w=44, h=44),
        # Panel body — vertical list of city stats
        _el(label="Population",      element_type="text",
            cx=2090, cy=300, w=180, h=32),
        _el(label="Goods Available", element_type="text",
            cx=2090, cy=400, w=240, h=32),
        _el(label="Tax Rate",        element_type="text",
            cx=2090, cy=500, w=140, h=32),
        # Bottom chrome
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _chromed_center_panel_with_title() -> list:
    """A chromed sub-menu with a center title and list content.

    Used by Inn / Manage Mate for example — the centre has a sub-menu
    bar (Hire / Loyalty / Injury) and a list of mates.  No X icon."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Manage Mate", element_type="text",
            cx=200, cy=50, w=240, h=40),
        # Centre title (a section header like "Hire") + list of mates
        _el(label="Hire", element_type="text",
            cx=900, cy=160, w=100, h=44),
        # Centre list of mate names
        _el(label="Anna Janssens",    element_type="text",
            cx=900, cy=300, w=200, h=30),
        _el(label="Stephan Peron",    element_type="text",
            cx=900, cy=380, w=200, h=30),
        _el(label="Captain Smith",    element_type="text",
            cx=900, cy=460, w=200, h=30),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _empty_overworld_no_panel() -> list:
    """Overworld with NO elements in the right panel zone (rare)."""
    return [
        _el(element_type="icon", cx=80, cy=120, w=120, h=160),
        _el(element_type="icon", cx=200, cy=60, w=50, h=28),
        _el(label="Amsterdam", element_type="text",
            cx=430, cy=55, w=200, h=40),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


# ── Direct detector tests ─────────────────────────────────────────────────


class OverworldPanelTests(unittest.TestCase):

    def test_tabs_and_list_items_detected(self):
        panel = detect_overworld_panel(
            _overworld_with_building_list(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(panel)
        # 4 tab icons
        self.assertEqual(len(panel.tabs), 4)
        # 5 list items (building list)
        labels = panel.labels()
        self.assertEqual(labels, ["Harbor", "Market", "Shipyard", "Bank", "Inn"])
        # Overworld panel is always-on; never overlay.
        self.assertFalse(panel.is_overlay)
        # No title text on overworld panel
        self.assertIsNone(panel.title)

    def test_no_right_panel_zone_returns_none(self):
        panel = detect_overworld_panel(
            _empty_overworld_no_panel(), FRAME_W, FRAME_H,
        )
        self.assertIsNone(panel)


class ChromedRightPanelTests(unittest.TestCase):

    def test_persistent_cart_no_x(self):
        panel = detect_right_panel(
            _item_shop_cart_persistent(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(panel)
        self.assertEqual(panel.title, "Cart")
        # Persistent panel → no X close
        self.assertFalse(panel.is_overlay)

    def test_overlay_city_info_with_x(self):
        panel = detect_right_panel(
            _world_map_city_info_overlay(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(panel)
        self.assertEqual(panel.title, "City Info")
        # X close icon present → overlay panel
        self.assertTrue(panel.is_overlay)
        # Panel body has the list of stats
        labels = panel.labels()
        self.assertIn("Population", labels)
        self.assertIn("Goods Available", labels)
        self.assertIn("Tax Rate", labels)

    def test_overworld_does_not_have_right_panel(self):
        panel = detect_right_panel(
            _overworld_with_building_list(), FRAME_W, FRAME_H,
        )
        # The chromed right_panel detector zone overlaps with the
        # overworld right panel zone, so it may detect items here, but
        # title will be None (no panel title text on overworld).  We
        # accept None OR Panel-with-no-title — slice 4 keeps the
        # detector position-based.
        if panel is not None:
            self.assertIsNone(panel.title)

    def test_panel_find_case_insensitive(self):
        panel = detect_right_panel(
            _world_map_city_info_overlay(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(panel.find("population"))
        self.assertIsNotNone(panel.find("TAX RATE"))
        self.assertIsNone(panel.find("Nonexistent"))


class ChromedCenterPanelTests(unittest.TestCase):

    def test_center_with_title_and_list(self):
        panel = detect_center_panel(
            _chromed_center_panel_with_title(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(panel)
        self.assertEqual(panel.title, "Hire")
        # Center panel never has X
        self.assertFalse(panel.is_overlay)
        labels = panel.labels()
        self.assertIn("Anna Janssens", labels)
        self.assertIn("Stephan Peron", labels)

    def test_center_does_not_pick_x_close_icon(self):
        """Center panel detector must NOT detect X icons — center panels
        cannot be dismissed."""
        # Even if a small icon is to the right of a title, center panel
        # should not flag is_overlay=True.  Verify with explicit fixture.
        elements = [
            _el(element_type="icon", cx=40, cy=50, w=40, h=40),
            _el(label="Sub", element_type="text", cx=140, cy=50, w=80, h=40),
            _el(label="Center Title", element_type="text",
                cx=900, cy=160, w=200, h=44),
            # Small icon to right of title — would be flagged on right
            # panel but NOT on center.
            _el(element_type="icon", cx=1100, cy=160, w=44, h=44),
            _el(label="Item A", element_type="text",
                cx=900, cy=300, w=120, h=30),
            _el(label="Atlantic Ocean", element_type="text",
                cx=1900, cy=1050, w=200, h=22),
        ]
        panel = detect_center_panel(elements, FRAME_W, FRAME_H)
        self.assertIsNotNone(panel)
        self.assertFalse(panel.is_overlay)


# ── Scene-model integration tests ─────────────────────────────────────────


class SceneModelPanelsIntegrationTests(unittest.TestCase):

    def test_overworld_scene_populates_overworld_panel(self):
        sm = detect_scene(FRAME_W, FRAME_H, _overworld_with_building_list())
        self.assertEqual(sm.scene_family, "overworld")
        self.assertIsNotNone(sm.overworld_panel)
        self.assertEqual(len(sm.overworld_panel.items), 5)
        # Right/center panels are not populated on overworld
        self.assertIsNone(sm.right_panel)
        self.assertIsNone(sm.center_panel)

    def test_chromed_scene_populates_right_and_center_panels(self):
        sm = detect_scene(FRAME_W, FRAME_H, _item_shop_cart_persistent())
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.right_panel)
        self.assertEqual(sm.right_panel.title, "Cart")
        # Overworld panel is not populated on chromed
        self.assertIsNone(sm.overworld_panel)

    def test_world_map_populates_right_panel_for_city_info(self):
        sm = detect_scene(FRAME_W, FRAME_H,
                          _world_map_city_info_overlay())
        # World map family — right panel (City Info) is populated; left
        # menu / action buttons not populated since family != chromed.
        self.assertEqual(sm.scene_family, "world_map")
        self.assertIsNotNone(sm.right_panel)
        self.assertEqual(sm.right_panel.title, "City Info")
        self.assertTrue(sm.right_panel.is_overlay)


# ── End-to-end against captured frames ────────────────────────────────────


@unittest.skipUnless(
    os.environ.get("SCENE_MODEL_E2E") == "1",
    "End-to-end tests require SCENE_MODEL_E2E=1 (loads OmniParser)",
)
class EndToEndCapturedFrameTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from vision.omniparser import get_omniparser
        cls.parser = get_omniparser()
        cls.parser.yolo_available()

    def _scene(self, path: str):
        frame = Image.open(path)
        elements = self.parser.parse_fast(frame)
        return detect_scene(frame.width, frame.height, elements)

    def test_inn_frame_right_panel_detected(self):
        sm = self._scene(
            "data/sessions/explore_amsterdanted_2026-05-17_01-27-48/"
            "frames/0000__inn_main__7789847232.png"
        )
        self.assertEqual(sm.scene_family, "chromed")
        # Right panel on Inn frame contains Hire/Party sections.  We
        # don't assert on the exact title (OCR may vary); just that
        # SOMETHING was detected.
        self.assertIsNotNone(sm.right_panel)
        # Inn right panel is persistent — no X.
        self.assertFalse(sm.right_panel.is_overlay)


if __name__ == "__main__":
    unittest.main()
