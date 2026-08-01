"""Phase 3.1 unit tests for _dialog_ok_pos / _resolve_dialog_ok in market_actions."""
import unittest
from unittest.mock import patch, MagicMock

from vision.region_detectors.dialog import DialogModel, DialogAction


def _fake_frame(w=2400, h=1080):
    f = MagicMock(); f.width = w; f.height = h; f.size = (w, h)
    return f


class DialogOkPosTests(unittest.TestCase):

    def test_returns_centre_of_ok_action_bbox(self):
        from actions import market_actions as M
        dialog = DialogModel(
            bbox=(800, 400, 1600, 900),
            actions=(DialogAction(label="OK", bbox=(1100, 800, 1300, 870),
                                  is_positive=True),),
            anchors_fired=("actions",),
        )
        with patch("vision.omniparser.get_omniparser") as mock_parser_factory, \
             patch("vision.region_detectors.dialog.detect_dialog",
                   return_value=dialog):
            mock_parser_factory.return_value.parse_fast.return_value = []
            pos = M._dialog_ok_pos(_fake_frame())
            self.assertEqual(pos, (1200, 835))

    def test_returns_none_when_no_dialog(self):
        from actions import market_actions as M
        with patch("vision.omniparser.get_omniparser") as mock_parser_factory, \
             patch("vision.region_detectors.dialog.detect_dialog",
                   return_value=None):
            mock_parser_factory.return_value.parse_fast.return_value = []
            self.assertIsNone(M._dialog_ok_pos(_fake_frame()))

    def test_returns_none_when_no_ok_action_in_dialog(self):
        """Dialog with only Cancel — _dialog_ok_pos should return None."""
        from actions import market_actions as M
        dialog = DialogModel(
            bbox=(800, 400, 1600, 900),
            actions=(DialogAction(label="Cancel", bbox=(1100, 800, 1300, 870)),),
            anchors_fired=("actions",),
        )
        with patch("vision.omniparser.get_omniparser") as mock_parser_factory, \
             patch("vision.region_detectors.dialog.detect_dialog",
                   return_value=dialog):
            mock_parser_factory.return_value.parse_fast.return_value = []
            self.assertIsNone(M._dialog_ok_pos(_fake_frame()))

    def test_returns_none_on_exception(self):
        """A detector exception must NOT bubble up — caller falls back."""
        from actions import market_actions as M
        with patch("vision.omniparser.get_omniparser",
                   side_effect=RuntimeError("boom")):
            self.assertIsNone(M._dialog_ok_pos(_fake_frame()))


class ResolveDialogOkTests(unittest.TestCase):

    def test_uses_dialog_when_typed_path_fires(self):
        from actions import market_actions as M
        from brain import dismissal_telemetry as T
        T.reset()
        with patch.object(M, "_dialog_ok_pos", return_value=(1200, 835)), \
             patch.object(M, "_flow_coords", return_value=(999, 999)) as legacy:
            pos = M._resolve_dialog_ok(_fake_frame(), {}, "any_fallback",
                                       handler="market_test")
            self.assertEqual(pos, (1200, 835))
            legacy.assert_not_called()
            self.assertEqual(T.snapshot()["market_test"]["typed"], 1)

    def test_falls_back_to_flow_coords_when_no_dialog(self):
        from actions import market_actions as M
        from brain import dismissal_telemetry as T
        T.reset()
        with patch.object(M, "_dialog_ok_pos", return_value=None), \
             patch.object(M, "_flow_coords", return_value=(999, 999)) as legacy:
            pos = M._resolve_dialog_ok(_fake_frame(), {}, "purchase_result_dialog",
                                       handler="market_test")
            self.assertEqual(pos, (999, 999))
            legacy.assert_called_once()
            self.assertEqual(T.snapshot()["market_test"]["legacy"], 1)


if __name__ == "__main__":
    unittest.main()
