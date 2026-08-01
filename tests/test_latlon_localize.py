"""Tests for actions/latlon_localize.py.

Covers:
  • Affine roundtrip with a known transform on disk.
  • Water-pixel detection on a synthetic frame with blue and brown regions.
  • Candidate filtering preserves order, drops land pixels.

End-to-end localize_screen_center requires ADB + the OCR engine and is
exercised only at runtime against the live game.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from actions.latlon_localize import (
    affine_available,
    catalogue_to_latlon,
    filter_water_candidates,
    is_pixel_water,
    latlon_to_catalogue,
    reset_affine_cache,
)


# A fixed test transform.  Numbers chosen so manual verification is easy.
_TEST_AFFINE = {
    "transform": {
        "a": 100.0, "b":   0.0, "c": 5000.0,
        "d":   0.0, "e": -50.0, "f": 3000.0,
    },
}


def _write_affine(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


class AffineApplyTests(unittest.TestCase):

    def setUp(self):
        reset_affine_cache()
        # Write a temp affine and point the loader at it.
        self.tmp = Path("/tmp/uwo_test_affine.json")
        _write_affine(self.tmp, _TEST_AFFINE)
        self.patcher = patch(
            "actions.latlon_localize._AFFINE_PATH", self.tmp,
        )
        self.patcher.start()
        reset_affine_cache()

    def tearDown(self):
        self.patcher.stop()
        reset_affine_cache()
        try:
            self.tmp.unlink()
        except FileNotFoundError:
            pass

    def test_available(self):
        self.assertTrue(affine_available())

    def test_forward(self):
        # (lat=10, lon=20) → x = 100*20 + 5000 = 7000; y = -50*10 + 3000 = 2500
        self.assertEqual(latlon_to_catalogue(10.0, 20.0), (7000.0, 2500.0))

    def test_inverse(self):
        # (x=7000, y=2500) → (lat=10, lon=20)
        result = catalogue_to_latlon(7000.0, 2500.0)
        self.assertIsNotNone(result)
        lat, lon = result
        self.assertAlmostEqual(lat, 10.0, places=6)
        self.assertAlmostEqual(lon, 20.0, places=6)

    def test_roundtrip(self):
        for lat, lon in [(40.19, -9.91), (32.9, 13.2), (-15.0, 75.0)]:
            cat = latlon_to_catalogue(lat, lon)
            self.assertIsNotNone(cat)
            back = catalogue_to_latlon(*cat)
            self.assertIsNotNone(back)
            self.assertAlmostEqual(back[0], lat, places=4)
            self.assertAlmostEqual(back[1], lon, places=4)


class NoAffineTests(unittest.TestCase):
    """Functions return None gracefully when calibration hasn't been run."""

    def setUp(self):
        reset_affine_cache()
        self.patcher = patch(
            "actions.latlon_localize._AFFINE_PATH",
            Path("/tmp/this_file_does_not_exist_xyz.json"),
        )
        self.patcher.start()
        reset_affine_cache()

    def tearDown(self):
        self.patcher.stop()
        reset_affine_cache()

    def test_available_false(self):
        self.assertFalse(affine_available())

    def test_forward_returns_none(self):
        self.assertIsNone(latlon_to_catalogue(0.0, 0.0))

    def test_inverse_returns_none(self):
        self.assertIsNone(catalogue_to_latlon(0.0, 0.0))


def _make_frame(regions):
    """Build a synthetic test frame from (left, top, right, bottom, RGB) tuples.

    Pixels outside any region default to black."""
    w, h = 200, 100
    img = Image.new("RGB", (w, h), (0, 0, 0))
    for (l, t, r, b, color) in regions:
        for y in range(t, b):
            for x in range(l, r):
                img.putpixel((x, y), color)
    return img


class WaterDetectionTests(unittest.TestCase):

    def test_sea_green_pixel_is_water(self):
        # Discovered UWO sea: dark teal, G dominant over R.
        # Empirically observed RGB ≈ (53, 65, 52) on the live game.
        frame = _make_frame([(0, 0, 200, 100, (53, 65, 52))])
        self.assertTrue(is_pixel_water(frame, 100, 50))

    def test_brighter_sea_green_is_water(self):
        # Brighter daylight Mediterranean sea: still G > R.
        frame = _make_frame([(0, 0, 200, 100, (90, 130, 120))])
        self.assertTrue(is_pixel_water(frame, 100, 50))

    def test_sea_blue_with_higher_g_is_water(self):
        # Sea-blue (deeper water with G > R but B > G also).
        frame = _make_frame([(0, 0, 200, 100, (60, 110, 150))])
        self.assertTrue(is_pixel_water(frame, 100, 50))

    def test_brown_pixel_is_land(self):
        # Sahara-ish tan: R highest, G-R negative.
        frame = _make_frame([(0, 0, 200, 100, (200, 170, 120))])
        self.assertFalse(is_pixel_water(frame, 100, 50))

    def test_neutral_grey_is_land(self):
        # Balanced grey — G-R is zero, below the delta threshold.
        frame = _make_frame([(0, 0, 200, 100, (140, 140, 145))])
        self.assertFalse(is_pixel_water(frame, 100, 50))

    def test_out_of_bounds_is_false(self):
        frame = _make_frame([(0, 0, 200, 100, (60, 110, 150))])
        self.assertFalse(is_pixel_water(frame, -1, 50))
        self.assertFalse(is_pixel_water(frame, 100, 999))

    def test_filter_candidates_preserves_order(self):
        # Left half water (sea-green), right half land (sandy tan).
        frame = _make_frame([
            (0,   0, 100, 100, (53, 65, 52)),       # water
            (100, 0, 200, 100, (200, 170, 120)),    # land
        ])
        candidates = [(150, 50), (50, 50), (180, 50), (20, 50)]
        result = filter_water_candidates(frame, candidates)
        # Water pixels in input order: (50, 50), (20, 50).
        self.assertEqual(result, [(50, 50), (20, 50)])

    def test_filter_candidates_all_land(self):
        frame = _make_frame([(0, 0, 200, 100, (200, 170, 120))])
        self.assertEqual(filter_water_candidates(frame, [(50, 50), (150, 50)]), [])


if __name__ == "__main__":
    unittest.main()
