"""Slice 6 — exit_to_overworld migrated to SceneModel.

Tests cover the `_is_on_overworld` helper which is the primary loop
signal for exit_to_overworld.  The migration:

  - High-confidence SceneModel verdict is authoritative.
    - port_overworld → return True (skip legacy fallback)
    - chromed / world_map / sea → return False (skip legacy fallback)
  - Medium/low-confidence SceneModel → fall through to legacy chrome
    heuristic (the pre-slice-6 behaviour).

These tests force each branch by stubbing `get_scene_model`.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from PIL import Image

from vision.scene_model import (
    SceneModel, TitleField, TopLeftRegion,
)


def _frame():
    return Image.new("RGB", (2400, 1080), color=(0, 0, 0))


def _scene_port_overworld(port="Amsterdam", confidence="high"):
    return SceneModel(
        scene_family="overworld",
        scene_kind="port_overworld",
        confidence=confidence,
        top_left=TopLeftRegion(
            family="overworld",
            big_icon="lighthouse",
            title=TitleField(text=port, raw_ocr=port, bbox=(0, 0, 100, 40)),
        ),
    )


def _scene_inside_building(name="inn", confidence="high"):
    return SceneModel(
        scene_family="chromed",
        scene_kind=f"building:{name}",
        confidence=confidence,
        top_left=TopLeftRegion(
            family="chromed",
            back_arrow=True,
            title=TitleField(text=name, raw_ocr=name, bbox=(0, 0, 100, 40)),
        ),
    )


def _scene_low_confidence_unknown():
    return SceneModel(
        scene_family="unknown", scene_kind="unknown", confidence="low",
    )


def _chrome_state(*, has_home=False, has_back_arrow=False,
                  has_hamburger=False, has_right_panel=False):
    """Build a stub ChromeState for the legacy fallback path."""
    s = MagicMock()
    s.has_home = has_home
    s.has_back_arrow = has_back_arrow
    s.has_hamburger = has_hamburger
    s.has_right_panel = has_right_panel
    return s


# ── Test cases ──────────────────────────────────────────────────────────


class IsOnOverworldSceneModelPrimaryTests(unittest.TestCase):
    """High-confidence SceneModel verdict is authoritative — legacy
    chrome heuristic never runs."""

    def test_high_confidence_overworld_returns_true(self):
        from actions.sail_actions import _is_on_overworld
        with patch(
            "vision.scene_model.get_scene_model",
            return_value=_scene_port_overworld(),
        ), patch(
            "vision.chrome_detector.get_chrome_detector",
        ) as fake_chrome:
            # Chrome detector MUST NOT be called when SceneModel is
            # high-confidence — fail loudly if it is.
            fake_chrome.side_effect = AssertionError(
                "chrome heuristic must not run when SceneModel is high-confidence",
            )
            self.assertTrue(_is_on_overworld(_frame()))

    def test_high_confidence_building_returns_false(self):
        from actions.sail_actions import _is_on_overworld
        with patch(
            "vision.scene_model.get_scene_model",
            return_value=_scene_inside_building("inn"),
        ), patch(
            "vision.chrome_detector.get_chrome_detector",
        ) as fake_chrome:
            fake_chrome.side_effect = AssertionError(
                "chrome heuristic must not run when SceneModel is high-confidence",
            )
            self.assertFalse(_is_on_overworld(_frame()))

    def test_high_confidence_sea_returns_false(self):
        from actions.sail_actions import _is_on_overworld
        sm = SceneModel(
            scene_family="overworld", scene_kind="sea", confidence="high",
        )
        with patch(
            "vision.scene_model.get_scene_model", return_value=sm,
        ), patch(
            "vision.chrome_detector.get_chrome_detector",
        ) as fake_chrome:
            fake_chrome.side_effect = AssertionError(
                "chrome heuristic must not run when SceneModel is high-confidence",
            )
            self.assertFalse(_is_on_overworld(_frame()))

    def test_high_confidence_world_map_returns_false(self):
        from actions.sail_actions import _is_on_overworld
        sm = SceneModel(
            scene_family="world_map", scene_kind="world_map",
            confidence="high",
        )
        with patch(
            "vision.scene_model.get_scene_model", return_value=sm,
        ), patch(
            "vision.chrome_detector.get_chrome_detector",
        ) as fake_chrome:
            fake_chrome.side_effect = AssertionError(
                "chrome heuristic must not run when SceneModel is high-confidence",
            )
            self.assertFalse(_is_on_overworld(_frame()))


class IsOnOverworldLegacyFallbackTests(unittest.TestCase):
    """Low/medium-confidence SceneModel → legacy chrome heuristic runs.
    This preserves pre-slice-6 behaviour for cases where the SceneModel
    couldn't make a confident call."""

    def test_low_confidence_falls_through_to_legacy_chrome_check(self):
        from actions.sail_actions import _is_on_overworld
        with patch(
            "vision.scene_model.get_scene_model",
            return_value=_scene_low_confidence_unknown(),
        ), patch(
            "vision.chrome_detector.get_chrome_detector",
        ) as fake_chrome, patch(
            "vision.ocr.read_port_name", return_value="Amsterdam",
        ):
            fake_chrome.return_value.detect.return_value = _chrome_state()
            # home=False, back=False, port=Amsterdam → legacy says overworld
            self.assertTrue(_is_on_overworld(_frame()))

    def test_low_confidence_home_button_visible_returns_false(self):
        from actions.sail_actions import _is_on_overworld
        with patch(
            "vision.scene_model.get_scene_model",
            return_value=_scene_low_confidence_unknown(),
        ), patch(
            "vision.chrome_detector.get_chrome_detector",
        ) as fake_chrome:
            fake_chrome.return_value.detect.return_value = _chrome_state(
                has_home=True,
            )
            # Legacy: home button visible → not overworld
            self.assertFalse(_is_on_overworld(_frame()))

    def test_low_confidence_back_arrow_returns_false(self):
        from actions.sail_actions import _is_on_overworld
        with patch(
            "vision.scene_model.get_scene_model",
            return_value=_scene_low_confidence_unknown(),
        ), patch(
            "vision.chrome_detector.get_chrome_detector",
        ) as fake_chrome, patch(
            "vision.ocr.read_screen_title", return_value="Inn",
        ):
            fake_chrome.return_value.detect.return_value = _chrome_state(
                has_back_arrow=True,
            )
            self.assertFalse(_is_on_overworld(_frame()))


class IsOnOverworldMediumConfidenceTests(unittest.TestCase):
    """Medium confidence also falls through to legacy (only high is
    authoritative).  This is the conservative migration choice — we
    only trust the SceneModel when it's most sure."""

    def test_medium_confidence_falls_through(self):
        from actions.sail_actions import _is_on_overworld
        sm = _scene_port_overworld(confidence="medium")
        with patch(
            "vision.scene_model.get_scene_model", return_value=sm,
        ), patch(
            "vision.chrome_detector.get_chrome_detector",
        ) as fake_chrome, patch(
            "vision.ocr.read_port_name", return_value="Amsterdam",
        ):
            fake_chrome.return_value.detect.return_value = _chrome_state()
            # Medium SceneModel → legacy runs.  Legacy with port name
            # visible says overworld → True.
            self.assertTrue(_is_on_overworld(_frame()))


if __name__ == "__main__":
    unittest.main()
