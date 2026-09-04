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
import actions.barter_panel as bp

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
        tiles = bp._tradable_tiles(_panel_elements())
        self.assertEqual(len(tiles), 4)
        self.assertEqual({t["category"] for t in tiles},
                         {"Spices", "Food", "Livestock", "Jewelry"})

    def test_a_tile_taps_its_icon_not_its_label(self):
        tiles = bp._tradable_tiles(_panel_elements())
        self.assertTrue(all(t["cy"] == 424 for t in tiles), "the icon row, not the labels")

    def test_the_stock_status_is_carried(self):
        tiles = {t["category"]: t["status"] for t in bp._tradable_tiles(_panel_elements())}
        self.assertEqual(tiles["Food"], "Depleted")


class SelectionVerifiesByReadingBack(unittest.TestCase):

    def _select(self, readings, good="Box of Nutmeg", recipe=RECIPE, baseline=None):
        # The panel is read ONCE before any tap, to establish what is already selected — a
        # first tap can be swallowed dismissing an info tip, and without a baseline that
        # unchanged reading gets recorded as "this tile is not the good" (live 2026-08-26).
        # Default baseline: nothing selected, matching no recipe.
        base = baseline or types.SimpleNamespace(
            selected_good="(nothing selected)", materials=[], amity_points=(0, 100000))
        taps, seq = [], [base] + list(readings)
        with patch("vision.omniparser.parse_fast_cached", return_value=_panel_elements()), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("actions.ui.tap_at", side_effect=lambda x, y, **k: taps.append((x, y))), \
             patch("actions.barter_reader.read_barter_panel",
                   side_effect=lambda _f: seq.pop(0) if len(seq) > 1 else seq[0]):
            ok = bp._select_trade_good(good, recipe)
        return ok, taps

    def test_the_category_hint_is_tried_first(self):
        """Box of Nutmeg is a spice — try the Spices tile before the rest."""
        ok, taps = self._select([_reading(good="Box of Nutmeg")])
        self.assertTrue(ok)
        # BELOW the icon's centre: a locked good draws a red banner across the middle of the
        # thumbnail, and tapping the banner raises an info tip instead of selecting — the tip
        # then swallows the next tap (live 2026-08-26, Svear).
        self.assertEqual(len(taps), 1)
        self.assertEqual(taps[0][0], 424)
        self.assertGreater(taps[0][1], 440, "the tap must clear the lock banner")

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
            self.assertFalse(bp._select_trade_good("Box of Nutmeg", RECIPE))


if __name__ == "__main__":
    unittest.main()


class TheNodeStatesAGoalAndMapsTheResult(unittest.TestCase):
    """The barter node no longer knows what a panel is.

    It was 219 lines and was both the goal and the procedure — open the panel, pick the tile,
    tap Exchange, confirm, clear overflow, count rounds, decide whether to sail. All of that
    is the village activity's now, and is tested in tests/test_village_activity.py. What is
    left here is a goal and a translation, so that is what these test.

    This class replaces `TheSelectionPathIsReachable`, which guarded a 2026-08-23
    UnboundLocalError caused by `mission_progress` being imported twice inside the node — a
    bug that cannot recur in a body with neither import nor branch.
    """

    def _run(self, observed, *, ok=True):
        from unittest.mock import patch
        from brain.activities.village import Barter
        from brain.barter_mission_live import make_live_executors
        from brain.dispatcher import ActivityResult, BLOCKED, FINISHED

        task = types.SimpleNamespace(params={"rounds": 1, "good": "Box of Nutmeg",
                                             "village": "Melanesian Village"})
        goals = []
        result = ActivityResult(FINISHED if ok else BLOCKED, observed, detail="barter")
        with patch("brain.run_goal.run_goal",
                   side_effect=lambda g, **k: goals.append(g) or result), \
             patch("brain.mission_progress.record_rounds"), \
             patch("brain.mission_progress.advance"):
            res = make_live_executors()["barter"](task)
        return res, goals

    def test_the_goal_names_the_good_and_the_village(self):
        from brain.activities.village import Barter
        _res, goals = self._run({"rounds_committed": 3})
        self.assertEqual(goals, [Barter("Box of Nutmeg", "Melanesian Village")])

    def test_the_plans_round_count_is_not_passed_on(self):
        """`rounds` is the pre-sail estimate and the panel outranks it."""
        _res, goals = self._run({"rounds_committed": 3})
        self.assertFalse(hasattr(goals[0], "rounds"))

    def test_the_rounds_committed_come_back(self):
        res, _g = self._run({"rounds_committed": 3})
        self.assertEqual(res["committed"], 3)

    def test_why_it_stopped_becomes_the_reason(self):
        res, _g = self._run({"rounds_committed": 3,
                             "stopped_because": "the village refused — Exchange is greyed"})
        self.assertIn("refused", res["reason"])

    def test_no_caller_is_told_to_come_back_for_more(self):
        """The activity barters until the village refuses, so there is nothing to re-enter
        for. Re-entering on a number the task re-derived from materials is what produced the
        74-round loop at Svear."""
        res, _g = self._run({"rounds_committed": 3})
        self.assertEqual(res["more_rounds_fundable"], 0)

    def test_a_village_that_cannot_be_reached_is_reported(self):
        from unittest.mock import patch
        from brain.barter_mission_live import make_live_executors
        task = types.SimpleNamespace(params={"good": "Box of Nutmeg", "village": "X"})
        with patch("brain.run_goal.run_goal", return_value=None):
            res = make_live_executors()["barter"](task)
        self.assertFalse(res["ok"])
        self.assertEqual(res["committed"], 0)
