"""A buy blocked for space sells its surplus in the market it is already standing in.

Live 2026-09-01 at Tripoli the fleet had everything it needed to unblock itself and did
nothing. `buy_to_goal` could not read how much Candle it owned, so it flipped to the Sell
grid to settle the ledger — and there, on screen, was

    Bambara Groundnut — 4,448 — Luxuries — 98% — profitable

It read the number, went back to Purchase, and failed the leg for want of space. The mission
graph does carry a selling leg:

    ['trim_before_gather', 'gather:Barcelona', 'gather:Tripoli', 'sell_surplus', ...]

scheduled AFTER the gather that was blocked. That is the deadlock: the gather needs room,
and the step that makes room is waiting on the gather.

The rule (user, 2026-09-01): if it is on the Sell page and holds profitable goods that are
not on the material list, it can just sell them. Selling is SUPPORT — opportunistic when the
place affords it, never a mandatory leg (CLAUDE.md) — so it belongs to the activity that owns
the market, not to a retry loop grown inside the buy primitive.

It stays BLOCKED after freeing space ON PURPOSE. The task re-dispatches the buy against a
hold that now has room; a handler that looped here would be the sub-loop this architecture
exists to remove.
"""
import unittest

from brain.activities.market import BLOCKED, FINISHED, MarketActivity


class _Goal:
    def __init__(self, orders): self.orders = dict(orders)
    def __str__(self): return f"hold {self.orders}"


def _activity(buy_result, sold):
    a = MarketActivity()
    a._buy = lambda *args, **kw: buy_result
    a._sell = lambda *args, **kw: {"ok": True, "sold": list(sold)}
    a._show_grid = lambda *a_, **k: None
    return a


ORDER = _Goal({"Iron": 822, "Matchlock Gun": 355, "Candle": 709})
BLOCKED_BUY = {"bought_total": 0, "met": False}


class ABlockedBuyFreesSpaceWhereItStands(unittest.TestCase):
    def test_it_sells_the_surplus(self):
        out = _activity(BLOCKED_BUY, ["Bambara Groundnut"])._buy_toward(ORDER, "Tripoli")
        self.assertEqual(out.observed.get("freed_for_space"), ["Bambara Groundnut"])

    def test_it_stays_blocked_so_the_task_re_dispatches_the_buy(self):
        out = _activity(BLOCKED_BUY, ["Bambara Groundnut"])._buy_toward(ORDER, "Tripoli")
        self.assertEqual(out.status, BLOCKED,
                         "the retry belongs to the task, not to a loop in this handler")

    def _protected(self):
        """The names the sell flow is told to keep. `_sell_off` passes them as `exclude`."""
        seen = {}

        def sell(_port, **kw):
            seen["protect"] = list(kw.get("exclude") or kw.get("keep") or [])
            return {"ok": True, "sold": []}

        a = _activity(BLOCKED_BUY, [])
        a._sell = sell
        a._buy_toward(ORDER, "Tripoli")
        return [p.lower() for p in seen.get("protect", [])]

    def test_the_materials_it_came_to_buy_are_protected(self):
        prot = self._protected()
        for m in ("iron", "matchlock gun", "candle"):
            self.assertIn(m, prot, "selling the materials would defeat the gather")

    def test_supplies_are_never_sold_to_make_room(self):
        prot = self._protected()
        self.assertIn("water", prot)
        self.assertIn("food", prot)

    def test_it_sells_on_profit_not_by_clearing_the_hold(self):
        """`clear` would dump at a loss; this is opportunistic, so it sells only profit."""
        seen = {}

        def sell(_port, **kw):
            seen["kw"] = kw
            return {"ok": True, "sold": []}

        a = _activity(BLOCKED_BUY, [])
        a._sell = sell
        a._buy_toward(ORDER, "Tripoli")
        self.assertNotEqual(seen["kw"].get("goal"), "clear")
        self.assertIn("exclude", seen["kw"])


class ItOnlyDoesThisWhenTheBuyGotNowhere(unittest.TestCase):
    def test_a_met_goal_sells_nothing(self):
        out = _activity({"bought_total": 900, "met": True}, ["X"])._buy_toward(ORDER, "Tripoli")
        self.assertEqual(out.status, FINISHED)
        self.assertEqual(out.observed.get("freed_for_space"), [])

    def test_partial_progress_sells_nothing(self):
        # Something was bought, so the hold is not the thing standing in the way.
        out = _activity({"bought_total": 120, "met": False}, ["X"])._buy_toward(ORDER, "Tripoli")
        self.assertEqual(out.observed.get("freed_for_space"), [])

    def test_a_sell_that_raises_never_breaks_the_buy_result(self):
        a = _activity(BLOCKED_BUY, [])
        a._sell_off = lambda *args, **kw: (_ for _ in ()).throw(RuntimeError("no market"))
        out = a._buy_toward(ORDER, "Tripoli")
        self.assertEqual(out.status, BLOCKED, "support must never be fatal")
        self.assertEqual(out.observed.get("freed_for_space"), [])


if __name__ == "__main__":
    unittest.main()
