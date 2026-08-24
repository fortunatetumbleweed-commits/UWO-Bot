"""Tests for #23 buy-materials executor (perceive-act-perceive) + purchase assignment."""
import types
import unittest

from brain.gathering_solver import assign_purchases
from actions.buy_materials import (
    buy_materials_at_port, purchase_goods, _find_material_tile, _find_purchase_commit,
    _cost_of,
)


def _el(label, cx, cy, etype="button"):
    return types.SimpleNamespace(label=label, cx=cx, cy=cy, element_type=etype)


def _commit(verb="Purchase", cost="2,501,234", currency="ducat", cx=2260, cy=990):
    return types.SimpleNamespace(verb=verb, cost=cost, currency=currency, cx=cx, cy=cy)


class AssignPurchasesTests(unittest.TestCase):
    def test_assigns_each_material_to_first_route_port(self):
        got = assign_purchases(["Malaga", "Montpellier"],
                               {"Garlic": ["Malaga"], "Carrot": ["Montpellier"],
                                "Onion": ["Malaga"]},
                               {"Garlic": 100, "Carrot": 50, "Onion": 200})
        self.assertEqual(got["Malaga"], {"Garlic": 100, "Onion": 200})
        self.assertEqual(got["Montpellier"], {"Carrot": 50})


class TileAndCommitTests(unittest.TestCase):
    def test_find_material_tile(self):
        els = [_el("Ruby", 200, 200), _el("Coral", 541, 473), _el("Skipjack Tuna", 900, 200)]
        self.assertEqual(_find_material_tile(els, "Coral"), (541, 473))
        self.assertIsNone(_find_material_tile(els, "Ebony"))

    def test_tile_ignores_right_panel(self):
        els = [_el("Coral", 2075, 431)]        # cart-side, not the grid
        self.assertIsNone(_find_material_tile(els, "Coral"))

    def test_find_purchase_commit(self):
        import unittest.mock as m
        with m.patch("vision.region_detectors.commit_button.detect_commit_buttons",
                     return_value=[_commit()]):
            c = _find_purchase_commit("F", [])
            self.assertEqual((c.cx, c.cy), (2260, 990))

    def test_cost_parse(self):
        self.assertEqual(_cost_of(_commit(cost="2,501,234")), 2501234)
        self.assertEqual(_cost_of(_commit(cost="")), 0)


class BuyFlowTests(unittest.TestCase):
    def _run(self, tiles, commit, **kw):
        taps = []
        els = [_el(name, cx, cy) for name, (cx, cy) in tiles.items()]
        return buy_materials_at_port(
            {"Coral": 1224}, "Male",
            capture_fn=lambda: "F",
            tap_fn=lambda x, y: taps.append((x, y)),
            omni_fn=lambda f: els,
            commit_fn=lambda f, e: commit, settle=0, **kw), taps

    def test_perceive_act_perceive_taps_tile_then_purchase(self):
        # Core act-then-perceive: tap the tile, read the commit, tap Purchase. (The
        # Confirm-Purchase OK step needs real frames — validated live, not here.)
        r, taps = self._run({"Coral": (541, 473)}, _commit())
        self.assertTrue(r["ok"])
        self.assertEqual(r["tapped"], ["Coral"])
        self.assertEqual(r["cost"], 2501234)
        self.assertIn((541, 473), taps)          # tapped the tile
        self.assertIn((2260, 990), taps)         # tapped Purchase (opens confirm dialog)

    def test_no_tile_found_reports_failure(self):
        r, _ = self._run({"Ruby": (200, 200)}, _commit())   # no Coral tile
        self.assertFalse(r["ok"])
        self.assertIn("no tiles", r["reason"])

    def test_no_commit_after_load_is_failure(self):
        r, _ = self._run({"Coral": (541, 473)}, None)
        self.assertFalse(r["ok"])
        self.assertFalse(r["purchased"])

    def test_red_gem_purchase_refused(self):
        r, taps = self._run({"Coral": (541, 473)}, _commit(currency="red_gem"))
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("refused"))
        self.assertNotIn((2260, 990), taps)      # never tapped the red-gem Purchase

    def test_empty_orders_noop(self):
        r = buy_materials_at_port({}, "Male", capture_fn=lambda: "F",
                                  tap_fn=lambda x, y: None, omni_fn=lambda f: [])
        self.assertTrue(r["ok"])
        self.assertFalse(r["purchased"])


class PurchaseGoalTests(unittest.TestCase):
    """The unifying `goal` variable: set → buy those goods; None → buy `goods` (max)."""
    def _tiles(self, names):
        return [_el(n, cx, cy) for n, (cx, cy) in names.items()]

    def test_goal_targets_named_goods(self):
        taps = []
        els = self._tiles({"Coral": (541, 473), "Ruby": (200, 200)})
        r = purchase_goods("Male", goal={"Coral": 1000},
                           capture_fn=lambda: "F", tap_fn=lambda x, y: taps.append((x, y)),
                           omni_fn=lambda f: els, commit_fn=lambda f, e: _commit(), settle=0)
        self.assertEqual(r["tapped"], ["Coral"])      # only the goal good
        self.assertIn((541, 473), taps)
        self.assertNotIn((200, 200), taps)            # Ruby not in goal

    def test_no_goal_loads_named_goods_max(self):
        taps = []
        els = self._tiles({"Ruby": (200, 200), "Coral": (541, 473)})
        r = purchase_goods("Male", goal=None, goods=["Ruby", "Coral"],
                           capture_fn=lambda: "F", tap_fn=lambda x, y: taps.append((x, y)),
                           omni_fn=lambda f: els, commit_fn=lambda f, e: _commit(), settle=0)
        self.assertEqual(sorted(r["tapped"]), ["Coral", "Ruby"])   # both, max mode

    def test_no_goal_no_goods_is_noop(self):
        r = purchase_goods("Male", capture_fn=lambda: "F", tap_fn=lambda x, y: None,
                           omni_fn=lambda f: [], commit_fn=lambda f, e: None)
        self.assertTrue(r["ok"])
        self.assertFalse(r["purchased"])


if __name__ == "__main__":
    unittest.main()
