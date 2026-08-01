"""Phase 2.3 (cont.) migration tests for the remaining dismiss handlers:

  - _dismiss_close_button       — uses DialogModel.close_button bbox
  - _dismiss_tap_ok_or_x        — X-close first, then OK action; bbox-scoped
  - _dismiss_tap_decline        — Cancel/No/Decline action inside bbox

Each test mocks _detect_dialog_on_frame to return a tailored DialogModel
and verifies the handler taps the typed bbox.  Fallback paths are also
covered.
"""
import unittest
from unittest.mock import patch, MagicMock

from vision.region_detectors.dialog import DialogModel, DialogAction


def _fake_frame(w=2400, h=1080):
    f = MagicMock(); f.width = w; f.height = h; f.size = (w, h)
    return f


def _dialog_with_close(x1=2150, y1=300, x2=2250, y2=400):
    return DialogModel(
        bbox=(800, 200, 2300, 900),
        close_button=(x1, y1, x2, y2),
        anchors_fired=("close",),
    )


def _dialog_with_actions(actions):
    return DialogModel(
        bbox=(800, 200, 2300, 900),
        actions=tuple(actions),
        anchors_fired=("actions",),
    )


# ── _dismiss_close_button ──────────────────────────────────────────


class DismissCloseButtonTypedTests(unittest.TestCase):

    def test_kb_position_takes_priority_over_dialog(self):
        from brain import perceive as P
        with patch.object(P, "_detect_dialog_on_frame",
                          return_value=_dialog_with_close()), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap:
            P._dismiss_close_button(_fake_frame(), "daily_news", [1000, 50])
            mock_tap.assert_called_once_with(1000, 50)

    def test_dialog_close_bbox_used_when_no_kb_position(self):
        from brain import perceive as P
        with patch.object(P, "_detect_dialog_on_frame",
                          return_value=_dialog_with_close(2150, 300, 2250, 400)), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button") as mock_find:
            P._dismiss_close_button(_fake_frame(), "perk_event", None)
            # centre of (2150, 300, 2250, 400)
            mock_tap.assert_called_once_with(2200, 350)
            mock_find.assert_not_called()

    def test_keyword_fallback_when_no_dialog(self):
        from brain import perceive as P
        with patch.object(P, "_detect_dialog_on_frame", return_value=None), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button",
                   return_value=(2100, 350)):
            P._dismiss_close_button(_fake_frame(), "iid", None)
            mock_tap.assert_called_once_with(2100, 350)


# ── _dismiss_tap_ok_or_x ───────────────────────────────────────────


class DismissTapOkOrXTypedTests(unittest.TestCase):

    def test_dialog_close_button_inside_bbox_taps_x(self):
        from brain import perceive as P
        bbox = (800, 200, 2300, 900)
        with patch.object(P, "_detect_dialog_on_frame",
                          return_value=_dialog_with_close(2150, 300, 2250, 400)), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button") as mock_find:
            P._dismiss_tap_ok_or_x(_fake_frame(), obstruction_bbox=bbox)
            mock_tap.assert_called_once_with(2200, 350)
            mock_find.assert_not_called()

    def test_dialog_close_outside_bbox_falls_to_keyword(self):
        """Close button outside obstruction bbox must NOT trip the typed path."""
        from brain import perceive as P
        bbox = (800, 200, 1800, 900)   # narrower than the close button at cx=2200
        with patch.object(P, "_detect_dialog_on_frame",
                          return_value=_dialog_with_close(2150, 300, 2250, 400)), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button",
                   return_value=(1500, 700)) as mock_find:
            P._dismiss_tap_ok_or_x(_fake_frame(), obstruction_bbox=bbox)
            mock_find.assert_called()    # fallback engaged
            mock_tap.assert_called_once_with(1500, 700)

    def test_no_close_uses_ok_action(self):
        from brain import perceive as P
        bbox = (800, 200, 2300, 900)
        dlg = _dialog_with_actions([
            DialogAction(label="OK", bbox=(1100, 800, 1300, 870), is_positive=True),
        ])
        with patch.object(P, "_detect_dialog_on_frame", return_value=dlg), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap:
            P._dismiss_tap_ok_or_x(_fake_frame(), obstruction_bbox=bbox)
            mock_tap.assert_called_once_with(1200, 835)


# ── _dismiss_tap_decline ───────────────────────────────────────────


class DismissTapDeclineTypedTests(unittest.TestCase):

    def test_cancel_action_inside_bbox_taps_typed(self):
        from brain import perceive as P
        bbox = (800, 200, 1900, 900)
        dlg = _dialog_with_actions([
            DialogAction(label="Cancel", bbox=(1100, 800, 1300, 870)),
        ])
        with patch.object(P, "_detect_dialog_on_frame", return_value=dlg), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button") as mock_find:
            P._dismiss_tap_decline(_fake_frame(), obstruction_bbox=bbox)
            mock_tap.assert_called_once_with(1200, 835)
            mock_find.assert_not_called()

    def test_cancel_outside_bbox_falls_to_keyword(self):
        from brain import perceive as P
        bbox = (800, 200, 1100, 400)   # tiny bbox far from button
        dlg = _dialog_with_actions([
            DialogAction(label="Cancel", bbox=(1500, 800, 1700, 870)),
        ])
        with patch.object(P, "_detect_dialog_on_frame", return_value=dlg), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button",
                   return_value=None) as mock_find:
            P._dismiss_tap_decline(_fake_frame(), obstruction_bbox=bbox)
            mock_find.assert_called()
            mock_tap.assert_not_called()

    def test_no_bbox_returns_silently(self):
        """Existing contract: tap_decline without bbox is a no-op for safety."""
        from brain import perceive as P
        with patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button") as mock_find:
            P._dismiss_tap_decline(_fake_frame(), obstruction_bbox=None)
            mock_tap.assert_not_called()
            mock_find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
