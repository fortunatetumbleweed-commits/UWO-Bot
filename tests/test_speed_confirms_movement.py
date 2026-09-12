"""Speed is the direct answer to "is the fleet moving?".

The game prints the fleet's speed in knots in the tile-strip left of the mini-map, so the
question does not have to be inferred from two HUD readings a game-day apart.  Two live
failures motivate this:

  * `read_sea_hud` never carried a `speed` key, so `_confirm_making_way`'s
    `before.get("speed")` was always None and was logged as `speed=None→None` as though it
    were evidence;
  * `read_speed`'s crop is derived as an offset from the `MINIMAP_CROP` constant, and the UI
    drifts — measured 2026-08-24, the mini-map's real left edge was x≈1862 against a constant
    saying 1984, so the crop landed INSIDE the mini-map disc and returned None on every
    at-sea frame.

The band is located from the mini-map's actual position instead, and is deliberately
generous: the speed is the only DECIMAL in that strip (wind and current are integers), so
`_SPEED_RE` disambiguates it without a tight box.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from vision.sea_hud import locate_minimap_bbox, locate_speed_band


def _el(cx, cy, x1, y1, x2, y2, etype="button"):
    return types.SimpleNamespace(element_type=etype, cx=cx, cy=cy,
                                 x1=x1, y1=y1, x2=x2, y2=y2)


# The mini-map as OmniParser actually reported it on an auto-sail frame.
MINIMAP = _el(2060, 300, 1862, 205, 2260, 400)


def _p(label, x1, y1, x2, y2, kind="icon"):
    return types.SimpleNamespace(label=label, element_type=kind, x1=x1, y1=y1, x2=x2, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


# The right panel at sea, from frame 0588 of trace_barter_cmd_2026-09-11T13-47-33: the gauge
# strip, the tab strip, the mini-map, the season row that anchors the lot, and one list row.
_AT_SEA = [
    _p("icon", 1778, 129, 1865, 200), _p("27.6", 1780, 197, 1864, 277, "button"),
    _p("icon", 1781, 274, 1864, 356), _p("14", 1780, 354, 1865, 435, "button"),
    _p("icon", 1864, 129, 1969, 200), _p("10", 1968, 132, 2069, 198, "button"),
    _p("icon", 2064, 134, 2160, 197), _p("icon", 2159, 133, 2265, 200),
    _p("26.04,-14,89", 1862, 201, 2267, 430, "button"),
    _p("Aug", 2072, 399, 2132, 438, "text"), _p("Night", 2169, 401, 2245, 437, "text"),
    _p("Summer", 1944, 402, 2050, 430, "text"),
    _p("Berber Village", 1864, 435, 2266, 540, "button"),
]


class LocatingTheSpeedTile(unittest.TestCase):

    def test_it_finds_the_tall_top_right_button(self):
        self.assertEqual(locate_minimap_bbox([MINIMAP]), (1862, 205, 2260, 400))

    def test_short_or_left_hand_buttons_are_not_the_minimap(self):
        """Height and quadrant both matter — the strip's own tiles are short."""
        decoys = [_el(1900, 260, 1780, 235, 1860, 285),      # the speed tile itself: short
                  _el(400, 300, 300, 200, 700, 400)]          # tall, but far left
        self.assertIsNone(locate_minimap_bbox(decoys))

    def test_the_tile_comes_from_the_PANEL_and_sits_left_of_the_minimap(self):
        """The gauge strip is part of the overworld right panel, so it is found with it
        (user, 2026-09-11) — no longer four offsets from the mini-map.

        It used to be computed as 230px left of the map, 180 tall, which gave a 225px band
        over open sea for an 84px tile. That read correctly, because the speed is the only
        decimal in the strip; its FALLBACK did not, landing inside the map disc.
        """
        band = locate_speed_band(None, _AT_SEA)
        self.assertIsNotNone(band)
        x1, _y1, x2, _y2 = band
        self.assertLessEqual(x2, MINIMAP.x1 + 4, "it must not overlap the mini-map disc")
        self.assertLessEqual(x1, 1780, "it must reach the speed tile at x~1780")

    def test_every_gauge_cell_is_offered_not_just_one(self):
        """WHICH cell holds the speed is decided by what is IN it — the only decimal in the
        strip. Trusting its index would be a calibrated coordinate in another hat."""
        from vision.sea_hud import speed_candidate_boxes
        self.assertEqual(len(speed_candidate_boxes(None, _AT_SEA)), 4)

    def test_no_panel_means_no_guess(self):
        """A wrong box reads somebody else's pixels; None is the honest answer. The strip and
        the panel are drawn together, so no panel means there is no speed on screen at all."""
        self.assertIsNone(locate_speed_band(None, []))
        self.assertIsNone(locate_speed_band(None, [MINIMAP]))


class ConfirmingDeparture(unittest.TestCase):
    """`_confirm_making_way` prefers speed, falls back to the ETA test, and never lets an
    unreadable speed fail a departure on its own."""

    def _run(self, *, speeds, hud_before, hud_after, moving_fn=lambda a, b: False):
        from actions import sail_actions
        seq = list(speeds)
        huds = [hud_before, hud_after]
        with patch.object(sail_actions, "read_sea_hud", side_effect=lambda *_a, **_k: huds.pop(0)), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("vision.sea_hud.read_speed", side_effect=lambda *_a, **_k: seq.pop(0)), \
             patch("time.sleep"):
            return sail_actions._confirm_making_way(45.0, moving_fn)

    def test_a_positive_speed_confirms_immediately(self):
        """No need to wait a game-day when the game says 27.5 kt."""
        moving, _hud = self._run(speeds=[27.5, 27.5],
                                 hud_before={"eta_days": 1}, hud_after={"eta_days": 1})
        self.assertTrue(moving)

    def test_zero_speed_twice_is_the_speed_0_bug(self):
        moving, hud = self._run(speeds=[0.0, 0.0],
                                hud_before={"eta_days": 1}, hud_after={"eta_days": 1})
        self.assertFalse(moving)
        self.assertEqual(hud["speed"], 0.0, "the measured 0.0 must reach the caller")

    def test_speed_rising_from_zero_still_confirms(self):
        """Right after the tap the ship may still be accelerating — 0.0 then is not a verdict."""
        moving, _hud = self._run(speeds=[0.0, 11.5],
                                 hud_before={"eta_days": 3}, hud_after={"eta_days": 3})
        self.assertTrue(moving)

    def test_an_unreadable_speed_falls_through_to_the_eta_test(self):
        """None means "cannot tell", and must not fail a departure that IS progressing."""
        moving, hud = self._run(speeds=[None, None],
                                hud_before={"eta_days": 3}, hud_after={"eta_days": 2},
                                moving_fn=lambda a, b: a["eta_days"] > b["eta_days"])
        self.assertTrue(moving)
        self.assertIsNone(hud["speed"])


if __name__ == "__main__":
    unittest.main()
