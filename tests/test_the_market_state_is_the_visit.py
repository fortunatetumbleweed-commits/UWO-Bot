"""What the market carries between ticks, and what it must not.

Once a flow becomes a tick per action, the locals that used to live for one call have to
live somewhere. This is that somewhere — and the interesting tests are about what it
REFUSES to hold.
"""

from __future__ import annotations

import unittest

from brain.market_state import MarketState


class ItBelongsToOneVisit(unittest.TestCase):
    """Guiding Principle #4: data has an owner and dies with it. A cart is PANEL data; a
    scroll position is BUILDING data; neither may cross into another visit."""

    def test_a_new_goal_gets_a_fresh_state(self):
        s = MarketState(key=("SellHold", "London"))
        s.sold.append("Bambara Groundnut")
        s.scrolled_pages = 2
        fresh = s.for_goal(("Hold", "London"))
        self.assertEqual(fresh.sold, [], "another goal's basket is not ours")
        self.assertEqual(fresh.scrolled_pages, 0)

    def test_a_new_port_gets_a_fresh_state(self):
        s = MarketState(key=("SellHold", "London"))
        s.scrolled_pages = 3
        self.assertEqual(s.for_goal(("SellHold", "Lisboa")).scrolled_pages, 0)

    def test_the_same_visit_keeps_its_state(self):
        s = MarketState(key=("SellHold", "London"))
        s.sold.append("Pig")
        self.assertIs(s.for_goal(("SellHold", "London")), s)
        self.assertEqual(s.sold, ["Pig"])


class TheRecordedIntentIsHowADroppedTapIsSeen(unittest.TestCase):
    """The judgement `barter_panel._try` makes inside a loop, made across ticks instead:
    what did I do, and what did the screen look like when I did it?"""

    def test_it_remembers_the_action_and_the_screen(self):
        s = MarketState()
        s.did("staged Raisin", signature=("sell", 3, 1540))
        self.assertEqual(s.last_intent, "staged Raisin")
        self.assertEqual(s.last_signature, ("sell", 3, 1540))

    def test_an_unchanged_signature_next_tick_means_nothing_happened(self):
        s = MarketState()
        s.did("tapped Sell", signature=("sell", 3, 1540))
        self.assertEqual(s.last_signature, ("sell", 3, 1540),
                         "the caller compares this against a FRESH reading")


class TheRetryBoundLivesWithTheGoal(unittest.TestCase):
    """Replacing `for attempt in range(...)`: the count is kept, the loop is not."""

    def test_a_control_may_be_retried_then_reported(self):
        s = MarketState()
        self.assertFalse(s.repeated("sell_button", limit=2))   # 1st
        self.assertFalse(s.repeated("sell_button", limit=2))   # 2nd
        self.assertTrue(s.repeated("sell_button", limit=2),    # 3rd — enough
                        "a control that never responds costs a report, not a loop")

    def test_a_control_that_responds_forgets_its_attempts(self):
        s = MarketState()
        s.repeated("sell_button", limit=2)
        s.landed("sell_button")
        self.assertFalse(s.repeated("sell_button", limit=2), "the count starts again")

    def test_controls_are_counted_separately(self):
        s = MarketState()
        s.repeated("sell_button", limit=1)
        s.repeated("sell_button", limit=1)
        self.assertFalse(s.repeated("scroll", limit=1),
                         "one stuck control does not exhaust another's budget")


class ItHoldsNothingAFreshLookCouldAnswer(unittest.TestCase):
    """A conclusion that outlives its evidence is the bug this whole design is against."""

    def test_there_is_no_staged_flag(self):
        fields = set(MarketState().__dataclass_fields__)
        for forbidden in ("staged", "is_staged", "basket_loaded", "tab", "grid"):
            self.assertNotIn(forbidden, fields,
                             f"{forbidden!r} is readable from the screen — the Sell button "
                             "carries a value when the basket has one")
