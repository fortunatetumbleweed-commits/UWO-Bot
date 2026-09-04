"""Reading the world map's Trade Event Schedule.

Mayors schedule market events; during a Bazaar the named category fetches a much higher price
at that city, so the schedule is a selling plan. Measured on the live dialog (2026-08-23):

    [Bazaar] Spices    2026/08/24 13:00 ~ 14:00   Bremen     Tax 15%
    [Bazaar] Jewelry   2026/08/24 15:00 ~ 16:00   Bergen     Tax  5%
    [Bazaar] Spices    2026/08/25 13:00 ~ 14:00   Cologne    Tax 15%
    [Bazaar] Sundries  2026/08/26 00:01 ~ 01:01   Massawa    Tax  5%
    [Bazaar] Weapons   2026/08/26 12:01 ~ 13:01   Shiraz     Tax  5%
    [ ...  ] Spices    2026/08/26 ...             Edinburgh          <- clipped, list scrolls

Times are KOREAN time (UTC+9), not the player's local time.
"""

from __future__ import annotations

import types
import unittest
from datetime import datetime, timezone

from vision.trade_event_reader import KST, TradeEvent, read_trade_events

FW, FH = 2400, 1080


def _el(label, cx, cy, etype="text", w=140, h=34):
    return types.SimpleNamespace(label=label, element_type=etype, cx=cx, cy=cy,
                                 x1=cx - w // 2, y1=cy - h // 2,
                                 x2=cx + w // 2, y2=cy + h // 2)


def _dialog(rows=None, extras=()):
    """The measured column layout, plus whatever rows are asked for."""
    els = [_el("Trade Event Schedule", 1198, 132),
           _el("Market Event", 553, 266), _el("Trade Goods", 857, 268),
           _el("Fixed-term", 1269, 268), _el("Location", 1689, 268)]
    for i, (kind, goods, d1, t1, d2, t2, city, tax) in enumerate(rows or []):
        y = 326 + i * 110
        if kind:
            els.append(_el(kind, 553, y + 23, etype="button", w=155, h=101))
        els.append(_el(goods, 786, y, etype="button", w=98, h=40))
        els.append(_el(d1, 1151, y + 5))
        els.append(_el(d2, 1322, y + 5))
        els.append(_el(t1, 1151, y + 43, w=70))
        els.append(_el(t2, 1322, y + 43, w=72))
        els.append(_el(city, 1664, y + 6, w=117))
        els.append(_el("Tax", 1602, y + 45, w=52))
        els.append(_el(f"{tax}%", 1670, y + 45, w=60))
    els.extend(extras)
    return els


BREMEN = ("Bazaar", "Spices", "2026/08/24", "13.00", "2026/08/24", "14.00", "Bremen", 15)
BERGEN = ("Bazaar", "Jewelry", "2026/08/24", "15.00", "2026/08/24", "16.00", "Bergen", 5)


class ReadingTheSchedule(unittest.TestCase):

    def test_it_reads_a_row(self):
        (ev,) = read_trade_events(None, elements=_dialog([BREMEN]))
        self.assertEqual((ev.kind, ev.goods, ev.city, ev.tax_pct),
                         ("Bazaar", "Spices", "Bremen", 15))

    def test_times_are_korean_time(self):
        """UTC+9 — not the player's local clock, and not the in-world clock."""
        (ev,) = read_trade_events(None, elements=_dialog([BREMEN]))
        self.assertEqual(ev.start, datetime(2026, 8, 24, 13, 0, tzinfo=KST))
        self.assertEqual(ev.end, datetime(2026, 8, 24, 14, 0, tzinfo=KST))

    def test_several_rows_are_read(self):
        evs = read_trade_events(None, elements=_dialog([BREMEN, BERGEN]))
        self.assertEqual([e.city for e in evs], ["Bremen", "Bergen"])

    def test_a_row_without_its_badge_is_still_an_event(self):
        """OmniParser found only 4 of 6 'Bazaar' badges — anchoring on them dropped two
        events, including a Spices bazaar. The goods column is the reliable anchor."""
        no_badge = ("", "Spices", "2026/08/26", "13.00", "2026/08/26", "14.00", "Cologne", 15)
        evs = read_trade_events(None, elements=_dialog([BREMEN, no_badge]))
        self.assertEqual([e.city for e in evs], ["Bremen", "Cologne"])

    def test_the_map_behind_the_dialog_is_not_a_cell(self):
        """'Gdansk' is a world-map label at cx=2270; nearest-column assignment made it the
        Bremen row's city until the search was bounded to the dialog."""
        (ev,) = read_trade_events(None, elements=_dialog([BREMEN],
                                                         extras=[_el("Gdansk", 2270, 332)]))
        self.assertEqual(ev.city, "Bremen")

    def test_a_clipped_row_reports_no_window_rather_than_guessing(self):
        """The list scrolls; the bottom row is usually cut off."""
        clipped = [_el("Spices", 786, 876, etype="button", w=98, h=40),
                   _el("2026/08/26", 1151, 881), _el("Edinburgh", 1664, 882, w=117)]
        evs = read_trade_events(None, elements=_dialog([BREMEN], extras=clipped))
        edi = next(e for e in evs if e.city == "Edinburgh")
        self.assertIsNone(edi.start)

    def test_a_mangled_city_is_corrected_against_the_catalogue(self):
        """OCR reads Edinburgh as 'Fdinhuroh' on this dialog."""
        mangled = ("Bazaar", "Spices", "2026/08/26", "13.00", "2026/08/26", "14.00",
                   "Fdinhuroh", 15)
        (ev,) = read_trade_events(None, elements=_dialog([mangled]))
        self.assertEqual(ev.city, "Edinburgh")

    def test_no_dialog_means_no_events(self):
        self.assertEqual(read_trade_events(None, elements=[_el("World Map", 164, 49)]), [])


class EventTiming(unittest.TestCase):

    EV = TradeEvent(kind="Bazaar", goods="Spices", city="Bremen",
                    start=datetime(2026, 8, 24, 13, 0, tzinfo=KST),
                    end=datetime(2026, 8, 24, 14, 0, tzinfo=KST))

    def test_starts_in_is_measured_across_timezones(self):
        now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)   # 11:00 KST
        self.assertEqual(self.EV.starts_in(now).total_seconds(), 2 * 3600)

    def test_it_knows_when_it_is_live(self):
        self.assertTrue(self.EV.is_live(datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)))

    def test_it_is_not_live_before_the_window(self):
        self.assertFalse(self.EV.is_live(datetime(2026, 8, 24, 3, 0, tzinfo=timezone.utc)))

    def test_it_is_not_live_after_the_window(self):
        self.assertFalse(self.EV.is_live(datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
