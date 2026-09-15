"""Tests for route-execution pure helpers (#10). Route-list tokens mirror the real
OCR from the live Portobelo world-map Route tab."""
import unittest

from actions.route_execution import (
    match_route_row, find_text_button,
    route_supply_ok, free_sail_supply_ok, is_moving, checkback_seconds,
    DEFAULT_LONGEST_LEG_DAYS, SEC_PER_GAME_DAY,
)

# Real-ish route-list tokens (left column) from the live Route tab.
_ROUTE_LIST = [
    ("Sailing Route 2", 0.9, 149, 145),
    ("Sell gold", 1.0, 100, 206),
    ("ea suetz", 0.8, 107, 265),
    ("Sailing Route 1", 0.8, 149, 329),
]

# World-map tab row + route panel with a Move button.
_TABS = [("Port", 1.0, 859, 52), ("Explore", 1.0, 1047, 55),
         ("Route", 1.0, 1245, 50), ("Trade", 1.0, 1371, 52)]


class RouteMatchTests(unittest.TestCase):
    def test_selects_route_2_not_route_1(self):
        # The trailing number must disambiguate the two "Sailing Route" rows.
        self.assertEqual(match_route_row(_ROUTE_LIST, "Sailing Route 2"), (149, 145))
        self.assertEqual(match_route_row(_ROUTE_LIST, "Sailing Route 1"), (149, 329))

    def test_matches_non_numbered_route(self):
        self.assertEqual(match_route_row(_ROUTE_LIST, "Sell gold"), (100, 206))

    def test_ignores_right_side_tokens(self):
        # A matching name in the map area (cx large) is not a route-list row.
        toks = _ROUTE_LIST + [("Sailing Route 2", 1.0, 1800, 145)]
        self.assertEqual(match_route_row(toks, "Sailing Route 2"), (149, 145))

    def test_unknown_route_returns_none(self):
        self.assertIsNone(match_route_row(_ROUTE_LIST, "Nonexistent Route"))

    def test_tolerates_minor_ocr_noise(self):
        toks = [("Saling Route 2", 0.7, 149, 145)]   # dropped a letter
        self.assertEqual(match_route_row(toks, "Sailing Route 2"), (149, 145))


class ButtonFindTests(unittest.TestCase):
    def test_finds_route_tab(self):
        self.assertEqual(find_text_button(_TABS, "route"), (1245, 50))

    def test_finds_move_button(self):
        toks = [("Move", 1.0, 1142, 1012), ("Filter", 1.0, 120, 1000)]
        self.assertEqual(find_text_button(toks, "move"), (1142, 1012))

    def test_absent_keyword_returns_none(self):
        self.assertIsNone(find_text_button(_TABS, "market"))


class SupplyCheckTests(unittest.TestCase):
    def test_route_needs_only_longest_leg(self):
        self.assertTrue(route_supply_ok(6))            # 6 >= default 6
        self.assertTrue(route_supply_ok(20))
        self.assertFalse(route_supply_ok(5))
        self.assertIsNone(route_supply_ok(None))

    def test_route_custom_longest_leg(self):
        self.assertFalse(route_supply_ok(6, longest_leg_days=8))
        self.assertTrue(route_supply_ok(8, longest_leg_days=8))

    def test_free_sail_needs_whole_voyage_plus_buffer(self):
        self.assertTrue(free_sail_supply_ok(11, 8))    # 11 >= 8+2
        self.assertFalse(free_sail_supply_ok(11, 10))  # 11 < 10+2
        self.assertIsNone(free_sail_supply_ok(None, 8))
        self.assertIsNone(free_sail_supply_ok(11, None))

    def test_default_longest_leg_is_six(self):
        self.assertEqual(DEFAULT_LONGEST_LEG_DAYS, 6)


class MotionTests(unittest.TestCase):
    def test_moving_when_eta_decreases(self):
        self.assertTrue(is_moving({"eta_days": 12}, {"eta_days": 11}))

    def test_moving_when_day_increases(self):
        self.assertTrue(is_moving({"day_at_sea": 1}, {"day_at_sea": 2}))

    def test_not_moving_when_unchanged(self):
        self.assertFalse(is_moving({"eta_days": 12, "day_at_sea": 3},
                                   {"eta_days": 12, "day_at_sea": 3}))

    def test_not_moving_when_unreadable(self):
        self.assertFalse(is_moving({"eta_days": None}, {"eta_days": None}))


class CheckbackTests(unittest.TestCase):
    def test_scales_with_eta(self):
        self.assertEqual(checkback_seconds(12), 12 * SEC_PER_GAME_DAY)

    def test_falls_back_to_one_day(self):
        self.assertEqual(checkback_seconds(None), SEC_PER_GAME_DAY)
        self.assertEqual(checkback_seconds(0), SEC_PER_GAME_DAY)


if __name__ == "__main__":
    unittest.main()


def test_the_route_TAB_is_not_a_route_ROW():
    """A saved route named "Sailing Route 2" contains the word "route".

    Live 2026-08-24: the Route tab token was not OCR'd at all, so an unbounded CONTAINS match
    picked the ROW instead — tapping it leaves the panel showing something else, and the row
    scan that follows then reports the wanted route "not found in list" even though it is
    right there. Bounding the search to the tab bar refuses rather than mis-taps.
    """
    from actions.route_execution import find_text_button
    toks = [("Port", 1.0, 859, 53), ("Map", 1.0, 282, 53),
            ("Sailing Route 2", 0.92, 149, 145), ("san to london", 0.87, 143, 204)]
    assert find_text_button(toks, "route") == (149, 145)          # the trap
    assert find_text_button(toks, "route", y_max=110) is None      # guarded

    with_tab = toks + [("Route", 1.0, 1234, 53)]
    assert find_text_button(with_tab, "route", y_max=110) == (1234, 53)


def test_THE_ROUTE_TAB_IS_REQUIRED_NOT_TAPPED():
    """The list is only the route list once the Route tab is LIT, so the tab is a
    precondition to check, never an action to perform and assume.

    Live 2026-09-14, one leg from home. The world map opened on the Port tab, the Route tap
    at (1234,49) was dropped, and nothing noticed: the tab strip is unchanged across the tap
    and all three list looks (mean pixel change 0.11, noise). The run reported
    "route 'Sans to London' not found in list", which is a CONCLUSION — it never saw the
    route list. That route had sailed three times in the preceding fortnight.

    This module had forked its own tab finder before `require_world_map_tab` existed and
    never came back to it. The canonical one confirms by EFFECT and repeats the same tap
    after a longer settle, and the port search, the village search and WorldMapActivity all
    use it. Asserted on the source so the fork cannot quietly return.
    """
    import inspect

    from actions.route_execution import select_route_and_move

    src = inspect.getsource(select_route_and_move)
    assert 'require_world_map_tab("route"' in src, "the tab must be REQUIRED, not tapped"

    # …and a refusal must stop, not fall through to reading whatever tab is lit.
    # ANCHOR ON THE CALL, NOT THE WORD: the comment above it names the function too, and an
    # index into the prose measures the comment rather than the code.
    gate = src.index("if not require_world_map_tab(")
    assert "return False" in src[gate:gate + 400]


def test_a_failed_tab_switch_does_not_blame_the_route():
    """The old message named the route, which sent the reader hunting a name that was
    right. What it must report is what it actually observed."""
    import inspect

    from actions.route_execution import select_route_and_move

    src = inspect.getsource(select_route_and_move)
    gate = src.index("if not require_world_map_tab(")
    assert "not the route list" in src[gate:gate + 600]
