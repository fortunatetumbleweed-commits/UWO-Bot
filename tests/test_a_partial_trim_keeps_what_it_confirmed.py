"""One good's dialog failing must not throw away the goods already confirmed.

Live 2026-09-06 at Tripoli. `sell_down_to` staged Candle 211 and Iron 177 — the exact
surpluses, the tiles left reading 709 and 822 — then Matchlock Gun's quantity dialog did not
open and the whole basket was abandoned: 205,013 ducats, profit 88,997, one tap from Sell.
That is the 2026-08-22 loss repeating ("981 Ebony loaded, Sell one tap away, thrown away").

The all-or-nothing abort exists for a real danger — with Put In Bulk still ON a tile tap loads
the WHOLE stack, and selling that dumps materials the barter needs. But the danger is about
THAT good, and it is observable: staging moves a good OUT of its tile (Iron 999 -> 822), so a
tile that has not moved is a tile whose tap did nothing. Every good already in the basket
passed a confirmed quantity dialog, so committing them is exactly as safe as the full commit.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from actions.sell_goods import sell_down_to

KEEP = {"Candle": 709, "Iron": 822, "Matchlock Gun": 411}
OWNED = {"Candle": 920, "Iron": 999, "Matchlock Gun": 486}


def _good(name, owned):
    return types.SimpleNamespace(name=name, owned_qty=owned, tap_x=100, tap_y=200,
                                 profit_per_unit=10)


class _Harness:
    def __init__(self, qty_fields, page_after_failure=None, modal=False):
        self.qty_fields = list(qty_fields)
        self.taps = []
        # `is_modal` gates the post-Load "did it close?" check, so it must be True to
        # exercise that path at all.
        self.modal = modal
        self.page_after_failure = page_after_failure
        self.sold, self.reads = None, 0

    def page(self, _frame):
        self.reads += 1
        owned = (self.page_after_failure
                 if self.page_after_failure is not None and self.reads > 1 else OWNED)
        return [_good(n, q) for n, q in owned.items()]

    def qty(self, *_a, **_k):
        return self.qty_fields.pop(0) if self.qty_fields else None

    def load_button(self, *_a, **_k):
        return (1313, 943)

    def commit(self, _frame, _els):
        return types.SimpleNamespace(cx=1959, cy=996, verb="Sell", cost="205,013")

    def run(self):
        with mock.patch("actions.sell_goods._find_qty_field", side_effect=self.qty), \
             mock.patch("memory.observed_facts.forget"):
            return sell_down_to(
                "Tripoli", KEEP,
                capture_fn=lambda: object(),
                tap_fn=lambda x, y, *a, **k: self.taps.append((x, y)),
                omni_fn=lambda _f: [], read_page_fn=self.page,
                set_bulk_fn=lambda *a, **k: True,
                type_qty_fn=lambda *a, **k: True, commit_fn=self.commit,
                find_button_fn=self.load_button,
                overlay_fn=lambda _f: types.SimpleNamespace(is_modal=self.modal,
                                                            bbox=None),
                react_fn=lambda *a, **k: None,
                ensure_sell_tab_fn=lambda *a, **k: True, settle=0)


# A quantity field: (x, y, owned). The DIALOG's count is the authority, so each good's
# own figure has to come back here — the grid badge is only our reading of a 40px overlay.
CANDLE, IRON, MATCH = (500, 600, 920), (500, 600, 999), (500, 600, 486)


class TheConfirmedGoodsAreStillSold(unittest.TestCase):

    def test_a_dropped_tap_is_retried_and_everything_is_trimmed(self):
        """The likeliest cause, and the cheapest fix: re-tap the same tile once."""
        h = _Harness([CANDLE, IRON, None, MATCH])    # Matchlock fails once, then opens
        out = h.run()
        self.assertTrue(out["ok"], out.get("reason"))
        self.assertEqual(set(out["trimmed"]), {"Candle", "Iron", "Matchlock Gun"})

    def test_a_good_that_never_opens_is_skipped_and_the_rest_sold(self):
        h = _Harness([CANDLE, IRON, None, None])     # fails, retried, fails again
        out = h.run()
        self.assertTrue(out["ok"], out.get("reason"))
        self.assertEqual(out["trimmed"], {"Candle": 211, "Iron": 177})
        self.assertTrue(any("Matchlock" in s for s in out["skipped"]))

    def test_a_tile_THAT_MOVED_still_aborts_everything(self):
        """A tap that bulk-loaded the stack moves the tile — the basket may hold an
        unconfirmed full stack, which is the case the abort exists for."""
        moved = dict(OWNED, **{"Matchlock Gun": 0})
        h = _Harness([CANDLE, IRON, None, None], page_after_failure=moved)
        out = h.run()
        self.assertFalse(out["ok"])
        self.assertEqual(out["trimmed"], {}, "nothing sold when the basket is not provably clean")

    def test_nothing_confirmed_yet_still_aborts(self):
        """With an empty basket there is nothing to save, so the old behaviour stands."""
        h = _Harness([None, None])
        out = h.run()
        self.assertFalse(out["ok"])
        self.assertEqual(out["trimmed"], {})


if __name__ == "__main__":
    unittest.main()


class ASwallowedLoadIsRetriedOnce(unittest.TestCase):
    """A dialog still open is PROOF the Load did not happen — which is what makes one re-tap
    safe: a Load that had worked would have closed it, so it cannot load the amount twice.

    Live 2026-09-06 at Madeira, frame 400 of trace_barter_cmd_2026-09-06T21-45-01: the Trade
    Goods Info card open for Pig, the field reading a correct `73 / 1,828`, and the tap at
    (1313,943) dead on Load. Four seconds later the card was unchanged. The trim aborted with
    "the amount may not be in the basket" and the whole surplus sailed on unsold.
    """

    def test_a_dropped_load_is_re_tapped_and_the_good_is_trimmed(self):
        # open, still-open (Load swallowed), closed on the retry
        h = _Harness([CANDLE, (500, 600, 920), None,
                      IRON, None,
                      MATCH, None], modal=True)
        out = h.run()
        self.assertTrue(out["ok"], out.get("reason"))
        self.assertIn("Candle", out["trimmed"])
        self.assertEqual(h.taps.count((1313, 943)), 4,
                         "three goods loaded, and the swallowed one re-tapped once")

    def test_a_dialog_that_will_not_close_still_aborts(self):
        """Two says the control is not responding, which is a fact for the mission."""
        h = _Harness([CANDLE, (500, 600, 920), (500, 600, 920)], modal=True)
        out = h.run()
        self.assertFalse(out["ok"])
        self.assertIn("still open after Load", out["reason"])
