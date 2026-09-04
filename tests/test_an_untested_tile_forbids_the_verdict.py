"""A tile whose tap never landed says NOTHING about what the village offers.

LIVE 2026-08-30 at Hutu Village. Three tiles: Luxuries, Medicine, Perfume. The FIRST tile
tapped swallowed both of its taps while the two after it selected on a single tap each — and
the swallowed one was the Luxuries tile, which IS Bambara Groundnut (tapping it by hand
straight afterwards selected it: Trade Quantity 859(+259), materials 1,580/219 and
1,792/219). The pass ended:

    could not get the Luxuries tile to select — moving on, but it remains UNTESTED
    none of the tiles read back as 'Bambara Groundnut'

and the caller reported "'Bambara Groundnut' is not on offer today" — six barter rounds open,
both materials aboard, standing in front of the good.

By the end of a pass the panel is demonstrably warm: other tiles have selected on it. So the
untested ones get another go, and if they still will not answer the row is UNREAD — which is
None, not False.
"""
import unittest
from unittest.mock import patch


class _Reading:
    def __init__(self, good): self.selected_good = good


class _Panel:
    """A panel where chosen tiles swallow their taps for the first N attempts."""
    def __init__(self, tiles, swallow=None):
        self.tiles, self.swallow = tiles, dict(swallow or {})
        self.selected = None
        self.taps = []

    def tap(self, cx, cy, why=""):
        cat = next((c for c, x in self.tiles.items() if x == cx), None)
        self.taps.append(cat)
        left = self.swallow.get(cat, 0)
        if left > 0:
            self.swallow[cat] = left - 1
            return                      # swallowed: selection does not change
        self.selected = cat

    def read(self, _frame=None):
        return _Reading(self.selected)


TILES = {"Luxuries": 424, "Medicine": 559, "Perfume": 694}
GOODS = {"Luxuries": "Bambara Groundnut", "Medicine": "Prunus Padus", "Perfume": "Geranium"}


def _run(panel, want="Bambara Groundnut"):
    from actions import barter_panel as bp
    tiles = [{"cx": x, "cy": 425, "tap_y": 475, "category": c, "status": "Abundant"}
             for c, x in TILES.items()]

    def read(_frame=None):
        r = panel.read()
        return _Reading(GOODS.get(r.selected_good)) if r.selected_good else _Reading(None)

    with patch.object(bp, "_tradable_tiles", return_value=tiles), \
         patch("capture.adb_capture.capture_screen", return_value=object()), \
         patch("vision.omniparser.parse_fast_cached", return_value=[]), \
         patch("actions.barter_reader.read_barter_panel", side_effect=read), \
         patch("actions.ui.tap_at", side_effect=panel.tap), \
         patch.object(bp, "_panel_matches", side_effect=lambda r, g, _rec: r.selected_good == g):
        return bp._select_trade_good(want)


class TheSwallowedTileIsTriedAgain(unittest.TestCase):
    def test_the_live_case_now_finds_the_good(self):
        # Exactly what happened: the first tile ate both taps, the others were fine.
        panel = _Panel(TILES, swallow={"Luxuries": 2})
        self.assertIs(_run(panel), True,
                      "the warm panel gives the swallowed tile another go, and it IS the good")
        self.assertGreater(panel.taps.count("Luxuries"), 2, "it went back for it")

    def test_a_tile_that_never_answers_leaves_the_row_unread(self):
        panel = _Panel(TILES, swallow={"Luxuries": 99})
        self.assertIsNone(_run(panel),
                          "None = the row is unread; False would claim the village lacks it")

    def test_a_good_genuinely_absent_is_still_reported_absent(self):
        panel = _Panel(TILES)                      # every tap lands
        self.assertIs(_run(panel, want="Box of Nutmeg"), False,
                      "every tile answered and none was it — that IS a verdict")

    def test_it_stops_as_soon_as_it_finds_the_good(self):
        panel = _Panel(TILES)
        self.assertIs(_run(panel), True)
        self.assertEqual(panel.taps, ["Luxuries"], "no need to try the rest")


if __name__ == "__main__":
    unittest.main()
