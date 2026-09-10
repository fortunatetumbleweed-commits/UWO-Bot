"""While a round remains, the overflow card has nothing worth taking — so do not work it.

Materials are protected until the last round and the output is never a dump candidate, which
leaves surplus supply: a few units the fleet needs at sea, against an overflow they cannot
cover. The probe that finds them costs a tap and a cancel per cargo tile.

Live 2026-09-10 at San Village, five overflows in a row. Each one probed six tiles — Pig,
Raisin, the Bambara Groundnut output, Water, Food — twelve taps and about seventy seconds, to
plan `[('Water', 3), ('Food', 3)]` against 47 pending: six units recoverable at best, bought
with supply. Pig was tapped five times and correctly never dumped, which is the whole point:
the answer was knowable before the first tap.

User, 2026-09-10: *"if it is not the last round, just receive. Just lose the 47 that
overflowed. Only do probe at the last round if there are surplus. And dump all the materials
that are left."*

The last-round test therefore had to move off the probe. It used to sum the materials named
by the probe — deriving the answer from the very thing the answer decides whether to do — and
it now comes from the barter panel, which the village reads on the tick that commits, before
the card covers it.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from actions.overflow_dialog import clear_overflow
from brain.activities.village import VillageActivity, _MAX_DAILY_ROUNDS


def _el(label, x=600, y=580):
    return types.SimpleNamespace(content=label, element_type="button",
                                 x1=x, y1=y, x2=x + 60, y2=y + 40, cx=x + 30, cy=y + 20)


class _Card:
    """An overflow card with 47 pending and a hold of materials, output and supplies."""

    def __init__(self):
        self.receive = _el("Receive", 1200, 930)
        self.pending = 47
        self.tiles = [types.SimpleNamespace(qty=q, element=_el(str(q), x))
                      for q, x in ((195, 620), (195, 750), (883, 878),
                                   (762, 1007), (2917, 1136))]
        self.cargo_used, self.cargo_capacity = 4952, 4952


def _run(last_round, *, taps, probed):
    card = _Card()
    with mock.patch("actions.overflow_dialog.read_overflow", return_value=card), \
         mock.patch("actions.overflow_dialog.probe_tiles",
                    side_effect=lambda *a, **k: probed.append(1) or []):
        return clear_overflow(
            output_good="Bambara Groundnut",
            needs_per_round={"Pig": 218, "Raisin": 188},
            reserves={"water": 192, "food": 192},
            last_round=last_round,
            capture_fn=lambda: object(), tap_fn=lambda *a: None,
            omni_fn=lambda _f: [],
            ui_mod=types.SimpleNamespace(
                tap_element=lambda el, **k: taps.append(getattr(el, "content", None))),
            type_qty_fn=lambda *a, **k: True)


class ARoundRemainsSoTheCardIsNotWorked(unittest.TestCase):

    def test_NOTHING_IS_TAPPED_BUT_RECEIVE(self):
        taps, probed = [], []
        res = _run(False, taps=taps, probed=probed)
        self.assertEqual(probed, [], "the probe ran with nothing to find")
        self.assertEqual(taps, ["Receive"])

    def test_the_overflow_is_reported_as_given_up(self):
        """Receiving what fits LOSES the rest, and that must be said, not hidden in an ok."""
        res = _run(False, taps=[], probed=[])
        self.assertEqual(res["sacrificed"], 47)
        self.assertEqual(res["discarded"], [])
        self.assertIn("not the last round", res["reason"])

    def test_the_last_round_still_probes(self):
        taps, probed = [], []
        _run(True, taps=taps, probed=probed)
        self.assertEqual(probed, [1], "the last round is where the probe earns its taps")


class TheLastRoundComesFromThePanelNotTheCard(unittest.TestCase):
    """The village answers it, because the card covers the panel that knows."""

    def _village(self, *, committed, funded):
        act = VillageActivity()
        act._committed = committed
        act._funded_rounds = funded
        return act

    def test_a_panel_funding_more_rounds_is_not_the_last(self):
        self.assertIs(self._village(committed=3, funded=5)._is_last_round(), False)

    def test_the_round_the_panel_funded_ITSELF_is_the_last(self):
        """`_funded_rounds` was read BEFORE the commit, so 1 meant 'this one, then none'."""
        self.assertIs(self._village(committed=3, funded=1)._is_last_round(), True)

    def test_the_days_allowance_ends_it_whatever_the_materials_say(self):
        act = self._village(committed=_MAX_DAILY_ROUNDS, funded=99)
        self.assertIs(act._is_last_round(), True)

    def test_UNKNOWN_STAYS_UNKNOWN(self):
        """No reading is not a licence to guess — None lets the card decide as it used to."""
        self.assertIsNone(self._village(committed=3, funded=None)._is_last_round())


if __name__ == "__main__":
    unittest.main()
