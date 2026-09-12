"""The navigation loop calibrates the gauge once; it does not locate it every tick.

THE RULE (user, 2026-09-12): manual navigation is at sea for the whole voyage, assumes NO
interruptor, and reads only the STEERING, the SPEED and the MINI-MAP. Its tick budget is under
3s, and that is not a preference — the ship keeps moving between decisions, so the interval is
how far it travels blind.

WHAT THIS GUARDS. `read_speed` was changed on 2026-09-12 to locate the gauge through the
overworld panel rather than four offsets against `MINIMAP_CROP`. That was right for a BUSINESS
run, whose old fallback landed inside the mini-map disc. It was wrong for navigation, which
calls it twice a tick: a whole-frame OmniParser parse costs ~2.6s measured, so the tick went
from under 3s to 6s. Nothing failed — it just got slower, and the Jeddah voyage took two
collisions on the tight stretches where that margin ran out.

Measured after the fix, on the same frame: 2,736 ms uncalibrated, 39 ms calibrated, same
reading.

See docs/navigation_is_not_a_business_run.md.
"""
from __future__ import annotations

import unittest
from unittest import mock

import vision.sea_hud as hud


class TheGaugeIsLocatedOnce(unittest.TestCase):

    def setUp(self):
        self._saved = hud.get_speed_boxes()
        hud._SPEED_BOXES = ()

    def tearDown(self):
        hud._SPEED_BOXES = self._saved

    def test_calibrating_remembers_the_cells(self):
        cells = [(1898, 128, 1982, 198), (1898, 196, 1981, 277)]
        with mock.patch.object(hud, "speed_candidate_boxes", return_value=cells):
            got = hud.calibrate_speed_gauge(object())
        self.assertEqual(list(got), cells)
        self.assertEqual(list(hud.get_speed_boxes()), cells)

    def test_A_CALIBRATED_READ_NEVER_LOCATES_THE_PANEL(self):
        """The whole point: no parse, no 2.6s, on any tick after the first."""
        hud._SPEED_BOXES = ((1898, 196, 1981, 277),)
        with mock.patch.object(hud, "speed_candidate_boxes") as locate, \
             mock.patch("actions.water_tap._get_reader") as reader:
            reader.return_value.readtext.return_value = []
            hud.read_speed(_Frame())
        locate.assert_not_called()

    def test_an_UNCALIBRATED_read_still_locates(self):
        """A business run does not calibrate — it meets a new screen every few seconds and
        must find the panel on the frame in front of it. That path stays."""
        with mock.patch.object(hud, "speed_candidate_boxes", return_value=[]) as locate, \
             mock.patch("actions.water_tap._get_reader") as reader:
            reader.return_value.readtext.return_value = []
            hud.read_speed(_Frame())
        locate.assert_called_once()

    def test_calibration_is_MEASURED_not_hardcoded(self):
        """Measured once is not the same as remembered. The old reader offset a crop from
        MINIMAP_CROP and, when the UI drifted, read the inside of the map disc. Calibration
        re-measures at startup, so it follows the drift — it just does not re-measure every
        tick. Read off the source so the distinction cannot quietly be lost."""
        import inspect

        src = inspect.getsource(hud.calibrate_speed_gauge)
        self.assertIn("speed_candidate_boxes", src,
                      "calibration must MEASURE the panel, not assume a box")


class TheNavigationLoopCalibratesAtStartup(unittest.TestCase):

    def test_the_runner_calibrates_the_gauge(self):
        import inspect
        import tools.run_ai_nav_live as nav

        src = inspect.getsource(nav)
        self.assertIn("calibrate_speed_gauge", src)
        # …alongside the two it has always calibrated.
        self.assertIn("calibrate_minimap_crop", src)

    def test_the_loop_does_not_perceive_per_tick(self):
        """`perceive()` answers 'what screen is this, is something in the way'. Navigation
        knows both. It warms up once — measured across 134 steering holds on the Jeddah
        voyage, perceive ran twice, both at startup."""
        import inspect
        import tools.run_ai_nav_live as nav

        # COUNT CALLS, NOT TEXT. `perceive()` also appears in a comment and a log message
        # here, and an assertion that cannot tell those apart fails for the wrong reason.
        calls = [l for l in inspect.getsource(nav).splitlines()
                 if "perceive()" in l
                 and not l.lstrip().startswith("#")
                 and '"' not in l and "'" not in l]
        self.assertEqual(len(calls), 1, f"perceive() belongs in the warm-up only: {calls}")


class _Frame:
    """Minimal stand-in: `read_speed` crops it and hands the array to OCR."""
    width, height = 2400, 1080

    def crop(self, box):
        return self

    def convert(self, mode):
        return self

    def __array__(self, dtype=None):
        import numpy as np
        return np.zeros((80, 84, 3), dtype=dtype or np.uint8)


if __name__ == "__main__":
    unittest.main()
