"""Choosing a trade event to sell into.

The plan (user, 2026-08-23): sail to LONDON as the hub, read the Trade Event Schedule, and if
a Bazaar for a category we carry starts within 12 HOURS, wake near the window and sail out —
the spice bazaars sit around London, so the hop is short.

Times are Korean (UTC+9) and this machine is not. Measured 2026-08-23: 22:28 CDT is 12:28 KST
the NEXT day, which put the 13:00 KST Bremen bazaar half an hour out — not the few hours a
naive read of the dialog suggests. Every test here works in aware datetimes for that reason.
"""

from __future__ import annotations

import types
import unittest
from datetime import datetime, timedelta, timezone

from brain.event_selling import (bazaar_is_live, plan_event_sale,
                                 sellable_events, should_sell_now)
from vision.trade_event_reader import KST, TradeEvent

# 22:28 CDT on 2026-08-23 == 12:28 KST on 2026-08-24.
NOW = datetime(2026, 8, 24, 3, 28, tzinfo=timezone.utc)


def _ev(goods, city, start_kst, hours=1, kind="Bazaar", tax=15):
    start = start_kst.replace(tzinfo=KST)
    return TradeEvent(kind=kind, goods=goods, city=city, start=start,
                      end=start + timedelta(hours=hours), tax_pct=tax)


BREMEN = _ev("Spices", "Bremen", datetime(2026, 8, 24, 13, 0))        # +0.5h
BERGEN = _ev("Jewelry", "Bergen", datetime(2026, 8, 24, 15, 0))       # +2.5h, wrong goods
COLOGNE = _ev("Spices", "Cologne", datetime(2026, 8, 25, 13, 0))      # +24.5h, too far off
CLIPPED = TradeEvent(kind="Event", goods="Spices", city="Edinburgh")  # scrolled off, no window
OVER = _ev("Spices", "Lisboa", datetime(2026, 8, 24, 10, 0))          # ended at 11:00 KST

ALL = [BREMEN, BERGEN, COLOGNE, CLIPPED, OVER]


class WhichEventsAreWorthSelling(unittest.TestCase):

    def test_it_picks_the_matching_category_within_the_window(self):
        evs = sellable_events(ALL, ["Spices"], now=NOW)
        self.assertEqual([e.city for e in evs], ["Bremen"])

    def test_a_different_category_is_not_ours(self):
        """Jewelry is 2.5h out and reachable — but we carry Spices."""
        self.assertNotIn("Bergen", [e.city for e in sellable_events(ALL, ["Spices"], now=NOW)])

    def test_beyond_twelve_hours_is_not_planned(self):
        """Cologne is 24.5h out — real, just not now."""
        self.assertNotIn("Cologne", [e.city for e in sellable_events(ALL, ["Spices"], now=NOW)])

    def test_a_wider_horizon_reaches_it(self):
        cities = [e.city for e in sellable_events(ALL, ["Spices"], now=NOW, within_hours=30)]
        self.assertEqual(cities, ["Bremen", "Cologne"])

    def test_an_event_already_over_is_skipped(self):
        self.assertNotIn("Lisboa", [e.city for e in sellable_events(ALL, ["Spices"], now=NOW)])

    def test_a_clipped_row_is_not_planned_on(self):
        """The list scrolls; a cut-off row has no window and nothing to plan against."""
        self.assertNotIn("Edinburgh",
                         [e.city for e in sellable_events(ALL, ["Spices"], now=NOW)])

    def test_carrying_several_categories(self):
        cities = [e.city for e in sellable_events(ALL, ["Spices", "Jewelry"], now=NOW)]
        self.assertEqual(cities, ["Bremen", "Bergen"])

    def test_the_match_is_case_insensitive(self):
        self.assertTrue(sellable_events(ALL, ["spices"], now=NOW))


