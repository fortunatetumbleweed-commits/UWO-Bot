"""Bbox-scoped dismissal — tap_ok_or_x must only search inside the
obstruction frame, not the whole screen.

Origin: 2026-05-15.  The bot got stuck in a 5-minute interruptor
loop in front of Fortune Teller because:
  1.  Layer A reported a phantom popup with bbox (1076, 197)–(2392, 1066)
  2.  Legacy keyword interruptor matched the building nameplate text
  3.  _dismiss_tap_ok_or_x searched the WHOLE frame for an X / OK
      button, found none, and blindly tapped (2208, 86) — top-right
      chrome icon row
  4.  That tap did nothing useful; next perceive saw the same scene;
      loop

Tests pin the contract:
  - Button INSIDE the supplied bbox → tapped
  - No matching button inside bbox → NO TAP fires
  - The blind (top-right corner) fallback is GONE
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

import brain.perceive as bp


def _frame():
    return Image.new("RGB", (2400, 1080), color=(0, 0, 0))


class BboxScopedDismissTests(unittest.TestCase):

    def setUp(self):
        self.tap_calls = []

        def fake_tap(x, y):
            self.tap_calls.append((x, y))

        self._patches = [
            patch("actions.adb_actions.tap", side_effect=fake_tap),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_button_inside_bbox_gets_tapped(self):
        with patch(
            "actions.sail_actions._find_button",
            return_value=(1200, 500),
        ) as fake_find:
            bp._dismiss_tap_ok_or_x(
                _frame(), obstruction_bbox=(800, 300, 1600, 700),
            )
        # _find_button called with the bbox limits
        self.assertTrue(fake_find.called)
        first_call_kwargs = fake_find.call_args_list[0].kwargs
        self.assertEqual(first_call_kwargs.get("x_min"), 800)
        self.assertEqual(first_call_kwargs.get("x_max"), 1600)
        self.assertEqual(first_call_kwargs.get("y_min"), 300)
        self.assertEqual(first_call_kwargs.get("y_max"), 700)
        # The found button was tapped
        self.assertEqual(self.tap_calls, [(1200, 500)])

    def test_no_button_inside_bbox_skips_tap(self):
        """The key regression: when the modal has no X/OK button (e.g.
        a building nameplate misclassified as a popup), the dismissal
        must NOT blind-tap somewhere on the chrome.  Skip and let the
        no-op counter retire the bogus interruptor."""
        with patch(
            "actions.sail_actions._find_button",
            return_value=None,
        ):
            bp._dismiss_tap_ok_or_x(
                _frame(), obstruction_bbox=(800, 300, 1600, 700),
            )
        # NO tap fired — that's the fix
        self.assertEqual(self.tap_calls, [],
                         "blind fallback tap must be gone")

    def test_no_bbox_provided_still_skips_when_no_button(self):
        """Back-compat path: even without a bbox, the blind fallback
        is gone.  When _find_button returns None, no tap fires."""
        with patch(
            "actions.sail_actions._find_button",
            return_value=None,
        ):
            bp._dismiss_tap_ok_or_x(_frame())
        self.assertEqual(self.tap_calls, [])

    def test_no_bbox_with_found_button_taps_it(self):
        with patch(
            "actions.sail_actions._find_button",
            return_value=(1500, 400),
        ):
            bp._dismiss_tap_ok_or_x(_frame())
        self.assertEqual(self.tap_calls, [(1500, 400)])


if __name__ == "__main__":
    unittest.main()
