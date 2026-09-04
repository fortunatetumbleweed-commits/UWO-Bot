"""Slice 2 — left_menu region detector.

Tests cover:
  - The Inn menu with 5 items, Hire locked by an Unavailable badge.
  - The Item Shop menu with 4 items, none locked.
  - The Cathedral menu with 2 items.
  - Overworld scenes (no menu) — returns None or empty items.
  - Lock-indicator scoping (right-panel Unavailable doesn't lock the
    left-strip item in the same Y band).
  - Filter of bare-numeric labels (shield count '20' rejected).
  - Title/back-arrow chrome NOT included as menu items.

End-to-end tests against captured frames are gated behind
SCENE_MODEL_E2E=1 (loads OmniParser).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from PIL import Image

from vision.omniparser import DetectedElement
from vision.region_detectors.left_menu import detect_left_menu
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


def _inn_elements() -> list:
    """Inn menu: 5 items, Hire is locked (Unavailable badge nearby)."""
    return [
        # Back arrow + title (chrome — must NOT show up as menu items)
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Inn", element_type="text", cx=140, cy=50, w=100, h=40),
        # Left menu column at cx ≈ 200
        _el(label="Hire",         cx=200, cy=150, w=120, h=40),
        _el(label="Party",        cx=200, cy=265, w=120, h=40),
        _el(label="Recruit Crew", cx=200, cy=370, w=180, h=40),
        _el(label="Employee",     cx=200, cy=480, w=150, h=40),
        _el(label="Manage Mate",  cx=200, cy=590, w=180, h=40),
        # Unavailable badge on Hire — same column (cx 220, |200-220|=20 within 150)
        _el(label="Unavailable",  cx=220, cy=110, w=140, h=24),
        # Bottom chrome (Language Effect, server, etc.) — must be excluded
        _el(label="Language Effect", cx=290, cy=1010, w=180, h=30),
        _el(label="Atlantic Ocean",  cx=1900, cy=1050, w=200, h=22),
    ]


def _item_shop_gear_elements() -> list:
    """Item Shop / Gear sub-menu: 4 items in left column, none locked.

    Plus a 'Mate Appearance' tab in the centre — must NOT be picked as
    a menu item because its cx is outside the left zone.
    """
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),     # back
        _el(label="Gear", element_type="text", cx=130, cy=50, w=100, h=40),
        # Left menu
        _el(label="Gear",         cx=200, cy=158, w=120, h=40),
        _el(label="Tool",         cx=200, cy=263, w=100, h=40),
        _el(label="Black Market", cx=200, cy=372, w=200, h=40),
        _el(label="Sell",         cx=200, cy=480, w=80,  h=40),
        # Centre tabs — these are at cx > 350 (outside left zone), must
        # be excluded.
        _el(label="All",             cx=570,  cy=140, w=80,  h=30),
        _el(label="Weapon",          cx=720,  cy=140, w=100, h=30),
        _el(label="Mate Appearance", cx=1500, cy=140, w=240, h=30),
        # Bottom chrome
        _el(label="Atlantic Ocean", cx=1900, cy=1050, w=200, h=22),
    ]


def _cathedral_pray_elements() -> list:
    """Cathedral menu: only 2 items."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Cathedral", element_type="text", cx=180, cy=50, w=180, h=40),
        _el(label="Pray",   cx=200, cy=158, w=80,  h=40),
        _el(label="Donate", cx=200, cy=265, w=120, h=40),
        _el(label="Atlantic Ocean", cx=1900, cy=1050, w=200, h=22),
    ]


def _port_overworld_elements() -> list:
    """Overworld scene — no left menu present.  Building list is on
    the right (cx > 2000), which should not match the left menu zone."""
    return [
        # Top-left compound
        _el(element_type="icon", cx=80, cy=120, w=120, h=160),
        _el(element_type="icon", cx=200, cy=60, w=50, h=28),
        _el(label="Amsterdam", element_type="text",
            cx=430, cy=55, w=200, h=40),
        # Building list on the RIGHT panel (cx ≈ 2100)
        _el(label="Harbor", element_type="text",
            cx=2090, cy=460, w=100, h=30),
        _el(label="Market", element_type="text",
            cx=2090, cy=540, w=100, h=30),
        # Bottom chrome
        _el(label="Atlantic Ocean", element_type="text",
            cx=1900, cy=1050, w=200, h=22),
    ]


