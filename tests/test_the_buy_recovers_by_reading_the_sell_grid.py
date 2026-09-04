"""When the right panel will not read, go and look at the panel that can.

The user's flow (2026-08-26): the sell grid says the fleet holds 400 Iron; the bot buys 200;
the right panel disagrees or will not read; so it opens the SELL panel again, which shows what
the fleet actually owns; it reads 600; the pending 200 resets; back to Purchase.

What this replaces: `owned count UNREADABLE after buying Iron — stopping rather than buying
blind`. That guard fires only when the cargo strip reads NO TILES AT ALL, so with a cluttered
hold it never fired and the loop bought past 2,000 against a goal of 470. Where it DOES fire,
stopping is not the best available move — the purchases were each confirmed by a result
dialog, and the sell grid can say what they added up to.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import buy_materials

_TILES = {(100, 200): 1, (300, 200): 4}


class TheSellGridIsConsultedRatherThanStopping(unittest.TestCase):

    def _run(self, *, owned_reads, cargo_totals, max_rounds=6, goal=470):
        """`owned_reads` is what each sell-grid read returns, in order."""
        reads = list(owned_reads)
        seen = {"owned_calls": 0, "rounds": 0}

        def read_owned(*_a, **_k):
            seen["owned_calls"] += 1
            return reads.pop(0) if len(reads) > 1 else reads[0]

        totals = list(cargo_totals)
        def cargo_total(_f):
            return totals.pop(0) if len(totals) > 1 else totals[0]

        def buy_round(*_a, **_k):
            seen["rounds"] += 1
            return {"ok": True, "cost": 139644}

        good = type("G", (), {"name": "Iron", "tap_x": 1, "tap_y": 1, "sold_out": False,
                              "available_qty": 999, "owned_qty": None})()

        with patch.object(buy_materials, "_read_cargo_total", side_effect=cargo_total), \
             patch.object(buy_materials, "track_bought_good", return_value=(None, None)), \
             patch.object(buy_materials, "tile_in_stock", return_value=True), \
             patch.object(buy_materials, "_cargo_tiles", return_value=_TILES), \
             patch.object(buy_materials, "time"):
            res = buy_materials.buy_to_goal(
                "Barcelona", {"Iron": goal}, max_rounds=max_rounds,
                capture_fn=lambda: object(), tap_fn=lambda *a, **k: None,
                omni_fn=lambda _f: [], read_market_fn=lambda *a, **k: [good],
                buy_round_fn=buy_round, refresh_fn=lambda *a, **k: {"ok": True},
                clear_blockers_fn=lambda *a, **k: False, show_grid_fn=lambda: None,
                tile_grayed_fn=lambda *a, **k: False, find_tile_fn=lambda *a, **k: (1, 1),
                read_owned_fn=read_owned, settle=0)
        return res, seen

    def test_it_re_reads_the_sell_grid_instead_of_stopping_blind(self):
        """First read seeds 400. The buy lands. The cargo strip goes unreadable, so the sell
        grid is consulted again and answers 600."""
        res, seen = self._run(owned_reads=[{"iron": 400}, {"iron": 600}],
                              cargo_totals=[None])
        self.assertGreaterEqual(seen["owned_calls"], 2,
                                "the sell grid must be consulted again, not given up on")
        self.assertNotIn("stopped to avoid buying blind", res.get("reason", ""))

    def test_it_still_stops_when_the_sell_grid_cannot_answer_either(self):
        """Going and looking is the better move; inventing a number is not. If the panel that
        can answer does not, stopping is right."""
        res, _seen = self._run(owned_reads=[{"iron": 400}, {}], cargo_totals=[None])
        self.assertFalse(res.get("ok"))
        self.assertIn("unreadable", res.get("reason", ""))

    def test_a_READABLE_tile_never_pays_for_the_sell_grid(self):
        """The expensive read is for the failure case only — it is a tab switch and a scroll.

        `track_bought_good` returning a number IS the readable case; the cargo total alone is
        not, because the per-good count is what the goal is measured in."""
        from unittest.mock import patch
        seen = {"owned_calls": 0}

        def read_owned(*_a, **_k):
            seen["owned_calls"] += 1
            return {"iron": 400}

        good = type("G", (), {"name": "Iron", "tap_x": 1, "tap_y": 1, "sold_out": False,
                              "available_qty": 999, "owned_qty": None})()
        with patch.object(buy_materials, "_read_cargo_total", return_value=2600), \
             patch.object(buy_materials, "track_bought_good", return_value=(500, (1, 1))), \
             patch.object(buy_materials, "tile_in_stock", return_value=True), \
             patch.object(buy_materials, "_cargo_tiles", return_value=_TILES), \
             patch.object(buy_materials, "time"):
            buy_materials.buy_to_goal(
                "Barcelona", {"Iron": 470}, max_rounds=3,
                capture_fn=lambda: object(), tap_fn=lambda *a, **k: None,
                omni_fn=lambda _f: [], read_market_fn=lambda *a, **k: [good],
                buy_round_fn=lambda *a, **k: {"ok": True}, refresh_fn=lambda *a, **k: {"ok": True},
                clear_blockers_fn=lambda *a, **k: False, show_grid_fn=lambda: None,
                tile_grayed_fn=lambda *a, **k: False, find_tile_fn=lambda *a, **k: (1, 1),
                read_owned_fn=read_owned, settle=0)
        self.assertEqual(seen["owned_calls"], 1, "seeded once, never re-read")


if __name__ == "__main__":
    unittest.main()
