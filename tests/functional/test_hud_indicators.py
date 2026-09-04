"""Tests for #15 HUD indicator detectors: currency icon colour + red badge."""
import unittest
from pathlib import Path

from PIL import Image

from vision.hud_indicators import (
    classify_currency_icon, has_red_badge, red_pixel_count, find_red_badges,
)

_F0008 = ("data/sessions/barter_apache_walkthrough_2026-08-14T12-48-30/frame_0008.png")


class CurrencyIconTests(unittest.TestCase):
    def test_real_sampled_red_and_green(self):
        # Means sampled from the live HUD icons.
        self.assertEqual(classify_currency_icon((103, 55, 52)), "red_gem")
        self.assertEqual(classify_currency_icon((64, 140, 68)), "action_points")

    def test_gold_ducat_and_blue_gem(self):
        self.assertEqual(classify_currency_icon((210, 180, 70)), "ducat")
        self.assertEqual(classify_currency_icon((60, 90, 200)), "blue_gem")

    def test_gray_is_none(self):
        self.assertIsNone(classify_currency_icon((100, 100, 100)))
        self.assertIsNone(classify_currency_icon((40, 40, 45)))


def _img(color, size=(60, 40)):
    return Image.new("RGB", size, color)


class RedBadgeSyntheticTests(unittest.TestCase):
    def test_red_patch_detected(self):
        img = Image.new("RGB", (100, 100), (30, 30, 30))
        for y in range(10, 40):
            for x in range(10, 40):
                img.putpixel((x, y), (238, 33, 67))   # 900-px red badge
        self.assertTrue(has_red_badge(img))
        self.assertGreater(red_pixel_count(img), 500)

    def test_no_red_no_badge(self):
        self.assertFalse(has_red_badge(_img((30, 30, 30))))
        self.assertFalse(has_red_badge(_img((40, 120, 200))))   # blue, not red

    def test_box_scopes_detection(self):
        img = Image.new("RGB", (200, 100), (30, 30, 30))
        for y in range(10, 30):
            for x in range(150, 190):
                img.putpixel((x, y), (240, 30, 60))
        self.assertTrue(has_red_badge(img, box=(140, 0, 200, 60)))   # badge here
        self.assertFalse(has_red_badge(img, box=(0, 0, 100, 100)))   # not here

    def test_find_centroid_in_box(self):
        img = Image.new("RGB", (200, 100), (30, 30, 30))
        for y in range(40, 70):
            for x in range(80, 120):
                img.putpixel((x, y), (240, 30, 60))
        centers = find_red_badges(img)
        self.assertEqual(len(centers), 1)
        cx, cy = centers[0]
        self.assertTrue(90 <= cx <= 110 and 50 <= cy <= 60)


class RedBadgeRealFrameTests(unittest.TestCase):
    def setUp(self):
        p = Path(_F0008)
        if not p.exists():
            self.skipTest("barter walkthrough frame not present")
        self.img = Image.open(p)

    def test_barter_menu_item_has_red_note(self):
        # Red note on the Barter menu item, sampled at x[128-339] y[567-600].
        self.assertTrue(has_red_badge(self.img, box=(120, 555, 350, 615)))

    def test_clean_menu_item_has_no_badge(self):
        # The 'Explore' menu item near the top has no attention badge.
        self.assertFalse(has_red_badge(self.img, box=(180, 140, 340, 190)))


if __name__ == "__main__":
    unittest.main()
