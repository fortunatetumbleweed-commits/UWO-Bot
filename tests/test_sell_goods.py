"""Tests for goal-aware sell (#22 sell executor). Filter is pure; the flow uses the
shared perceive-act-perceive dialog handler (react_after_commit)."""
import types
import unittest
from unittest import mock

from actions.sell_goods import select_sellable, sell_goods


def _g(name, is_loss=False, profit=100, tap=(500, 500)):
    return types.SimpleNamespace(name=name, is_loss=is_loss, profit_per_unit=profit,
                                 tap_x=tap[0], tap_y=tap[1])


class SelectSellableTests(unittest.TestCase):
    def _goods(self):
        return [
            _g("Box of Nutmeg", profit=80000),      # the barter output — profitable
            _g("Coral", profit=50),                 # a barter MATERIAL — protect
            _g("Ruby", profit=200),                 # generic profitable good
            _g("Cheap Junk", is_loss=True),         # a loss — skip
            _g("Water"),                            # supply — protect
        ]

    def test_default_sells_all_profitable(self):
        got = [g.name for g in select_sellable(self._goods())]         # goal="profit"
        self.assertEqual(got, ["Box of Nutmeg", "Coral", "Ruby", "Water"])  # losses skipped

    def test_keep_protects_barter_materials_and_supply(self):
        got = [g.name for g in select_sellable(self._goods(),
                                               keep=["Coral", "Water", "Food"])]
        self.assertEqual(got, ["Box of Nutmeg", "Ruby"])   # materials + supply protected
        self.assertNotIn("Coral", got)

    def test_exclude_is_a_legacy_alias_for_keep(self):
        got = [g.name for g in select_sellable(self._goods(), exclude=["Coral", "Water"])]
        self.assertEqual(got, ["Box of Nutmeg", "Ruby"])

    def test_only_limits_to_named_goods(self):
        got = [g.name for g in select_sellable(self._goods(), only=["Ruby"])]
        self.assertEqual(got, ["Ruby"])

    def test_profit_goal_never_sells_losses(self):
        got = [g.name for g in select_sellable([_g("X", is_loss=True)])]
        self.assertEqual(got, [])

    def test_clear_goal_dumps_losses_too(self):
        # goal="clear" ignores profit → sells the loss-maker too (frees cargo for barter)
        got = [g.name for g in select_sellable(self._goods(), goal="clear",
                                               keep=["Coral", "Water"])]
        self.assertEqual(got, ["Box of Nutmeg", "Ruby", "Cheap Junk"])

    def test_case_insensitive_keep(self):
        got = [g.name for g in select_sellable([_g("Coral")], keep=["coral"])]
        self.assertEqual(got, [])


class SellFlowTests(unittest.TestCase):
    """The sell LOOP, with the page reader injected.

    Reaching the Sell page is a precondition of the flow, not part of it: `ensure_sell_tab`
    does a real capture-and-verify, and these tests hand `sell_goods` a stub reader and a
    string for a frame. Declaring the precondition here keeps them about selling. The switch
    itself, and what happens when it FAILS, are covered in
    `test_a_clear_that_never_saw_the_hold_is_not_finished`.
    """

    def setUp(self):
        patcher = mock.patch("actions.buy_materials.ensure_sell_tab", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_perceive_act_perceive_loads_selected_then_sells(self):
        taps = []
        commit = types.SimpleNamespace(verb="Sell", cost="900000", currency="ducat",
                                       cx=2260, cy=990)
        # Stateful perceive: round 0 shows Ruby+Coral; after Ruby sells it's gone (reflow).
        pages = [[_g("Ruby", profit=200, tap=(300, 400)), _g("Coral", profit=50, tap=(500, 400))],
                 [_g("Coral", profit=50, tap=(500, 400))]]
        calls = {"n": 0}
        def read(_f):
            p = pages[min(calls["n"], len(pages) - 1)]; calls["n"] += 1; return p
        r = sell_goods("Goa", keep=["Coral"],
                       capture_fn=lambda: "F",
                       tap_fn=lambda x, y: taps.append((x, y)),
                       omni_fn=lambda f: [],
                       read_profits_fn=read,
                       commit_fn=lambda f, e: commit, settle=0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["sold"], ["Ruby"])           # sold once, then loop ended (only Coral left)
        self.assertIn((300, 400), taps)      # loaded Ruby tile
        self.assertNotIn((500, 400), taps)   # did NOT load protected Coral
        self.assertIn((2260, 990), taps)     # tapped Sell

    def test_clear_goal_loops_over_reflow_until_only_materials_left(self):
        # >9-goods case in miniature: each round sells one batch; sold goods vanish, rest reflow.
        pages = [[_g("A", tap=(1, 1)), _g("B", tap=(2, 2)), _g("Coral", tap=(9, 9))],
                 [_g("C", is_loss=True, tap=(3, 3)), _g("Coral", tap=(9, 9))],  # C is a loss
                 [_g("Coral", tap=(9, 9))]]              # only the material remains → stop
        calls = {"n": 0}
        def read(_f):
            p = pages[min(calls["n"], len(pages) - 1)]; calls["n"] += 1; return p
        commit = types.SimpleNamespace(cx=2260, cy=990)
        r = sell_goods("Goa", goal="clear", keep=["Coral"],
                       capture_fn=lambda: "F", tap_fn=lambda x, y: None, omni_fn=lambda f: [],
                       read_profits_fn=read, commit_fn=lambda f, e: commit, settle=0)
        self.assertEqual(r["sold"], ["A", "B", "C"])    # C (a loss) sold too under goal="clear"
        self.assertEqual(len(r["rounds"]), 2)

    def test_nothing_sellable_is_ok_noop(self):
        r = sell_goods("Goa", keep=["Coral"],
                       capture_fn=lambda: "F", tap_fn=lambda x, y: None,
                       omni_fn=lambda f: [], read_profits_fn=lambda f: [_g("Coral")],
                       commit_fn=lambda f, e: None, settle=0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["sold"], [])


if __name__ == "__main__":
    unittest.main()
