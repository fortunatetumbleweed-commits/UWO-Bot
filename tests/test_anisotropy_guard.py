"""Tests for the anisotropy guard on persisted world-map scale.

Regression: 2026-05-20 — bot saved a calibration with scale_x=1.92 and
scale_y=0.86 (>2x ratio).  The world map renders isotropically (same
zoom on both axes), so any large ratio between the two scales is a
calibration error, not a real signal.  Loading that file poisoned every
subsequent pan attempt: stride pans overshot wildly on x, undershot on
y, and the bot oscillated.

Guard: reject anisotropic scales on both load (delete corrupt cache)
and save (refuse to write).
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from actions.world_map_nav import (
    _is_isotropic_scale,
    _MAX_ANISOTROPY_RATIO,
    _load_persisted_scale,
    _save_persisted_scale,
)


class IsIsotropicScaleTests(unittest.TestCase):

    def test_equal_scales_are_isotropic(self):
        self.assertTrue(_is_isotropic_scale(2.5, 2.5))

    def test_small_ratio_is_isotropic(self):
        # 2.5 / 2.4 = 1.04 — well under the 1.5 cap.
        self.assertTrue(_is_isotropic_scale(2.5, 2.4))

    def test_at_threshold_is_isotropic(self):
        # exactly at the ratio cap is allowed.
        self.assertTrue(_is_isotropic_scale(1.0, _MAX_ANISOTROPY_RATIO))

    def test_large_ratio_is_anisotropic(self):
        # 1.92 / 0.86 ≈ 2.23 — well past 1.5.
        self.assertFalse(_is_isotropic_scale(1.92, 0.86))

    def test_zero_or_negative_rejected(self):
        self.assertFalse(_is_isotropic_scale(0, 2.5))
        self.assertFalse(_is_isotropic_scale(2.5, 0))
        self.assertFalse(_is_isotropic_scale(-1.0, 2.5))

    def test_none_rejected(self):
        self.assertFalse(_is_isotropic_scale(None, 2.5))
        self.assertFalse(_is_isotropic_scale(2.5, None))


class LoadRejectsAnisotropicTests(unittest.TestCase):
    """A poisoned calibration.json on disk must be detected on load and
    deleted, so the next session starts clean instead of reapplying the
    bad scale forever.
    """

    def test_anisotropic_file_returns_none_and_deletes(self):
        anisotropic = {"scale_x": 1.92, "scale_y": 0.86}
        fake_path = unittest.mock.MagicMock(spec=Path)
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = json.dumps(anisotropic)

        with patch("actions.world_map_nav._SCALE_CACHE_PATH", fake_path):
            sx, sy = _load_persisted_scale()

        self.assertIsNone(sx)
        self.assertIsNone(sy)
        fake_path.unlink.assert_called_once()

    def test_isotropic_file_returns_values(self):
        isotropic = {"scale_x": 2.4, "scale_y": 2.5}
        fake_path = unittest.mock.MagicMock(spec=Path)
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = json.dumps(isotropic)

        with patch("actions.world_map_nav._SCALE_CACHE_PATH", fake_path):
            sx, sy = _load_persisted_scale()

        self.assertEqual(sx, 2.4)
        self.assertEqual(sy, 2.5)
        fake_path.unlink.assert_not_called()


class SaveRejectsAnisotropicTests(unittest.TestCase):
    """If a bad calibration somehow produces an anisotropic pair, refuse
    to write it to disk — otherwise the next session reloads a poisoned
    cache (the exact failure mode this guard exists to prevent)."""

    def test_anisotropic_values_not_written(self):
        fake_path = unittest.mock.MagicMock(spec=Path)
        with patch("actions.world_map_nav._SCALE_CACHE_PATH", fake_path):
            _save_persisted_scale(1.92, 0.86)
        fake_path.write_text.assert_not_called()

    def test_isotropic_values_are_written(self):
        fake_path = unittest.mock.MagicMock(spec=Path)
        fake_path.parent.mkdir = unittest.mock.MagicMock()
        with patch("actions.world_map_nav._SCALE_CACHE_PATH", fake_path):
            _save_persisted_scale(2.4, 2.5)
        fake_path.write_text.assert_called_once()


if __name__ == "__main__":
    unittest.main()
