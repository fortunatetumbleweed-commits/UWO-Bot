"""A trim with nothing to trim must leave the market untouched.

Walkthrough of run 24 (live 2026-08-22, frames 15-17 of trace_barter_cmd_2026-08-22T21-21-44):

    21:24:51  [sell] omni grid 1x3: 3 goods            <- the hold, read correctly
    21:24:59  Tapping 'Put in Bulk' @ (387,1008) (ON -> OFF)
    21:25:08  [sell] omni grid 1x3: 3 goods            <- read correctly AGAIN
    21:25:11  Tapping 'Put in Bulk' @ (387,1008) (OFF -> ON)
    21:25:20  [clear_surplus] trimmed {} (nothing to trim)
    21:25:25  tap (107,53)                             <- the page title "< Sell"
    21:25:31  could not confirm the Sell tab - refusing to report owned counts

The Sell page's centre grid IS the cargo hold, so nothing above needed a tap: the decision
was answerable from the frame, and the counts the caller wanted had already been read twice.
"""

from __future__ import annotations

import unittest

from actions.sell_goods import sell_down_to


class _Good:
    def __init__(self, name, owned):
        self.name, self.owned_qty = name, owned
        self.tap_x = self.tap_y = 100


HOLD = [_Good("Ebony", 700), _Good("Coral", 797), _Good("Textiles", 1049)]


def _run(keep, goods=HOLD):
    """Drive sell_down_to with every device interaction recorded."""
    calls = {"bulk": [], "taps": [], "qty": [], "commits": []}
    res = sell_down_to(
        "Kolkata", keep=keep,
        capture_fn=lambda: object(),
        tap_fn=lambda x, y: calls["taps"].append((x, y)),
        omni_fn=lambda _f: [],
        read_page_fn=lambda _f: list(goods),
        set_bulk_fn=lambda on, _f=None: calls["bulk"].append(on),
        type_qty_fn=lambda n, **kw: calls["qty"].append(n) or True,
        commit_fn=lambda _f, _e: calls["commits"].append(1),
        react_fn=lambda *a, **k: None,
        find_button_fn=lambda *a, **k: (0, 0),
        settle=0,
    )
    return res, calls


class NothingToTrim(unittest.TestCase):

    KEEP_ALL_SATISFIED = {"Ebony": 700, "Coral": 1049, "Textiles": 1049}

    def test_the_bulk_checkbox_is_never_touched(self):
        _res, calls = _run(self.KEEP_ALL_SATISFIED)
        self.assertEqual(calls["bulk"], [])

    def test_nothing_is_tapped_at_all(self):
        _res, calls = _run(self.KEEP_ALL_SATISFIED)
        self.assertEqual(calls["taps"], [])

    def test_it_reports_success_with_an_empty_trim(self):
        res, _calls = _run(self.KEEP_ALL_SATISFIED)
        self.assertTrue(res["ok"])
        self.assertEqual(res["trimmed"], {})

    def test_it_hands_back_the_hold_it_read(self):
        """The caller needs these counts; re-reading them is what lost them live."""
        res, _calls = _run(self.KEEP_ALL_SATISFIED)
        self.assertEqual(res["owned"], {"ebony": 700, "coral": 797, "textiles": 1049})

    def test_a_good_at_exactly_its_keep_level_is_not_over_stocked(self):
        _res, calls = _run({"Coral": 797})
        self.assertEqual(calls["bulk"], [])


class SomethingToTrim(unittest.TestCase):

    def test_an_over_stocked_good_still_turns_bulk_off(self):
        _res, calls = _run({"Textiles": 900})
        self.assertIn(False, calls["bulk"])

    def test_it_only_engages_for_the_over_stocked_good(self):
        res, _calls = _run({"Ebony": 700, "Coral": 797, "Textiles": 900})
        self.assertNotIn("Ebony", res.get("trimmed", {}))


if __name__ == "__main__":
    unittest.main()