def _right_panel_unavailable_does_not_lock_inn_menu_elements() -> list:
    """Inn variant where 'Unavailable' appears in the RIGHT panel
    (Inn's empty Hire panel says 'Unavailable / No Mate has visited').
    The right-panel Unavailable is at cx ~ 2100; menu items at cx ~ 200.
    The cx delta (1900) is well outside the ±150 lock-indicator
    tolerance, so Hire must NOT be marked locked here."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Inn", element_type="text", cx=140, cy=50, w=100, h=40),
        _el(label="Hire",         cx=200, cy=150, w=120, h=40),
        _el(label="Party",        cx=200, cy=265, w=120, h=40),
        # Right panel: Unavailable badge at the same y as Hire BUT
        # in a different column.
        _el(label="Unavailable", cx=2100, cy=150, w=180, h=24),
        _el(label="Atlantic Ocean", cx=1900, cy=1050, w=200, h=22),
    ]


def _shield_count_not_menu_elements() -> list:
    """Item Shop frame where the shield count '20' (numeric label) is
    in the left zone but must NOT be picked as a menu item — '20' has
    no alphabetic characters."""
    return [
        _el(element_type="icon", cx=40, cy=50, w=40, h=40),
        _el(label="Gear", element_type="text", cx=130, cy=50, w=100, h=40),
        _el(label="Gear",         cx=200, cy=158, w=120, h=40),
        _el(label="Tool",         cx=200, cy=263, w=100, h=40),
        # Shield count appearing in the left zone but numeric-only
        _el(label="20",           element_type="button",
            cx=250, cy=200, w=60, h=60),
        _el(label="Atlantic Ocean", cx=1900, cy=1050, w=200, h=22),
    ]


# ── Direct detector tests ─────────────────────────────────────────────────


class LeftMenuDetectorTests(unittest.TestCase):

    def test_inn_five_items_hire_locked(self):
        menu = detect_left_menu(_inn_elements(), FRAME_W, FRAME_H)
        self.assertIsNotNone(menu)
        self.assertEqual(menu.labels(),
                         ["Hire", "Party", "Recruit Crew",
                          "Employee", "Manage Mate"])
        # Hire is locked; the others aren't.
        self.assertEqual(
            [(it["label"], it["is_locked"]) for it in menu.items],
            [("Hire", True), ("Party", False), ("Recruit Crew", False),
             ("Employee", False), ("Manage Mate", False)],
        )
        # Convenience accessor
        self.assertEqual(
            menu.unlocked_labels(),
            ["Party", "Recruit Crew", "Employee", "Manage Mate"],
        )

    def test_item_shop_gear_four_items_no_locks(self):
        menu = detect_left_menu(_item_shop_gear_elements(), FRAME_W, FRAME_H)
        self.assertIsNotNone(menu)
        self.assertEqual(menu.labels(),
                         ["Gear", "Tool", "Black Market", "Sell"])
        self.assertTrue(all(not it["is_locked"] for it in menu.items))

    def test_centre_tabs_not_picked_as_menu(self):
        menu = detect_left_menu(_item_shop_gear_elements(), FRAME_W, FRAME_H)
        # The centre filter tabs (All, Weapon, Mate Appearance) live at
        # cx > 350, outside the left zone.  They must not appear.
        self.assertNotIn("All",             menu.labels())
        self.assertNotIn("Weapon",          menu.labels())
        self.assertNotIn("Mate Appearance", menu.labels())

    def test_cathedral_two_items(self):
        menu = detect_left_menu(_cathedral_pray_elements(), FRAME_W, FRAME_H)
        self.assertIsNotNone(menu)
        self.assertEqual(menu.labels(), ["Pray", "Donate"])

    def test_right_panel_unavailable_does_not_lock_left_strip(self):
        menu = detect_left_menu(
            _right_panel_unavailable_does_not_lock_inn_menu_elements(),
            FRAME_W, FRAME_H,
        )
        self.assertEqual(menu.labels(), ["Hire", "Party"])
        # Crucial: Hire is NOT marked locked when the Unavailable badge
        # is in the right panel rather than the left strip.
        self.assertEqual(
            [(it["label"], it["is_locked"]) for it in menu.items],
            [("Hire", False), ("Party", False)],
        )

    def test_shield_count_numeric_label_not_picked(self):
        menu = detect_left_menu(_shield_count_not_menu_elements(),
                                FRAME_W, FRAME_H)
        # '20' has no alphabetic chars — must be excluded.
        self.assertNotIn("20", menu.labels())
        self.assertEqual(menu.labels(), ["Gear", "Tool"])

    def test_overworld_no_left_menu(self):
        """On overworld there's no left-strip menu (building list is on
        the right).  Detector should return None or empty items."""
        menu = detect_left_menu(_port_overworld_elements(), FRAME_W, FRAME_H)
        # Detector may return None (no candidates at all) or
        # LeftMenuRegion(items=[]) (candidates exist but no cluster).
        # Both are acceptable; the SceneModel-level integration filters.
        if menu is not None:
            self.assertEqual(menu.items, [])

    def test_back_arrow_title_chrome_not_in_menu(self):
        """The back-arrow icon and title text live in the title region
        and must NOT be picked as menu items (they're filtered by the
        cy minimum of 110 px)."""
        menu = detect_left_menu(_inn_elements(), FRAME_W, FRAME_H)
        # Title 'Inn' is at cy=50, below the cy_min threshold (110).
        self.assertNotIn("Inn", menu.labels())

    def test_find_lookup_case_insensitive(self):
        menu = detect_left_menu(_inn_elements(), FRAME_W, FRAME_H)
        self.assertIsNotNone(menu.find("recruit crew"))
        self.assertIsNotNone(menu.find("RECRUIT CREW"))
        self.assertIsNotNone(menu.find("Recruit Crew"))
        self.assertIsNone(menu.find("Nonexistent"))


# ── Scene-model integration tests ─────────────────────────────────────────


class SceneModelLeftMenuIntegrationTests(unittest.TestCase):
    """detect_scene populates SceneModel.left_menu for chromed scenes
    but leaves it None for overworld scenes."""

    def test_chromed_scene_has_left_menu_populated(self):
        sm = detect_scene(FRAME_W, FRAME_H, _inn_elements())
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.left_menu)
        self.assertEqual(len(sm.left_menu.items), 5)
        # The detector contribution shows up in signals_matched
        self.assertTrue(
            any("left_menu_items" in s for s in sm.signals_matched),
            f"left_menu signal missing: {sm.signals_matched}",
        )

    def test_overworld_scene_has_no_left_menu(self):
        sm = detect_scene(FRAME_W, FRAME_H, _port_overworld_elements())
        self.assertEqual(sm.scene_family, "overworld")
        self.assertIsNone(sm.left_menu)


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

    def test_inn_frame_has_menu_with_five_items(self):
        sm = self._scene(
            "data/sessions/explore_amsterdanted_2026-05-17_01-27-48/"
            "frames/0000__inn_main__7789847232.png"
        )
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.left_menu)
        labels = [l.lower() for l in sm.left_menu.labels()]
        # We expect the 5 Inn menu items.  Some labels may be slightly
        # OCR-corrupted; require substring matches.
        for expected in ("hire", "party", "recruit", "employee", "manage"):
            self.assertTrue(
                any(expected in l for l in labels),
                f"expected '{expected}' in menu labels: {labels}",
            )

    def test_item_shop_gear_frame_has_menu(self):
        sm = self._scene(
            "data/sessions/explore_amsterdanted_2026-05-17_01-27-48/"
            "frames/0000__item_shop_main__7789855910.png"
        )
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.left_menu)
        labels = [l.lower() for l in sm.left_menu.labels()]
        for expected in ("gear", "tool", "black market", "sell"):
            self.assertTrue(
                any(expected in l for l in labels),
                f"expected '{expected}' in menu labels: {labels}",
            )

    def test_cathedral_frame_has_two_item_menu(self):
        sm = self._scene(
            "data/sessions/explore_amsterdanted_2026-05-17_01-27-48/"
            "frames/0000__cathedral_main__7789851131.png"
        )
        self.assertEqual(sm.scene_family, "chromed")
        self.assertIsNotNone(sm.left_menu)
        labels = [l.lower() for l in sm.left_menu.labels()]
        self.assertTrue(any("pray" in l for l in labels))
        self.assertTrue(any("donate" in l for l in labels))


if __name__ == "__main__":
    unittest.main()
