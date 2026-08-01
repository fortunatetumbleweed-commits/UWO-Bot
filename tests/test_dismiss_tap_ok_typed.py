"""Phase 2.3 migration test: _dismiss_tap_ok prefers DialogModel.actions
over the legacy keyword button search.
"""
import unittest
from unittest.mock import patch, MagicMock


class DismissTapOkTypedTests(unittest.TestCase):

    def _fake_frame(self, w=2400, h=1080):
        f = MagicMock()
        f.width = w; f.height = h
        f.size = (w, h)
        return f

    def _fake_dialog_with_ok(self):
        """Construct a DialogModel-shaped duck whose actions[0] is OK."""
        from vision.region_detectors.dialog import DialogModel, DialogAction
        return DialogModel(
            bbox=(800, 400, 1600, 900),
            actions=(
                DialogAction(label="OK", bbox=(1100, 800, 1300, 870), is_positive=True),
            ),
            anchors_fired=("actions",),
        )

    def test_uses_dialog_model_action_when_present(self):
        from brain import perceive as P
        f = self._fake_frame()
        with patch.object(P, "_detect_dialog_on_frame",
                          return_value=self._fake_dialog_with_ok()), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button") as mock_find:
            P._dismiss_tap_ok(f)
            # Tapped at centre of OK bbox: ((1100+1300)/2, (800+870)/2)
            mock_tap.assert_called_once_with(1200, 835)
            # Legacy fallback NOT called
            mock_find.assert_not_called()

    def test_falls_back_to_keyword_search_when_no_dialog(self):
        from brain import perceive as P
        f = self._fake_frame()
        with patch.object(P, "_detect_dialog_on_frame", return_value=None), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button",
                   return_value=(500, 600)) as mock_find:
            P._dismiss_tap_ok(f)
            mock_find.assert_called_once()
            mock_tap.assert_called_once_with(500, 600)

    def test_falls_back_to_centre_when_no_button_at_all(self):
        from brain import perceive as P
        f = self._fake_frame()
        with patch.object(P, "_detect_dialog_on_frame", return_value=None), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button", return_value=None):
            P._dismiss_tap_ok(f)
            mock_tap.assert_called_once_with(1200, 540)  # centre of 2400x1080

    def test_no_ok_action_in_dialog_falls_back_to_keyword(self):
        """DialogModel present but its actions don't contain OK/Confirm/Yes."""
        from brain import perceive as P
        from vision.region_detectors.dialog import DialogModel, DialogAction
        dialog_no_ok = DialogModel(
            bbox=(800, 400, 1600, 900),
            actions=(
                DialogAction(label="Cancel", bbox=(1100, 800, 1300, 870)),
            ),
            anchors_fired=("actions",),
        )
        f = self._fake_frame()
        with patch.object(P, "_detect_dialog_on_frame",
                          return_value=dialog_no_ok), \
             patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap, \
             patch("actions.sail_actions._find_button",
                   return_value=(700, 800)) as mock_find:
            P._dismiss_tap_ok(f)
            mock_find.assert_called_once()
            mock_tap.assert_called_once_with(700, 800)


if __name__ == "__main__":
    unittest.main()
