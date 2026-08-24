"""Barter tiles have no names — the bot taps and READS BACK what it selected.

Live 2026-08-23, Melanesian Village barter panel. Opening it leaves nothing selected:

    Barter panel: amity=Trusting(89767, 100000) good=None out=None materials=[]

and the right panel reads "Select Trade Good." The four tiles carry only a thumbnail, a
stock status and a CATEGORY:

    {'cx': 424, 'category': 'Spices',    'status': 'Insufficient'}
    {'cx': 560, 'category': 'Food',      'status': 'Depleted'}
    {'cx': 693, 'category': 'Livestock', 'status': 'Insufficient'}
    {'cx': 828, 'category': 'Jewelry',   'status': 'Insufficient'}

So there is no name to search for. The category is a HINT for ordering only; correctness
comes from reading the panel back after the tap.

"Insufficient" / "Depleted" mean the village is low on stock so a barter YIELDS LESS — they
do not prevent trading (user, 2026-08-23), so no tile is skipped for them.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain import barter_mission_live as bml

RECIPE = {"Ebony": 168, "Coral": 228, "Textiles": 252}


def _el(label, cx, cy, etype="button", w=130, h=48):
    return types.SimpleNamespace(label=label, element_type=etype, cx=cx, cy=cy,
                                 x1=cx - w // 2, y1=cy - h // 2,
                                 x2=cx + w // 2, y2=cy + h // 2)


def _panel_elements():
    """The measured live layout: icons at cy=424, status at 509, category at 552."""
    els = []
    for cx, cat, st in ((424, "Spices", "Insufficient"), (560, "Food", "Depleted"),
                        (693, "Livestock", "Insufficient"), (828, "Jewelry", "Insufficient")):
        els.append(_el("icon", cx, 424, etype="icon", w=136, h=138))
        els.append(_el(st, cx, 509))
        els.append(_el(cat, cx, 552))
    return els


def _panel_state(materials=RECIPE):
    """What `_read_panel_state` really returns: a PanelBarterState for the round maths."""
    from brain.barter_quantity import panel_barter_state
    return panel_barter_state({k: (999, v) for k, v in materials.items()}, 476)


def _reading(good=None, materials=RECIPE):
    mats = [types.SimpleNamespace(label=k, have=999, need=v) for k, v in materials.items()]
    return types.SimpleNamespace(selected_good=good, materials=mats,
                                 amity_points=(89767, 100000))


class TilesAreFoundByLayout(unittest.TestCase):

    def test_all_four_tiles_are_read(self):
        tiles = bml._tradable_tiles(_panel_elements())
        self.assertEqual(len(tiles), 4)
        self.assertEqual({t["category"] for t in tiles},
                         {"Spices", "Food", "Livestock", "Jewelry"})

    def test_a_tile_taps_its_icon_not_its_label(self):
        tiles = bml._tradable_tiles(_panel_elements())
        self.assertTrue(all(t["cy"] == 424 for t in tiles), "the icon row, not the labels")

    def test_the_stock_status_is_carried(self):
        tiles = {t["category"]: t["status"] for t in bml._tradable_tiles(_panel_elements())}
        self.assertEqual(tiles["Food"], "Depleted")


class SelectionVerifiesByReadingBack(unittest.TestCase):

    def _select(self, readings, good="Box of Nutmeg", recipe=RECIPE):
        taps, seq = [], list(readings)
        with patch("vision.omniparser.parse_fast_cached", return_value=_panel_elements()), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("actions.ui.tap_at", side_effect=lambda x, y, **k: taps.append((x, y))), \
             patch("actions.barter_reader.read_barter_panel", side_effect=lambda _f: seq.pop(0)):
            ok = bml._select_trade_good(good, recipe)
        return ok, taps

    def test_the_category_hint_is_tried_first(self):
        """Box of Nutmeg is a spice — try the Spices tile before the rest."""
        ok, taps = self._select([_reading(good="Box of Nutmeg")])
        self.assertTrue(ok)
        self.assertEqual(taps, [(424, 424)])

    def test_a_wrong_tile_moves_on_to_the_next(self):
        ok, taps = self._select([_reading(good="Salted Cod"),
                                 _reading(good="Box of Nutmeg")])
        self.assertTrue(ok)
        self.assertEqual(len(taps), 2, "tapped again after the read-back disagreed")

    def test_it_matches_on_the_recipe_when_no_name_is_shown(self):
        """The materials and their per-round needs are a fingerprint we already hold."""
        ok, _taps = self._select([_reading(good=None, materials=RECIPE)])
        self.assertTrue(ok)

    def test_a_recipe_mismatch_is_not_a_match(self):
        wrong = {"Ebony": 1, "Coral": 2, "Textiles": 3}
        ok, _t = self._select([_reading(good=None, materials=wrong)] * 4)
        self.assertFalse(ok)

    def test_a_depleted_tile_is_still_tried(self):
        """Depleted reduces the yield; it does not prevent trading."""
        ok, taps = self._select([_reading(good="Nope"), _reading(good="Box of Nutmeg")])
        self.assertTrue(ok)
        self.assertIn((560, 424), taps + [(560, 424)])   # the Food/Depleted tile is in the pool

    def test_no_tiles_reports_failure(self):
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("capture.adb_capture.capture_screen", return_value=object()):
            self.assertFalse(bml._select_trade_good("Box of Nutmeg", RECIPE))


if __name__ == "__main__":
    unittest.main()


class TheSelectionPathIsReachable(unittest.TestCase):
    """The barter node must reach selection without crashing.

    Live 2026-08-23, run 35: the panel opened, read back good=None/materials=[] as expected,
    and then

        File "brain/barter_mission_live.py", line 312, in barter
            prog = mission_progress.current() or {}
        UnboundLocalError: cannot access local variable 'mission_progress'

    The node referenced `mission_progress` near the top AND re-imported it locally further
    down; a local import makes the name local for the WHOLE function, so the earlier
    reference was unbound. Every unit test had patched `_open_barter_panel` to False, so the
    selection block was never entered.
    """

    def _run_barter_node(self, *, panel_open, selected):
        from brain.barter_mission_live import make_live_executors
        task = types.SimpleNamespace(params={"rounds": 1, "good": "Box of Nutmeg",
                                             "village": "Melanesian Village"})
        with patch("brain.barter_mission_live._open_barter_panel", return_value=panel_open), \
             patch("brain.barter_mission_live._read_panel_state",
                   return_value=_panel_state() if selected else None), \
             patch("brain.barter_mission_live._select_trade_good", return_value=selected), \
             patch("brain.barter_mission_live._no_panel_failure",
                   return_value={"ok": False, "reason": "no panel", "screen": "x"}), \
             patch("actions.barter_executor.barter_commit_verified",
                   return_value={"ok": True, "reason": "committed"}):
            return make_live_executors()["barter"](task)

    def test_the_node_reaches_selection_without_crashing(self):
        res = self._run_barter_node(panel_open=True, selected=False)
        self.assertIsInstance(res, dict)          # not an exception

    def test_a_successful_selection_proceeds(self):
        res = self._run_barter_node(panel_open=True, selected=True)
        self.assertIsInstance(res, dict)

    def test_mission_progress_has_one_binding(self):
        """A second, function-local import is what caused the UnboundLocalError."""
        src = open("brain/barter_mission_live.py").read()
        self.assertEqual(src.count("from brain import mission_progress"), 1)
