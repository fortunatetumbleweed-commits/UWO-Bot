"""Slice 3.5 — composite_buttons region detector.

Tests cover:
  - The full pattern: icon + numeric cost + action verb fused as one
    composite button.
  - Patterns with cost but no icon (icon was missed by OmniParser).
  - Patterns with verb only (plain action button without payment).
  - 'Free' cost label parsed as 0.
  - Multiple composites on screen (Cathedral / Pray with Free + 75 gem
    cards); the rightmost-bottommost is marked is_positive.
  - Scope: verbs outside the search area aren't picked.

End-to-end tests against captured frames behind SCENE_MODEL_E2E=1.
"""

from __future__ import annotations

import os
import unittest

from PIL import Image

from vision.omniparser import DetectedElement
from vision.region_detectors.composite_buttons import detect_composite_buttons
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


def _item_shop_purchase_composite() -> list:
    """Item Shop / Gear: composite [icon] 43,200 Purchase at bottom-right."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Gear", element_type="text", cx=140, cy=50, w=100, h=40),
        # The composite: icon at left, cost number, action verb at right
        _el(element_type="icon", cx=2000, cy=998, w=60, h=60),
        _el(label="43,200", element_type="text",
            cx=2080, cy=1000, w=110, h=36),
        _el(label="Purchase", element_type="text",
            cx=2210, cy=1000, w=140, h=36),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _cathedral_pray_two_composites() -> list:
    """Cathedral / Pray sub-menu has TWO composite cards:
       'Free' card on the left, '75 (Special Prayer)' card on the right.

    The rightmost composite should be marked is_positive=True.
    """
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Pray", element_type="text", cx=140, cy=50, w=100, h=40),
        # Left card: 'Free' (no cost icon needed for 'Free' but include
        # an icon to verify icon_present is True when present)
        _el(element_type="icon", cx=830, cy=550, w=40, h=40),
        _el(label="Free", element_type="text",
            cx=900, cy=550, w=80,  h=36),
        # Hmm, for Cathedral the action verb is below the card not next
        # to the price.  Let me restructure: use "Pray" as the verb at
        # the bottom of each card — but those would conflict.  For test
        # simplicity, use distinct verbs.  Real Cathedral cards have
        # 'Pray' as the action; we accept both being 'Pray' but the
        # rightmost wins is_positive.
        # Right card: 75-cost (Special Prayer)
        _el(element_type="icon", cx=1530, cy=550, w=40, h=40),
        _el(label="75", element_type="text",
            cx=1600, cy=550, w=80,  h=36),
        _el(label="Donate", element_type="text",
            cx=1720, cy=550, w=130, h=36),
        # Left composite — Pray verb at the same y as the Free icon/text
        _el(label="Pray", element_type="text",
            cx=1020, cy=550, w=100, h=36),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _confirm_only_no_cost() -> list:
    """A plain action button with no cost shown (e.g. Confirm/Cancel
    dialog).  Detector should still pick up the verb as a composite
    with cost_value=None and icon_present=False."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Dialog", element_type="text",
            cx=140, cy=50, w=100, h=40),
        _el(label="Confirm", element_type="text",
            cx=1500, cy=600, w=160, h=40),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _cost_without_icon() -> list:
    """OmniParser sometimes misses the small currency icon — we should
    still detect the composite when verb + cost are present."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Sell", element_type="text", cx=140, cy=50, w=100, h=40),
        # cost without icon
        _el(label="1,500", element_type="text",
            cx=2080, cy=1000, w=110, h=36),
        _el(label="Sell", element_type="text",
            cx=2220, cy=1000, w=90,  h=36),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _world_map_go_to_city() -> list:
    """World map City Info panel: 'Go to City' composite button."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="World Map", element_type="text",
            cx=200, cy=50, w=240, h=40),
        _el(element_type="icon", cx=40, cy=80, w=38, h=38),    # tutorial ?
        _el(label="Go to City", element_type="text",
            cx=1200, cy=970, w=200, h=40),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _overworld_no_composites() -> list:
    """Overworld scene — no action verbs visible."""
    return [
        _el(element_type="icon", cx=80, cy=120, w=120, h=160),
        _el(element_type="icon", cx=200, cy=60, w=50, h=28),
        _el(label="Amsterdam", element_type="text",
            cx=430, cy=55, w=200, h=40),
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


# ── Direct detector tests ─────────────────────────────────────────────────


class CompositeButtonDetectorTests(unittest.TestCase):

    def test_purchase_with_cost_and_icon(self):
        region = detect_composite_buttons(
            _item_shop_purchase_composite(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(region)
        self.assertEqual(len(region.buttons), 1)
        b = region.buttons[0]
        self.assertEqual(b.action_label, "Purchase")
        self.assertEqual(b.cost_value, 43200)
        self.assertTrue(b.icon_present)
        self.assertTrue(b.is_positive)
        # cost_currency is None in slice 3.5 (TODO).
        self.assertIsNone(b.cost_currency)

    def test_two_composites_rightmost_is_positive(self):
        region = detect_composite_buttons(
            _cathedral_pray_two_composites(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(region)
        # Two composites: 'Pray' (Free) and 'Donate' (75)
        labels = region.labels()
        self.assertIn("Pray", labels)
        self.assertIn("Donate", labels)
        # The rightmost-bottommost composite is positive
        positive = region.positive()
        self.assertIsNotNone(positive)
        # 'Donate' is rightmost
        self.assertEqual(positive.action_label, "Donate")
        # 'Free' parses to 0
        pray = region.find("Pray")
        self.assertEqual(pray.cost_value, 0)
        donate = region.find("Donate")
        self.assertEqual(donate.cost_value, 75)

    def test_plain_action_no_cost(self):
        region = detect_composite_buttons(
            _confirm_only_no_cost(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(region)
        self.assertEqual(len(region.buttons), 1)
        b = region.buttons[0]
        self.assertEqual(b.action_label, "Confirm")
        self.assertIsNone(b.cost_value)
        self.assertFalse(b.icon_present)
        self.assertTrue(b.is_positive)   # single button → positive

    def test_cost_without_icon(self):
        region = detect_composite_buttons(
            _cost_without_icon(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(region)
        self.assertEqual(len(region.buttons), 1)
        b = region.buttons[0]
        self.assertEqual(b.action_label, "Sell")
        self.assertEqual(b.cost_value, 1500)
        # No icon detected adjacent — flag is False
        self.assertFalse(b.icon_present)

    def test_world_map_go_to_city(self):
        region = detect_composite_buttons(
            _world_map_go_to_city(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(region)
        # 'Go to City' is multi-word; only that one matches
        self.assertEqual(region.labels(), ["Go to City"])

    def test_overworld_no_verbs_returns_none(self):
        region = detect_composite_buttons(
            _overworld_no_composites(), FRAME_W, FRAME_H,
        )
        # No verbs anywhere → None
        self.assertIsNone(region)

    def test_find_case_insensitive(self):
        region = detect_composite_buttons(
            _item_shop_purchase_composite(), FRAME_W, FRAME_H,
        )
        self.assertIsNotNone(region.find("purchase"))
        self.assertIsNotNone(region.find("PURCHASE"))
        self.assertIsNone(region.find("Sell"))

    def test_cost_value_parsing(self):
        from vision.region_detectors.composite_buttons import _parse_cost
        self.assertEqual(_parse_cost("43,200"), 43200)
        self.assertEqual(_parse_cost("1,890,000"), 1890000)
        self.assertEqual(_parse_cost("0"), 0)
        self.assertEqual(_parse_cost("Free"), 0)
        self.assertEqual(_parse_cost("FREE"), 0)
        self.assertIsNone(_parse_cost("not a number"))


# ── Scene-model integration tests ─────────────────────────────────────────


class SceneModelCompositeButtonsIntegrationTests(unittest.TestCase):

    def test_chromed_scene_populates_composite_buttons(self):
        sm = detect_scene(FRAME_W, FRAME_H, _item_shop_purchase_composite())
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.composite_buttons)
        self.assertEqual(len(sm.composite_buttons.buttons), 1)
        self.assertTrue(
            any("composite_buttons" in s for s in sm.signals_matched),
            f"composite_buttons signal missing: {sm.signals_matched}",
        )

    def test_overworld_scene_no_composite_buttons(self):
        sm = detect_scene(FRAME_W, FRAME_H, _overworld_no_composites())
        self.assertEqual(sm.scene_family, "overworld")
        self.assertIsNone(sm.composite_buttons)


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

    def test_item_shop_gear_purchase_composite(self):
        sm = self._scene(
            "data/sessions/explore_amsterdanted_2026-05-17_01-27-48/"
            "item_shop/frames/0001__item_shop_gear__7789856073.png"
        )
        self.assertEqual(sm.scene_family, "chromed")
        # Composite buttons detector should find the Purchase composite
        self.assertIsNotNone(sm.composite_buttons)
        labels = [l.lower() for l in sm.composite_buttons.labels()]
        self.assertTrue(
            any("purchase" in l for l in labels),
            f"expected 'purchase' composite: {labels}",
        )

    def test_item_shop_sell_composite(self):
        sm = self._scene(
            "data/sessions/explore_amsterdanted_2026-05-17_01-27-48/"
            "item_shop/frames/0004__item_shop_sell__7789859441.png"
        )
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.composite_buttons)
        labels = [l.lower() for l in sm.composite_buttons.labels()]
        self.assertTrue(
            any("sell" in l for l in labels),
            f"expected 'sell' composite: {labels}",
        )


if __name__ == "__main__":
    unittest.main()
