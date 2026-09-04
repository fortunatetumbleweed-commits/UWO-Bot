"""Sailing to a trade event via its row's location pin.

Each event row carries two buttons at its right edge (user, 2026-08-23): the scales (market
info) and the PIN. Tapping the pin sends the fleet there, and on arrival the player walks to
the market on its own — so event selling needs no world-map search, no saved route and no ETA
arithmetic. Every spice-bazaar city is within ~2 days of London.

The one thing to verify is the game's speed-0 bug: the destination can READ as set while the
tap did nothing. Movement decides — a falling ETA or a rising day-at-sea.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from actions.trade_events import find_event_pin, sail_to_event
from vision.trade_event_reader import TradeEvent

FW = 2400
FRAME = types.SimpleNamespace(width=FW, height=1080)


def _el(label, cx, cy, etype="icon", w=70, h=70):
    return types.SimpleNamespace(label=label, element_type=etype, cx=cx, cy=cy,
                                 x1=cx - w // 2, y1=cy - h // 2,
                                 x2=cx + w // 2, y2=cy + h // 2)


def _row(cy):
    """The measured pair: scales at 1797, pin at 1872."""
    return [_el("icon", 1797, cy + 22), _el("icon", 1872, cy + 24)]


BREMEN = TradeEvent(kind="Bazaar", goods="Spices", city="Bremen", row_cy=326)
CLIPPED = TradeEvent(kind="Event", goods="Spices", city="Edinburgh", row_cy=876)


class FindingTheRowsPin(unittest.TestCase):

    def test_it_takes_the_rightmost_button(self):
        """Scales first, pin second — the pin is the one that sails."""
        self.assertEqual(find_event_pin(BREMEN, _row(326), FW), (1872, 350))

    def test_a_row_with_no_buttons_on_screen_refuses(self):
        """The clipped bottom row: its buttons are below the visible area."""
        self.assertIsNone(find_event_pin(CLIPPED, _row(326), FW))

    def test_a_mangled_city_does_not_move_the_target(self):
        """Edinburgh reads as 'Fdinhuroh'. Matching on city text put the pin in the dialog's
        TITLE BAR at (1899,135) — a tap there closes the dialog."""
        elements = _row(876) + [_el("Fdinhuroh", 1664, 882, etype="text", w=117, h=36),
                                _el("icon", 1899, 135)]
        self.assertEqual(find_event_pin(CLIPPED, elements, FW), (1872, 900))

    def test_an_event_with_no_row_position_refuses(self):
        no_row = TradeEvent(kind="Bazaar", goods="Spices", city="Nowhere")
        self.assertIsNone(find_event_pin(no_row, _row(326), FW))

    def test_left_hand_icons_are_not_the_pin(self):
        """The event badge and goods icon sit far left; only the right-edge pair counts."""
        elements = _row(326) + [_el("icon", 684, 349), _el("icon", 420, 349)]
        self.assertEqual(find_event_pin(BREMEN, elements, FW), (1872, 350))


# The pin only OPENS a dialog. It raises **Location Info** — the city, a `Nearby` badge, a
# mini-map and a gold **Move** button — and nothing sails until Move is tapped (user,
# 2026-08-24). The first live attempt tapped the pin, waited for motion that could never
# start, and sat in port while the bazaar window ran out.
MOVE = (1937, 854)


def _location_info(with_move=True):
    els = [_el("Location Info", 1201, 191, etype="text", w=200, h=40),
           _el("Bremen", 1300, 400, etype="text", w=160, h=40)]
    if with_move:
        els.append(_el("Move", MOVE[0], MOVE[1], etype="button", w=180, h=70))
    return els


class SailingAndProvingIt(unittest.TestCase):

    def _sail(self, *, moving, pin_rows=None, eta=None, dialog=None):
        taps = []
        if eta is None:
            eta = 2 if moving else 5
        screens = [pin_rows or _row(326),
                   _location_info() if dialog is None else dialog]

        def _parse(_frame):
            return screens.pop(0) if len(screens) > 1 else screens[0]

        with patch("actions.sail_actions._confirm_making_way",
                   return_value=(moving, {"eta_days": eta})), \
             patch("brain.commit_actions.has_positive_background",
                   side_effect=lambda _f, e: getattr(e, "label", "") == "Move"), \
             patch("time.sleep"), \
             patch("vision.omniparser.parse_fast_cached", side_effect=_parse):
            return sail_to_event(BREMEN, capture_fn=lambda: FRAME,
                                 tap_fn=lambda x, y: taps.append((x, y))), taps

    def test_it_taps_move_on_the_dialog_the_pin_opened(self):
        """The pin alone never sails — Move is what commits the voyage."""
        res, taps = self._sail(moving=True)
        self.assertTrue(res["ok"])
        self.assertEqual(taps, [(1872, 350), MOVE])

    def test_a_dialog_without_move_is_a_failure(self):
        """No Move button means nothing was committed; do not report a voyage."""
        res, taps = self._sail(moving=True, dialog=_location_info(with_move=False))
        self.assertFalse(res["ok"])
        self.assertEqual(taps, [(1872, 350)], "it tapped the pin and stopped there")

    def test_a_short_hop_is_confirmed_by_arrival_not_by_a_falling_eta(self):
        """A 1-day ETA has no finer granularity to fall through, and the whole voyage is over
        in ~90s. Live 2026-08-24 the fleet reached Bremen while the movement test was still
        calling `eta=None->1` the speed-0 bug, and the run aborted on a success."""
        res, _taps = self._sail(moving=False, eta=1)
        self.assertTrue(res["ok"])
        self.assertTrue(res["short_hop"])

    def test_a_long_voyage_that_never_moves_is_still_the_speed_0_bug(self):
        """The short-hop allowance must not swallow a real swallowed tap."""
        res, _taps = self._sail(moving=False, eta=9)
        self.assertFalse(res["ok"])

    def test_it_taps_the_pin_and_reports_under_way(self):
        res, taps = self._sail(moving=True)
        self.assertTrue(res["ok"])
        self.assertEqual(taps, [(1872, 350), MOVE])
        self.assertEqual(res["eta_days"], 2)

    def test_a_fleet_that_never_moves_is_a_failure(self):
        """The speed-0 bug: the destination shows as set while the fleet sits still."""
        res, taps = self._sail(moving=False)
        self.assertFalse(res["ok"])
        self.assertFalse(res["moving"])
        self.assertEqual(taps, [(1872, 350), MOVE],
                         "it did tap pin and Move — the taps were swallowed")

    def test_no_pin_means_no_tap_at_all(self):
        taps = []
        with patch("vision.omniparser.parse_fast_cached", return_value=[]):
            res = sail_to_event(BREMEN, capture_fn=lambda: FRAME,
                                tap_fn=lambda x, y: taps.append((x, y)))
        self.assertFalse(res["ok"])
        self.assertEqual(taps, [], "never tap a guessed position")


if __name__ == "__main__":
    unittest.main()
