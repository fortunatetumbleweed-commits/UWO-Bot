"""The rail cannot be detected on the one screen where the list must be opened.

Measured 2026-09-01 across three frames. OmniParser reports the rail's icons only when a LIST
IS ALREADY OPEN behind them — the dark panel is what gives the small glyphs contrast:

    frame_0014, list open behind    icon (70,184) (70,301) (71,536)     found
    frame_0026, bare map behind     nothing at that x — only 'Far Sev', 'asablanca'
    live frame, bare map behind     nothing at that x — only 'Faro', 'Sev', 'asablanca'

Not a misread: an ABSENCE. So `_explore_left_icons` returning [] is correct — it refuses to
offer a port pin, which is what tapped FARO and started the whole cascade — but refusing was
not enough on its own. Live 2026-09-01 the run declined on every tick,

    [world_map] 'Barcelona' is not on screen — opening the list
    [world_map] no icon rail detected — not tapping a remembered point

and the leg stalled having done nothing.

The rail is FIXED CHROME, so where it sits is knowable, and CLAUDE.md's exception is written
for exactly this: "`ui.tap_at(x, y, why=…)` is the loud, logged exception for genuinely
calibrated HUD controls". What makes it safe is that the tap is JUDGED — the next tick asks
whether a list opened, the same question it asks of a detected icon.

This is not a retreat from `a-fallback-fires-when-guessing-is-worst`. There, a working lookup
existed and the constant was its stale shadow, firing only when the lookup failed. Here the
lookup provably cannot see the control on this screen, so there is no better answer being
displaced — and a map pin still cannot become a candidate, because the column-and-clip test
rejects it before any of this.
"""
import types
import unittest
from unittest import mock

from actions.sail_actions import _RAIL_FALLBACK_POINTS, _VILLAGE_LIST_ICON_INDEX


class ThePointsAreWhereTheRailWasMeasured(unittest.TestCase):
    def test_the_port_list_icon(self):
        x, y = _RAIL_FALLBACK_POINTS[0]
        # frame_0014 read it at (70,184); the tap that opened it live was (68,176).
        self.assertTrue(60 <= x <= 80, x)
        self.assertTrue(165 <= y <= 195, y)

    def test_the_village_list_icon_is_the_second(self):
        self.assertGreater(len(_RAIL_FALLBACK_POINTS), _VILLAGE_LIST_ICON_INDEX)
        x, y = _RAIL_FALLBACK_POINTS[_VILLAGE_LIST_ICON_INDEX]
        self.assertTrue(60 <= x <= 80, x)      # frame_0014 read it at (70,301)
        self.assertTrue(285 <= y <= 320, y)

    def test_they_are_a_column(self):
        xs = [x for x, _y in _RAIL_FALLBACK_POINTS]
        self.assertLessEqual(max(xs) - min(xs), 12, "the rail is drawn at one x")


def _activity(icons):
    from brain.activities.world_map import WorldMapActivity
    taps = []
    a = WorldMapActivity(tap_fn=lambda x, y: taps.append((x, y)),
                         capture_fn=lambda: object())
    return a, taps, mock.patch("actions.sail_actions._explore_left_icons",
                               return_value=icons)


class ADetectedRailIsAlwaysPreferred(unittest.TestCase):
    def test_the_detected_icon_is_tapped_not_the_constant(self):
        a, taps, patched = _activity([(70, 184), (70, 301)])
        with patched:
            a._open_list(types.SimpleNamespace(kind="port"))
        self.assertEqual(taps, [(70, 184)])
        self.assertNotIn(_RAIL_FALLBACK_POINTS[0], taps)


class AnUndetectableRailIsStillReachable(unittest.TestCase):
    def test_the_calibrated_point_is_used_when_nothing_is_detected(self):
        a, taps, patched = _activity([])
        with patched:
            a._open_list(types.SimpleNamespace(kind="port"))
        self.assertEqual(taps, [_RAIL_FALLBACK_POINTS[0]],
                         "declining every tick is what stalled the leg")

    def test_a_village_goal_reaches_for_the_second_icon(self):
        a, taps, patched = _activity([])
        with patched:
            a._open_list(types.SimpleNamespace(kind="village"))
        self.assertEqual(taps, [_RAIL_FALLBACK_POINTS[_VILLAGE_LIST_ICON_INDEX]])

    def test_a_failed_attempt_moves_to_the_next_candidate(self):
        """The next tick judges: still no list, so try the other point."""
        a, taps, patched = _activity([])
        with patched:
            a._open_list(types.SimpleNamespace(kind="port"))
            a._open_list(types.SimpleNamespace(kind="port"))
        self.assertEqual(len(taps), 2)
        self.assertNotEqual(taps[0], taps[1], "repeating the same point learns nothing")


if __name__ == "__main__":
    unittest.main()
