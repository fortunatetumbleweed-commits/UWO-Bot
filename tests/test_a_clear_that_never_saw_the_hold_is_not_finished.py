"""A clear that never reached the Sell grid must not report the hold as clear.

Live 2026-09-01 the birch mission died at Tripoli with `Cargo 4,952(+684)/4,952` in red,
still carrying 4,448 Bambara Groundnut from the previous run. The cause was two legs and
twenty minutes earlier, at Luanda:

    [sell] this is not the Sell grid — not reading it as the hold     <- twice
    [Luanda] nothing sellable after scrolling to page 2 — the clear is finished
    [market] freed at Luanda: nothing
    [mission_runner] trim_before_gather done

The trim tapped `MARKET_COORDS["sell"]` blind. That point lands in the DEAD SPACE between
'Purchase' and 'Sell', so the tab never switched. `_sell_page` noticed and said so — and
then returned `[]`, the same value it returns for a page with nothing on it. The caller
cannot tell those apart, so "I was never looking at the hold" became "the hold is empty".

Three places had to agree for the mission to sail on:
  * the tab switch, which tapped a calibrated point and verified nothing;
  * `_sell_page`, whose `[]` meant two different things;
  * `_sell_off`, which returned FINISHED unconditionally.

The correct switch existed the whole time, ~100 lines away in `buy_materials`, which is why
the ledger's own trip to the Sell page at Tripoli DID get there. One concern, two
implementations, and the broken one was in the selling path.
"""
import unittest
from unittest import mock

from actions.sell_goods import _sell_page, sell_goods


class TheWrongPageAndAnEmptyPageAreDifferentAnswers(unittest.TestCase):
    def test_a_page_that_is_not_the_sell_grid_reads_as_None(self):
        with mock.patch("actions.buy_materials._on_sell_tab", return_value=False):
            self.assertIsNone(_sell_page(object()),
                              "None means 'I was not looking at the hold'")

    def test_a_real_but_empty_sell_grid_reads_as_empty(self):
        with mock.patch("actions.buy_materials._on_sell_tab", return_value=True), \
             mock.patch("vision.market_reader.read_market_page_omni", return_value=[]):
            self.assertEqual(_sell_page(object()), [],
                             "[] means 'I looked, and there is nothing sellable'")


def _run_sell(*, reached, pages=None, **kw):
    """sell_goods with the device stubbed out; `reached` is what the tab switch reports."""
    pages = list(pages or [])
    seq = {"n": 0}

    def read_profits(_frame):
        i = seq["n"]; seq["n"] += 1
        return pages[i] if i < len(pages) else (pages[-1] if pages else [])

    with mock.patch("actions.buy_materials.ensure_sell_tab", return_value=reached):
        return sell_goods("Luanda",
                          capture_fn=lambda: object(),
                          tap_fn=lambda *a, **k: None,
                          read_profits_fn=read_profits,
                          scroll_fn=lambda: None,
                          settle=0.0, **kw) or {}


class AFailedSwitchIsRefusedNotConcluded(unittest.TestCase):
    def test_it_does_not_report_the_hold_as_clear(self):
        res = _run_sell(reached=False)
        self.assertIs(res.get("ok"), False)
        self.assertEqual(res.get("sold"), [])

    def test_the_reason_names_the_actual_problem(self):
        res = _run_sell(reached=False)
        self.assertIn("Sell grid", res.get("reason", ""),
                      "'nothing to sell' would be the lie that sailed a full hold")

    def test_losing_the_grid_midway_is_also_a_refusal(self):
        # Reached the grid, then it stayed unreadable across two looks.
        res = _run_sell(reached=True, pages=[None, None])
        self.assertIs(res.get("ok"), False)
        self.assertIn("lost the Sell grid", res.get("reason", ""))

    def test_one_unreadable_look_is_not_a_lost_grid(self):
        """The frame right after a sale is expected to be unsettled.

        Live 2026-09-01 at Tripoli this refused nine seconds after a SUCCESSFUL sale of
        4,448 Bambara Groundnut (+130M ducats), on a page that read perfectly a moment
        later — turning a completed clear into a failed leg. Waiting for our own effect is
        allowed; declaring defeat on the first blink is not.
        """
        res = _run_sell(reached=True, pages=[None, []])
        self.assertIsNot(res.get("ok"), False,
                         "a settled second look found the page; that is not a lost grid")


class TheActivityMustNotCallAFailedClearFinished(unittest.TestCase):
    def _sell_off(self, res):
        from brain.activities.market import MarketActivity
        a = MarketActivity()
        a._sell = lambda *args, **kw: res
        return a._sell_off(["Water", "Food"], "Luanda", clear=True)

    def test_a_refused_clear_reports_blocked(self):
        from brain.activities.market import BLOCKED
        out = self._sell_off({"ok": False, "sold": [],
                              "reason": "could not reach the Sell grid"})
        self.assertEqual(out.status, BLOCKED,
                         "FINISHED here is what marked trim_before_gather done")

    def test_a_genuinely_empty_hold_still_finishes(self):
        from brain.activities.market import FINISHED
        out = self._sell_off({"ok": True, "sold": []})
        self.assertEqual(out.status, FINISHED,
                         "nothing to sell is a real, successful clear")

    def test_a_flow_that_reports_no_ok_at_all_still_finishes(self):
        # Only an explicit False is a refusal; older flows omit the key entirely.
        from brain.activities.market import FINISHED
        self.assertEqual(self._sell_off({"sold": ["Ebony"]}).status, FINISHED)


if __name__ == "__main__":
    unittest.main()
