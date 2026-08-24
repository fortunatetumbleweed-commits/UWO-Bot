"""Finding the ☰ must be about IDENTITY, never "whatever is furthest right".

Live 2026-08-21, from the captured session trace_barter_cmd_2026-08-21T17-50-17: the old
`find_top_right_icon` took the rightmost icon in the top band with no check of what it
was. OmniParser had not emitted the ☰, so it tapped CHAT at x=2119 (opening the chat
window), and on retry tapped MAIL at x=2023 — while logging "the ☰ was detected" both
times. actions.jsonl proves the hamburger was never tapped at all.

The strip is the ruler: the top-right chrome is an evenly spaced row and the ☰ is its last
member, so the neighbours' pitch predicts it even when it is not detected itself."""
import types
import unittest
from unittest import mock

from actions.ui import _menu_icon_from_strip


def _icon(cx, cy):
    return types.SimpleNamespace(element_type="icon", label="icon", cx=cx, cy=cy,
                                 x1=cx - 20, y1=cy - 20, x2=cx + 20, y2=cy + 20)


class StripInferenceTests(unittest.TestCase):
    def _run(self, icons):
        frame = types.SimpleNamespace(width=2400, height=1080)
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=icons):
            return _menu_icon_from_strip(frame)

    def test_the_real_failing_frame_yields_the_true_position(self):
        # The icons OmniParser actually returned on frame_0010; true ☰ sits at ~2213.
        el = self._run([_icon(1922, 56), _icon(2024, 69), _icon(2118, 48), _icon(2119, 63)])
        self.assertIsNotNone(el)
        self.assertAlmostEqual(el.cx, 2213, delta=15)

    def test_it_refuses_when_the_strip_is_too_sparse(self):
        # frame_0011: the chat panel covers most of the row. Guessing here is what
        # tapped mail; None lets the caller report instead.
        self.assertIsNone(self._run([_icon(2023, 69), _icon(2119, 64)]))

    def test_it_refuses_an_implausible_pitch(self):
        # Scattered icons that are not a strip must not be measured as one.
        self.assertIsNone(self._run([_icon(1700, 40), _icon(1710, 900), _icon(2350, 30)]))

    def test_it_does_not_run_off_the_frame(self):
        # If the rightmost detected icon IS the ☰, stepping again would leave the screen.
        el = self._run([_icon(2020, 50), _icon(2120, 50), _icon(2220, 50), _icon(2330, 50)])
        self.assertIsNotNone(el)
        self.assertLessEqual(el.cx, 2380)

    def test_it_never_returns_the_chat_icon_position(self):
        # The specific regression: chat at 2119 must not be handed back as the menu.
        el = self._run([_icon(1922, 56), _icon(2024, 69), _icon(2118, 48)])
        self.assertIsNotNone(el)
        self.assertGreater(el.cx, 2160, "returned a position in the chat/mail range")


if __name__ == "__main__":
    unittest.main()
