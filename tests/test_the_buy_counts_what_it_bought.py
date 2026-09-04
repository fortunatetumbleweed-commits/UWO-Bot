"""A loop that cannot read the hold still knows what it PUT IN the hold.

From the user, watching the gather buy past 2,000 Iron while reporting 0/470 (2026-08-26):
"even if it could not read what the ship had, it should know this session alone has bought
more than enough."

The per-good tile was below the fold, so `track_bought_good` returned None every round and
`bought_total` never moved off 0. But the CARGO TOTAL — the `N/M` load counter in the right
panel — was legible in every frame of that run. Buying one good, its rise IS the units
bought, and that bounds the loop without any per-good reading at all.

It is a deliberately WEAKER claim than the owned count: it says nothing about what was
already aboard, so it can only ever stop the loop EARLY, never let it run on.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import buy_materials


# Tiles ARE present — just not the bought good's. That is the live case, and it matters:
# the existing "stopping rather than buying blind" guard fires only when the cargo strip
# reads NO tiles at all, so with a cluttered hold it never fired and the loop ran on.
_TILES = {(100, 200): 1, (300, 200): 4}


class TheSessionBoundStopsTheLoop(unittest.TestCase):

    def _run(self, cargo_totals, goal=470, max_rounds=24):
        """`cargo_totals` is what the load counter reads, in order, as the loop asks."""
        seq = list(cargo_totals)
        reads = {"n": 0}

        def cargo_total(_frame):
            i = min(reads["n"], len(seq) - 1)
            reads["n"] += 1
            return seq[i]

        rounds = {"n": 0}

        def buy_round(*a, **k):
            rounds["n"] += 1
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
                omni_fn=lambda _f: [], read_market_fn=lambda *a, **k: [good],   # a LIST of goods, not a dict
                buy_round_fn=buy_round, refresh_fn=lambda *a, **k: {"ok": True},
                clear_blockers_fn=lambda *a, **k: False, show_grid_fn=lambda: None,
                tile_grayed_fn=lambda *a, **k: False, find_tile_fn=lambda *a, **k: (1, 1),
                read_owned_fn=lambda *a, **k: {}, settle=0)
        return res, rounds["n"]

    def test_it_stops_once_the_session_has_bought_the_goal(self):
        """Baseline 2,600, and the round puts 500 aboard against a goal of 470.

        The session bound is checked BEFORE the existing "unreadable — stop rather than buy
        blind" guard, so it is what ends the loop here: it can say the goal is MET, where the
        guard can only say it has gone blind."""
        res, _n = self._run([2600, 3100] + [3100] * 30)
        self.assertTrue(res.get("met"), f"expected met, got {res.get('reason')}")
        self.assertGreaterEqual(res.get("session_bought", 0), 470)
        self.assertIn("this session", res.get("reason", ""))

    def test_it_does_not_stop_before_the_goal(self):
        """A weaker claim must never let the loop finish early."""
        res, _n = self._run([2600, 2650, 2700, 2750], max_rounds=3)
        self.assertLess(res.get("session_bought", 0), 470)

    def test_an_unreadable_counter_does_not_invent_progress(self):
        res, _n = self._run([None] * 40, max_rounds=3)
        self.assertLess(res.get("session_bought", 0) or 0, 470)


if __name__ == "__main__":
    unittest.main()
