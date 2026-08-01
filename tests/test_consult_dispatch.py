"""Tests for the Claude-consult dismissal dispatch.

Regression: in the 2026-05-19 live run, an accidental tap on another
player brought up a Company Info dialog.  Claude was consulted, returned
dismissal='tap_close_x', and the result was cached.  But the dispatch
in _dismiss_interruptor only handled tap_decline — every subsequent
visit cache-hit with no action, the dialog stayed on screen for ~4 min.

This test suite covers the now-extended dispatch for all consult
methods Claude can return.
"""
import unittest
from unittest.mock import patch, MagicMock


def _fake_frame(w=2400, h=1080):
    f = MagicMock(); f.width = w; f.height = h; f.size = (w, h)
    return f


class ConsultDispatchTests(unittest.TestCase):

    def test_tap_close_x_routes_to_dismiss_close_button(self):
        from brain import perceive as P
        with patch.object(P, "_dismiss_close_button") as mock_close, \
             patch.object(P, "_dismiss_tap_decline"), \
             patch.object(P, "_dismiss_tap_ok"):
            P._dismiss_interruptor("_consult:tap_close_x", _fake_frame())
            mock_close.assert_called_once()
            args, kwargs = mock_close.call_args
            # Should be called with (frame, iid, position=None)
            self.assertIs(args[1], "_consult:tap_close_x")
            self.assertIsNone(kwargs.get("position", args[2] if len(args) > 2 else 1))

    def test_tap_decline_still_routes_to_dismiss_tap_decline(self):
        """Backward-compat: original tap_decline wiring must still work."""
        from brain import perceive as P
        with patch.object(P, "_dismiss_tap_decline") as mock_decline:
            P._dismiss_interruptor(
                "_consult:tap_decline", _fake_frame(), obstruction_bbox=(0,0,100,100)
            )
            mock_decline.assert_called_once()

    def test_tap_anywhere_taps_centre(self):
        from brain import perceive as P
        with patch("brain.perceive.time.sleep"), \
             patch("actions.adb_actions.tap") as mock_tap:
            P._dismiss_interruptor("_consult:tap_anywhere", _fake_frame())
            mock_tap.assert_called_once_with(1200, 540)   # centre of 2400×1080

    def test_tap_ok_routes_to_dismiss_tap_ok(self):
        from brain import perceive as P
        with patch.object(P, "_dismiss_tap_ok") as mock_ok:
            P._dismiss_interruptor("_consult:tap_ok", _fake_frame())
            mock_ok.assert_called_once()

    def test_tap_accept_routes_to_dismiss_tap_ok(self):
        """tap_accept is shaped like tap_ok — same dispatch."""
        from brain import perceive as P
        with patch.object(P, "_dismiss_tap_ok") as mock_ok:
            P._dismiss_interruptor("_consult:tap_accept", _fake_frame())
            mock_ok.assert_called_once()

    def test_press_back_calls_press_back(self):
        from brain import perceive as P
        with patch("brain.perceive.time.sleep"), \
             patch("actions.sail_actions.press_back") as mock_back:
            P._dismiss_interruptor("_consult:press_back", _fake_frame())
            mock_back.assert_called_once()

    def test_unknown_consult_method_warns_and_returns(self):
        """Unknown method must NOT crash; just logs warning."""
        from brain import perceive as P
        with patch("brain.perceive.logger.warning") as mock_warn:
            # Should not raise
            P._dismiss_interruptor("_consult:nonsense", _fake_frame())
            mock_warn.assert_called_once()
            # The warning message should mention the method
            args = mock_warn.call_args.args[0]
            self.assertIn("nonsense", args)


if __name__ == "__main__":
    unittest.main()
