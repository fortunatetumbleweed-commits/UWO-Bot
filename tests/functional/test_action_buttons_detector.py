"""Slice 3 — action_buttons region detector.

Tests cover:
  - Single-button cases (Purchase, Sell, Recruit, Donate, …).
  - Multi-button rows (Empty + Purchase; Add All + filter + Sell).
  - is_positive = rightmost button.
  - Text-typed elements in the same row (filter chips, counts, values)
    NOT picked as action buttons — only button-typed elements are.
  - Bottom OS chrome (server name, wifi) at cy > 1020 NOT picked.
  - Numeric-only labels NOT picked.
  - Convenience accessors: labels(), positive(), find().

End-to-end tests against captured frames behind SCENE_MODEL_E2E=1.
"""

from __future__ import annotations

import os
import unittest

from PIL import Image

from vision.omniparser import DetectedElement
from vision.region_detectors.action_buttons import detect_action_buttons
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


def _item_shop_tool_elements() -> list:
    """Item Shop / Tool sub-menu: Empty + Purchase action buttons.

    Real frame coords (from Claude analysis of captured sub-menu):
      'Empty'    @ (1916, 998)
      'Purchase' @ (2177, 998)
    """
    return [
        # Back arrow + title
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Tool", element_type="text", cx=140, cy=50, w=100, h=40),
        # Left menu (background to make sure we don't pick these)
        _el(label="Gear",         cx=200, cy=158, w=120, h=40),
        _el(label="Tool",         cx=200, cy=263, w=100, h=40),
        # Action buttons at bottom-right
        _el(label="Empty",    element_type="button", cx=1916, cy=998, w=180, h=50),
        _el(label="Purchase", element_type="button", cx=2177, cy=998, w=200, h=50),
        # Non-button text in the same row (filter chips, counts) — must
        # not be picked as action buttons.
        _el(label="Tool 143/149", element_type="text",
            cx=1711, cy=1013, w=200, h=24),
        _el(label="Load 1,000",   element_type="text",
            cx=579, cy=1016, w=160, h=24),
        # Bottom OS chrome
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _item_shop_sell_elements() -> list:
    """Item Shop / Sell sub-menu: just Sell button at bottom-right."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Sell", element_type="text", cx=130, cy=50, w=100, h=40),
        # Filter chips and counts — TEXT type, must not be picked
        _el(label="Add All",      element_type="text",
            cx=1480, cy=1011, w=180, h=30),
        _el(label="Common 22/87", element_type="text",
            cx=1697, cy=1011, w=200, h=30),
        # Action button
        _el(label="Sell", element_type="button", cx=2239, cy=1001, w=200, h=50),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _inn_recruit_crew_elements() -> list:
    """Inn / Recruit Crew sub-menu: Recruit button at bottom-right."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Recruit Crew", element_type="text",
            cx=200, cy=50, w=240, h=40),
        # Min / Max / Max Waiting buttons
        _el(label="Min",  element_type="button", cx=1925, cy=702, w=100, h=50),
        _el(label="Max Waiting", element_type="button",
            cx=2095, cy=703, w=180, h=50),
        _el(label="Max",  element_type="button", cx=2261, cy=702, w=80,  h=50),
        # Emergency Recruit / Normal Recruit
        _el(label="Emergency Recruit", element_type="button",
            cx=2092, cy=767, w=300, h=50),
        _el(label="Normal Recruit", element_type="button",
            cx=2091, cy=829, w=280, h=50),
        # The final Recruit action button — at the bottom-right
        _el(label="Recruit", element_type="button",
            cx=2239, cy=898, w=200, h=50),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _cathedral_donate_elements() -> list:
    """Cathedral / Donate sub-menu: Donate button + three oblation
    options shown as cards.  Only Donate is the commit button."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Donate", element_type="text",
            cx=140, cy=50, w=100, h=40),
        _el(label="Normal Oblation",  element_type="text",
            cx=580,  cy=500, w=300, h=30),
        _el(label="Costly Oblation",  element_type="text",
            cx=1180, cy=500, w=300, h=30),
        _el(label="Special Oblation", element_type="text",
            cx=1780, cy=500, w=300, h=30),
        # Donate confirm button at bottom-right
        _el(label="Donate", element_type="button",
            cx=2239, cy=998, w=200, h=50),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _no_action_buttons_elements() -> list:
    """A chromed scene with no action button (e.g. read-only info)."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Info", element_type="text", cx=140, cy=50, w=100, h=40),
        # Only text elements — no buttons
        _el(label="Some info text", element_type="text",
            cx=1200, cy=500, w=400, h=40),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _overworld_elements() -> list:
    """Overworld — no action buttons region."""
    return [
        _el(element_type="icon", cx=80, cy=120, w=120, h=160),
        _el(element_type="icon", cx=200, cy=60, w=50, h=28),
        _el(label="Amsterdam", element_type="text",
            cx=430, cy=55, w=200, h=40),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


# ── Direct detector tests ─────────────────────────────────────────────────


class ActionButtonsDetectorTests(unittest.TestCase):

    def test_item_shop_tool_empty_plus_purchase(self):
        region = detect_action_buttons(
            _item_shop_tool_elements(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(region)
        self.assertEqual(region.labels(), ["Empty", "Purchase"])
        # 'Purchase' is rightmost → is_positive
        positive = region.positive()
        self.assertIsNotNone(positive)
        self.assertEqual(positive["label"], "Purchase")
        # 'Empty' is left of Purchase → not positive
        empty = region.find("Empty")
        self.assertFalse(empty["is_positive"])

    def test_text_filter_chips_not_picked_as_action_buttons(self):
        """'Tool 143/149' and 'Load 1,000' are text-typed in the same
        bottom row but must NOT appear as action buttons."""
        region = detect_action_buttons(
            _item_shop_tool_elements(), FRAME_W, FRAME_H,
        )
        self.assertNotIn("Tool 143/149", region.labels())
        self.assertNotIn("Load 1,000",   region.labels())

    def test_item_shop_sell_only_sell_button(self):
        region = detect_action_buttons(
            _item_shop_sell_elements(), FRAME_W, FRAME_H,
        )
        self.assertEqual(region.labels(), ["Sell"])
        self.assertTrue(region.positive()["label"] == "Sell")
        # 'Add All' / 'Common 22/87' are TEXT-typed — not picked.
        self.assertNotIn("Add All", region.labels())
        self.assertNotIn("Common 22/87", region.labels())

    def test_inn_recruit_crew_recruit_is_positive(self):
        region = detect_action_buttons(
            _inn_recruit_crew_elements(), FRAME_W, FRAME_H,
        )
        # Many buttons in the row; the rightmost one is 'Recruit'.
        labels = region.labels()
        self.assertIn("Recruit", labels)
        # Rightmost (highest cx) is Recruit at cx=2239.
        positive = region.positive()
        self.assertEqual(positive["label"], "Recruit")

    def test_cathedral_donate_button_picked(self):
        region = detect_action_buttons(
            _cathedral_donate_elements(), FRAME_W, FRAME_H,
        )
        self.assertEqual(region.labels(), ["Donate"])
        # Oblation cards are TEXT, not picked.
        self.assertNotIn("Normal Oblation",  region.labels())
        self.assertNotIn("Costly Oblation",  region.labels())
        self.assertNotIn("Special Oblation", region.labels())

    def test_no_action_buttons_returns_none(self):
        region = detect_action_buttons(
            _no_action_buttons_elements(), FRAME_W, FRAME_H,
        )
        self.assertIsNone(region)

    def test_overworld_returns_none(self):
        region = detect_action_buttons(
            _overworld_elements(), FRAME_W, FRAME_H,
        )
        self.assertIsNone(region)

    def test_find_case_insensitive(self):
        region = detect_action_buttons(
            _item_shop_tool_elements(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(region.find("purchase"))
        self.assertIsNotNone(region.find("PURCHASE"))
        self.assertIsNotNone(region.find("Empty"))
        self.assertIsNone(region.find("Cancel"))

    def test_buttons_sorted_left_to_right(self):
        region = detect_action_buttons(
            _inn_recruit_crew_elements(), FRAME_W, FRAME_H,
        )
        # Sort order must be ascending cx
        cxs = [b["cx"] for b in region.buttons]
        self.assertEqual(cxs, sorted(cxs))


# ── Scene-model integration tests ─────────────────────────────────────────


class SceneModelActionButtonsIntegrationTests(unittest.TestCase):

    def test_chromed_scene_has_action_buttons_populated(self):
        sm = detect_scene(FRAME_W, FRAME_H, _item_shop_tool_elements())
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.action_buttons)
        self.assertEqual(sm.action_buttons.labels(), ["Empty", "Purchase"])
        self.assertTrue(
            any("action_buttons" in s for s in sm.signals_matched),
            f"action_buttons signal missing: {sm.signals_matched}",
        )

    def test_overworld_scene_has_no_action_buttons(self):
        sm = detect_scene(FRAME_W, FRAME_H, _overworld_elements())
        self.assertEqual(sm.scene_family, "overworld")
        self.assertIsNone(sm.action_buttons)


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

    def test_item_shop_tool_has_purchase_button(self):
        sm = self._scene(
            "data/sessions/explore_amsterdanted_2026-05-17_01-27-48/"
            "item_shop/frames/0002__item_shop_tool__7789856762.png"
        )
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.action_buttons)
        labels = [l.lower() for l in sm.action_buttons.labels()]
        self.assertTrue(
            any("purchase" in l for l in labels),
            f"expected 'purchase' in action buttons: {labels}",
        )
        # Positive button should be Purchase (rightmost)
        positive = sm.action_buttons.positive()
        self.assertIsNotNone(positive)
        self.assertIn("purchase", positive["label"].lower())

    def test_item_shop_sell_has_sell_button(self):
        sm = self._scene(
            "data/sessions/explore_amsterdanted_2026-05-17_01-27-48/"
            "item_shop/frames/0004__item_shop_sell__7789859441.png"
        )
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.action_buttons)
        labels = [l.lower() for l in sm.action_buttons.labels()]
        self.assertTrue(
            any("sell" in l for l in labels),
            f"expected 'sell' in action buttons: {labels}",
        )


if __name__ == "__main__":
    unittest.main()