class WhenToSetOut(unittest.TestCase):

    def test_it_leaves_in_time_to_arrive_as_the_window_opens(self):
        plan = plan_event_sale(ALL, ["Spices"], lambda _c: 600, now=NOW)   # 10 min sailing
        self.assertEqual(plan.event.city, "Bremen")
        self.assertEqual(plan.wake_at, BREMEN.start - timedelta(minutes=10))

    def test_a_voyage_longer_than_the_window_is_refused(self):
        """Bremen closes at 14:00 KST; a 3-hour sail cannot make it."""
        self.assertIsNone(plan_event_sale([BREMEN], ["Spices"], lambda _c: 3 * 3600, now=NOW))

    def test_an_unknown_sailing_time_is_not_planned_around(self):
        """Arriving after the window closes wastes the trip — do not guess."""
        self.assertIsNone(plan_event_sale([BREMEN], ["Spices"], lambda _c: None, now=NOW))

    def test_it_sets_out_immediately_when_the_window_is_already_open(self):
        live = _ev("Spices", "Bremen", datetime(2026, 8, 24, 12, 0), hours=2)  # open now
        plan = plan_event_sale([live], ["Spices"], lambda _c: 600, now=NOW)
        self.assertEqual(plan.wake_at, NOW)

    def test_it_falls_through_to_a_later_reachable_event(self):
        """Bremen unreachable, Cologne reachable inside a wider horizon."""
        def sail(city):
            return 5 * 3600 if city == "Bremen" else 600
        plan = plan_event_sale(ALL, ["Spices"], sail, now=NOW, within_hours=30)
        self.assertEqual(plan.event.city, "Cologne")

    def test_no_matching_event_means_no_plan(self):
        self.assertIsNone(plan_event_sale(ALL, ["Ore"], lambda _c: 600, now=NOW))


if __name__ == "__main__":
    unittest.main()


class SellingOnlyAtBazaarPricing(unittest.TestCase):
    """The sale must land INSIDE the bazaar, and the game shows when it is live.

    On the Sell page a good in the bazaar's category gets a GREEN BAND on its tile and its
    price index runs very high — usually above 150%, often more (user, 2026-08-23). A normal
    market sits near 100%, so the index separates them on its own; the colour cue is
    corroboration and is deliberately not guessed at until a live bazaar frame is measured.
    """

    def _good(self, name, category, index):
        return types.SimpleNamespace(name=name, category=category, index_pct=index)

    def test_a_high_index_is_the_bazaar(self):
        goods = [self._good("Box of Nutmeg", "Spices", 187)]
        self.assertTrue(bazaar_is_live(goods, "Spices"))

    def test_an_ordinary_market_is_not(self):
        goods = [self._good("Box of Nutmeg", "Spices", 104)]
        self.assertFalse(bazaar_is_live(goods, "Spices"))

    def test_another_category_being_hot_does_not_count(self):
        """A Jewelry bazaar does not lift our Spices."""
        goods = [self._good("Coral", "Jewelry", 210), self._good("Box of Nutmeg", "Spices", 99)]
        self.assertFalse(bazaar_is_live(goods, "Spices"))

    def test_an_unreadable_index_is_not_a_bazaar(self):
        """'Cannot tell' must never read as 'the bazaar is on' — that dumps the cargo at the
        ordinary price."""
        goods = [self._good("Box of Nutmeg", "Spices", None)]
        self.assertFalse(bazaar_is_live(goods, "Spices"))

    def test_nothing_of_ours_on_the_page_is_not_a_bazaar(self):
        self.assertFalse(bazaar_is_live([self._good("Ruby", "Jewelry", 300)], "Spices"))

    def test_the_best_tile_in_the_category_decides(self):
        goods = [self._good("Pepper", "Spices", 101), self._good("Box of Nutmeg", "Spices", 165)]
        self.assertTrue(bazaar_is_live(goods, "Spices"))


class WhetherToCommitTheSale(unittest.TestCase):

    EV = _ev("Spices", "Bremen", datetime(2026, 8, 24, 13, 0))
    IN_WINDOW = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)      # 13:30 KST
    BEFORE = datetime(2026, 8, 24, 3, 30, tzinfo=timezone.utc)         # 12:30 KST

    def _good(self, index):
        return types.SimpleNamespace(name="Box of Nutmeg", category="Spices", index_pct=index)

    def test_it_sells_inside_the_window_at_bazaar_pricing(self):
        ok, _why = should_sell_now(self.EV, [self._good(180)], now=self.IN_WINDOW)
        self.assertTrue(ok)

    def test_it_does_not_sell_inside_the_window_at_ordinary_pricing(self):
        """Arrived early, or the wrong market — the price is what the sale is worth."""
        ok, why = should_sell_now(self.EV, [self._good(100)], now=self.IN_WINDOW)
        self.assertFalse(ok)
        self.assertIn("not at bazaar pricing", why)

    def test_the_price_decides_when_the_clock_disagrees(self):
        """If the tiles are hot, the bazaar IS on, whatever a stale schedule read said."""
        ok, why = should_sell_now(self.EV, [self._good(190)], now=self.BEFORE)
        self.assertTrue(ok)
        self.assertIn("outside the scheduled window", why)

    def test_it_does_not_sell_before_the_window_at_ordinary_pricing(self):
        ok, _why = should_sell_now(self.EV, [self._good(98)], now=self.BEFORE)
        self.assertFalse(ok)
