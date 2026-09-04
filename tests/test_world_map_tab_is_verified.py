"""Switching a world-map tab must be confirmed by EFFECT, not assumed from the tap.

`select_world_map_tab` used to tap and return True, reasoning that tapping an already-active
tab is a harmless no-op. But a tap that is never heard looks identical to a harmless one.
Live 2026-08-25: the tap landed inside the Explore tab at (1047,55), the strip stayed on
PORT, and the caller then hunted the PORT tab's icons for a village list — opening the
trade-goods filter instead, giving up, and panning the map to find a village in plain view.

The trace also shows the SAME coordinate (1046,51) working later in the run, so the fix is to
wait and repeat, not to tap somewhere else.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

def _frame():
    """A real frame: the strip masks the tab band by brightness before parsing."""
    from PIL import Image
    return Image.new("RGB", (2400, 1080))


from actions import sail_actions


def _tab_els(names, y=51):
    return [types.SimpleNamespace(label=n, cx=800 + 200 * i, cy=y, y1=y - 20, y2=y + 20,
                                  x1=760 + 200 * i, x2=840 + 200 * i, element_type="button")
            for i, n in enumerate(names)]


class TheTabMustActuallyChange(unittest.TestCase):

    def _run(self, lit_sequence):
        """lit_sequence: index of the lit tab each time the strip is inspected."""
        taps, seen = [], iter(lit_sequence)
        parser = mock.Mock()
        parser.yolo_available.return_value = True
        parser.parse_fast.return_value = _tab_els(list(sail_actions._WORLD_MAP_TABS))

        with mock.patch.object(sail_actions, "get_omniparser", lambda: parser, create=True), \
             mock.patch("vision.omniparser.get_omniparser", lambda: parser), \
             mock.patch("capture.adb_capture.capture_screen", _frame), \
             mock.patch("actions.adb_actions.tap", lambda x, y: taps.append((x, y))), \
             mock.patch.object(sail_actions, "selected_tab_index",
                               lambda *_a, **_k: next(seen, None)), \
             mock.patch("time.sleep", lambda _s: None):
            ok = sail_actions.select_world_map_tab("explore")
        return ok, taps

    def test_a_swallowed_tap_is_not_success(self):
        """Every inspection says PORT is lit — the switch never happened."""
        ok, taps = self._run([0, 0, 0, 0, 0, 0, 0])
        self.assertFalse(ok, "reported success for a tab that never changed")
        self.assertGreater(len(taps), 1, "it should have retried")

    def test_a_retry_uses_the_same_coordinate(self):
        """The trace shows the same point working later — waiting is the fix, not moving."""
        _ok, taps = self._run([0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(len(set(taps)), 1, f"the retry moved the tap: {taps}")

    def test_it_succeeds_once_the_tab_is_lit(self):
        # before-tap: PORT lit; after-tap: EXPLORE lit
        ok, taps = self._run([0, 1])
        self.assertTrue(ok)
        self.assertEqual(len(taps), 1)

    def test_an_already_selected_tab_needs_no_tap(self):
        ok, taps = self._run([1])
        self.assertTrue(ok)
        self.assertEqual(taps, [], "tapping a tab that is already lit is wasted input")
