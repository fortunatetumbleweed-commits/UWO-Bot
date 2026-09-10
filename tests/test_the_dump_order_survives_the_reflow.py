"""Discarding re-flows the Cargo row, so the dumps must run RIGHT TO LEFT.

The tiles are located during the probe, before anything is dumped. Emptying one removes it
and every tile to its RIGHT slides left — so a coordinate taken earlier now lands on its
neighbour, and the plan taps the wrong thing from the second dump onward.

Live 2026-09-10 at Hutu Village, last round. The row was

    Water(623)  Food(751)  Pig(878)  Raisin(1007)  Output(1136)

and the plan was [Pig 141, Raisin 141, Water 2, Food 10]. Pig went first and its tile was
removed, so Raisin slid into 878 — and the tap at the remembered 1007 hit the OUTPUT tile,
which is inert, opened nothing, and logged "expected Raisin, got None". Water and Food still
worked because they sit LEFT of Pig, where nothing moved. 141 Raisin stayed aboard and 141
more Bambara Groundnut were dropped for want of the room they would have freed.

Rightmost first makes every remaining coordinate stay valid with no re-read: a removal only
moves tiles that come after it. Same defect CLAUDE.md records for the sell grid — "selling
re-flows the grid, so the fix is sell-page -> scroll -> repeat, never read-all-then-tap".
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from actions.overflow_dialog import clear_overflow


def _el(label, x1, y1=522, w=130, h=128):
    return types.SimpleNamespace(label=label, element_type="button",
                                 x1=x1, x2=x1 + w, y1=y1, y2=y1 + h,
                                 cx=x1 + w // 2, cy=y1 + h // 2)


# The Hutu row, left to right.
ROW = [("Water", 194, 557), ("Food", 202, 687), ("Pig", 141, 813),
       ("Raisin", 141, 942), ("Bambara Groundnut", 4274, 1070)]


class _Card:
    def __init__(self):
        self.pending = 612
        self.receive = _el("Receive", 1140, 900)
        self.tiles = [types.SimpleNamespace(qty=q, element=_el(str(q), x))
                      for _n, q, x in ROW]
        self.cargo_used, self.cargo_capacity = 4952, 4952


def _run(taps):
    """Drive a last-round clear, recording the ORDER goods were tapped for discard.

    Keyed by POSITION, not quantity: Pig and Raisin both hold 141 — which is exactly the
    ambiguity that makes re-locating a good after a dump impossible, and why ordering is
    the fix rather than a re-read."""
    by_x = {x: n for n, _q, x in ROW}

    def _probe(_capture, _tap, state, **_kw):
        return [{"name": by_x[t.element.x1], "qty": t.qty, "tile": t} for t in state.tiles
                if by_x[t.element.x1] != "Bambara Groundnut"]

    def _read_discard(_els):
        # Whatever the plan asked for, so only the ORDER is under test here.
        return types.SimpleNamespace(name=_read_discard.expect, selected=None, held=None,
                                     qty_field=None, ok=_el("Ok", 1300), cancel=None)

    def _tap_element(el, **kw):
        # ONLY THE TILE TAP counts — the confirm button carries a `discard 141 Pig` why of
        # its own, and counting both would double every entry.
        name = by_x.get(getattr(el, "x1", None))
        if name is not None:
            _read_discard.expect = name
            taps.append(name)

    _read_discard.expect = None
    with mock.patch("actions.overflow_dialog.read_overflow", return_value=_Card()), \
         mock.patch("actions.overflow_dialog.probe_tiles", _probe), \
         mock.patch("actions.overflow_dialog.read_discard", _read_discard):
        return clear_overflow(
            output_good="Bambara Groundnut",
            needs_per_round={"Pig": 218, "Raisin": 188},
            reserves={"water": 192, "food": 192},
            last_round=True,
            capture_fn=lambda: object(), tap_fn=lambda *a: None,
            omni_fn=lambda _f: [],
            ui_mod=types.SimpleNamespace(tap_element=_tap_element),
            type_qty_fn=lambda *a, **k: True)


class TheDumpsRunRightToLeft(unittest.TestCase):

    def test_RAISIN_IS_TAPPED_BEFORE_PIG(self):
        """Pig sits left of Raisin, so dumping Pig first is what moved Raisin's tile."""
        taps = []
        _run(taps)
        self.assertIn("Pig", taps)
        self.assertIn("Raisin", taps)
        self.assertLess(taps.index("Raisin"), taps.index("Pig"),
                        f"Pig was dumped before Raisin, which slides Raisin's tile: {taps}")

    def test_every_planned_good_is_still_dumped(self):
        """Reordering must not drop one — the whole point is that all of them land."""
        taps = []
        _run(taps)
        self.assertEqual(sorted(taps), sorted(["Pig", "Raisin", "Water", "Food"]))

    def test_the_order_is_strictly_right_to_left(self):
        taps = []
        _run(taps)
        x = {n: pos for n, _q, pos in ROW}
        self.assertEqual(taps, sorted(taps, key=lambda n: x[n], reverse=True))


if __name__ == "__main__":
    unittest.main()
