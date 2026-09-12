"""The port overworld and the sea carry the SAME panel, and it is found the same way.

The fixtures below are not invented. They are the OmniParser boxes read off two frames of
`data/sessions/trace_barter_cmd_2026-09-11T13-47-33` — frame 0000 at London and frame 0588 in
the Atlantic — transcribed so the test carries the live geometry without needing the images
(the frames themselves are gitignored; see tests/stage_suite/README.md).

What the two readings show, and what this locks down:

    part         PORT (frame 0000)        SEA (frame 0588)
    tabs         y[129,202], 4 boxes      y[129,200], 4 boxes   <- centres agree to ~2px
    minimap      derived [202,397]        derived [200,399]
    season row   y[397,439]               y[399,438]            <- THE ANCHOR
    list         Harbor, Market, …        Berber Village, …
    gauges       none                     4 cells attached at the left

Three bugs live in this element being five separate constants. `CHROME_RIGHT_PANEL_REGION`
starts at 2050 when the panel starts at 1862 and runs to the frame edge when the panel ends at
2267. On 2026-09-01 ENTER_BUILDING at sea cycled "the minimap's four tab icons" because the
strip exists in both worlds and the code knew only the port's. And the obstruction classifier
has read the whole panel as a POPUP.
"""
from __future__ import annotations

import types
import unittest

from vision.region_detectors.overworld_panel import detect_overworld_panel


def _el(label, x1, y1, x2, y2, kind="icon"):
    return types.SimpleNamespace(label=label, element_type=kind,
                                 x1=x1, y1=y1, x2=x2, y2=y2)


_FRAME = types.SimpleNamespace(width=2400, height=1080)

# ── London, frame 0000 — the port overworld ──────────────────────────────────
PORT = [
    # the account bar along the very top: small icons in a row, and NOT the tab strip
    _el("'200;", 1772, 0, 1881, 107, "button"),
    _el("icon", 1877, 0, 1966, 111),
    _el("icon", 1967, 1, 2067, 110),
    _el("icon", 2075, 2, 2158, 109),
    _el("20.06348", 1778, 60, 1872, 86, "text"),
    # the tab strip: Tasks · Buildings · Players · location pin
    _el("icon", 1864, 129, 1969, 202),
    _el("icon", 1967, 132, 2066, 198),
    _el("icon", 2064, 133, 2162, 201),
    _el("icon", 2159, 133, 2263, 200),
    # the minimap, which the parse returns as ONE tall box over the strip's y-range too
    _el("10", 1862, 134, 2267, 425, "button"),
    _el("icon", 2191, 322, 2265, 398),              # the globe, inside the minimap
    # the season row
    _el("Aug", 2071, 397, 2134, 439, "text"),
    _el("Summer", 1942, 402, 2050, 430, "text"),
    _el("00.10", 2170, 402, 2244, 432, "text"),
    # the list
    _el("Harbor", 1863, 437, 2267, 505, "button"),
    _el("Market", 1863, 503, 2267, 576, "button"),
]

# ── The Atlantic, frame 0588 — at sea ────────────────────────────────────────
SEA = [
    _el("icon", 2166, 2, 2264, 116),
    _el("icon", 2079, 4, 2156, 114),
    _el("icon", 1777, 6, 1873, 92),
    _el("icon", 1879, 7, 1959, 91),
    _el("icon", 1978, 8, 2060, 91),
    # the gauge strip attached to the panel's left edge: tide, speed, wind, current
    _el("icon", 1778, 129, 1865, 200),
    _el("27.6", 1780, 197, 1864, 277, "button"),
    _el("icon", 1781, 274, 1864, 356),
    _el("14", 1780, 354, 1865, 435, "button"),
    # the tab strip: Tasks · Ports · fleets · ships
    _el("icon", 1864, 129, 1969, 200),
    _el("10", 1968, 132, 2069, 198, "button"),
    _el("icon", 2064, 134, 2160, 197),
    _el("icon", 2159, 133, 2265, 200),
    # the minimap
    _el("26.04,-14,89", 1862, 201, 2267, 430, "button"),
    # the season row — the last cell is a day phase here, a clock at a port
    _el("Aug", 2072, 399, 2132, 438, "text"),
    _el("Night", 2169, 401, 2245, 437, "text"),
    _el("Summer", 1944, 402, 2050, 430, "text"),
    # the list
    _el("Berber Village", 1864, 435, 2266, 540, "button"),
    _el("Approx. 65.7km", 2045, 493, 2259, 533, "text"),
    _el("d Las Palmas", 1861, 536, 2265, 645, "button"),
]


class TheSamePanelIsFoundInBothWorlds(unittest.TestCase):

    def test_the_port(self):
        p = detect_overworld_panel(_FRAME, PORT)
        self.assertIsNotNone(p)
        self.assertEqual(len(p.tabs), 4)
        self.assertEqual([r[0] for r in p.rows], ["Harbor", "Market"])

    def test_the_sea(self):
        p = detect_overworld_panel(_FRAME, SEA)
        self.assertIsNotNone(p)
        self.assertEqual(len(p.tabs), 4)
        self.assertEqual([r[0] for r in p.rows], ["Berber Village", "d Las Palmas"])

    def test_THE_TABS_LAND_IN_THE_SAME_PLACE(self):
        """The point of the class. Same element, same geometry, different pictures on it —
        so the strip must be found by the same rule and not by a per-world constant."""
        port = detect_overworld_panel(_FRAME, PORT)
        sea = detect_overworld_panel(_FRAME, SEA)
        for a, b in zip(port.tab_points, sea.tab_points):
            self.assertLess(abs(a[0] - b[0]), 6, f"tab x {a} vs {b}")
            self.assertLess(abs(a[1] - b[1]), 6, f"tab y {a} vs {b}")

    def test_the_panel_body_spans_agree(self):
        """The BODY is the same rectangle in both worlds — strip, minimap, season row, list."""
        port = detect_overworld_panel(_FRAME, PORT)
        sea = detect_overworld_panel(_FRAME, SEA)
        self.assertLess(abs(port.minimap[0] - sea.minimap[0]), 6)
        self.assertLess(abs(port.minimap[2] - sea.minimap[2]), 6)
        self.assertLess(abs(port.box[2] - sea.box[2]), 6)

    def test_the_OUTER_box_differs_by_the_gauge_strip(self):
        """And only by that. The sea hangs a column off the panel's left edge; the port
        does not, so the sea's box reaches further left by exactly the strip's width."""
        port = detect_overworld_panel(_FRAME, PORT)
        sea = detect_overworld_panel(_FRAME, SEA)
        self.assertEqual(port.box[0], port.minimap[0])
        self.assertEqual(sea.box[0], min(g[0] for g in sea.gauges))
        self.assertLess(sea.box[0], port.box[0])


class TheGaugeStripBelongsToThePanel(unittest.TestCase):
    """A vertical icon strip attached to the panel's top-left, drawn at sea only (user,
    2026-09-11). Read off frame 0588 top to bottom: HW in red (tide state), a ship over 27.6
    (speed), a windsock over 4 ◄ (wind), waves over 1 ◄ (current).

    It was previously EXCLUDED as if it were foreign, which is what made the tab search need
    containment: the cells are the same size and shape as tabs and sit immediately beside
    them, so only which side of the seam they fall on tells them apart.
    """

    def test_four_cells_at_sea(self):
        p = detect_overworld_panel(_FRAME, SEA)
        self.assertEqual(len(p.gauges), 4)
        self.assertTrue(p.at_sea)

    def test_none_at_a_port(self):
        p = detect_overworld_panel(_FRAME, PORT)
        self.assertEqual(p.gauges, ())
        self.assertFalse(p.at_sea)

    def test_they_are_stacked_top_to_bottom(self):
        p = detect_overworld_panel(_FRAME, SEA)
        tops = [g[1] for g in p.gauges]
        self.assertEqual(tops, sorted(tops))

    def test_ATTACHED_is_the_test_not_a_position(self):
        """Each cell's RIGHT edge meets the panel's LEFT edge. That is a relation between two
        things read off the frame, so it survives the shift that moves both."""
        p = detect_overworld_panel(_FRAME, SEA)
        for g in p.gauges:
            self.assertLess(abs(g[2] - p.minimap[0]), 14, g)

    def test_the_speed_tile_is_the_second_cell(self):
        """`vision.sea_hud.read_speed` locates this tile from the minimap with four offset
        constants and falls back to an absolute crop. Measured on frame 0588 that fallback,
        (1915,243,1975,283), lands INSIDE the minimap rather than on the strip."""
        p = detect_overworld_panel(_FRAME, SEA)
        self.assertEqual(p.gauges[1], (1780, 197, 1864, 277))


class TheSeasonRowIsTheAnchor(unittest.TestCase):

    def test_it_is_the_cells_on_that_line_not_everything_crossing_it(self):
        """The minimap is one tall box spanning y[134,425] at a port, so 'anything whose band
        contains this line' swallowed it, the row came back 300px tall, and the tab search
        then ran above the wrong line and returned the ACCOUNT BAR as the strip."""
        p = detect_overworld_panel(_FRAME, PORT)
        self.assertLess(p.season_row[3] - p.season_row[1], 60, p.season_row)

    def test_THE_ACCOUNT_BAR_IS_NOT_THE_TAB_STRIP(self):
        """Both are rows of small icons on the right. The strip is the LOWEST such row above
        the minimap; the account bar sits at the top of the screen."""
        p = detect_overworld_panel(_FRAME, PORT)
        self.assertTrue(all(t.box[1] > 120 for t in p.tabs),
                        [t.box for t in p.tabs])

    def test_no_season_row_means_no_panel(self):
        """A building interior has no such panel, and saying so is a real answer."""
        self.assertIsNone(detect_overworld_panel(_FRAME, [
            _el("Purchase", 1900, 100, 2300, 200, "button"),
            _el("Sell", 1900, 300, 2300, 400, "button"),
        ]))


class TheBoxesAreDerivedNotRemembered(unittest.TestCase):

    def test_the_minimap_sits_between_the_strip_and_the_season_row(self):
        p = detect_overworld_panel(_FRAME, PORT)
        self.assertEqual(p.minimap[1], max(t.box[3] for t in p.tabs))
        self.assertEqual(p.minimap[3], p.season_row[1])

    def test_A_SHIFTED_PANEL_IS_STILL_FOUND(self):
        """The camera-cutout offset moves everything: the same panel has rendered ~110px
        apart between two sessions. Nothing here may be pinned to a y."""
        shifted = [_el(e.label, e.x1, e.y1 + 110, e.x2, e.y2 + 110, e.element_type)
                   for e in PORT]
        p = detect_overworld_panel(_FRAME, shifted)
        self.assertIsNotNone(p)
        self.assertEqual(len(p.tabs), 4)
        self.assertEqual([r[0] for r in p.rows], ["Harbor", "Market"])

    def test_nothing_runs_past_the_bottom_of_the_frame(self):
        p = detect_overworld_panel(_FRAME, PORT)
        self.assertLessEqual(p.box[3], 1080)


class TheAnchorSurvivesHowTheParseMangesIt(unittest.TestCase):
    """Three ways the season row comes back that an exact match on a lone word did not
    survive. Measured over 120 frames of the September traces BEFORE any of this ran live:
    the port was found on 57% and the sea on 77%. All three are now covered, and every
    remaining miss is a frame with no panel on it — a blank loader, or a player-info card the
    family classifier mislabelled.
    """

    def test_THE_TROPICS_HAVE_THEIR_OWN_SEASONS(self):
        """`Wet Season Oct` off Hutu Village. The game names the season by LATITUDE, so a
        list of the four temperate ones is an assumption written down — and it cost 23% of
        at-sea frames."""
        els = [e for e in SEA if e.label != "Summer"]
        els.append(_el("Wet Season Oct", 1944, 402, 2130, 448, "text"))
        p = detect_overworld_panel(_FRAME, els)
        self.assertIsNotNone(p)
        self.assertEqual(len(p.tabs), 4)

    def test_a_truncated_season_still_anchors(self):
        """Off the same village it came back as just `Dry`."""
        els = [e for e in SEA if e.label != "Summer"]
        els.append(_el("Dry", 1944, 402, 2010, 430, "text"))
        self.assertIsNotNone(detect_overworld_panel(_FRAME, els))

    def test_THE_MERGED_HEAD_is_not_a_cell(self):
        """The parse swallows the tab strip, the minimap and the season row into ONE box and
        labels it with the season — 29 of 66 anchors sampled. Its BOTTOM edge is the row's
        bottom; treating it as a cell padded a 314px box into a 577px 'row' spanning the
        whole frame, and every box downstream was computed from that."""
        els = [e for e in PORT if e.label not in ("Summer", "Aug", "00.10", "10")]
        els.append(_el("Spring", 1864, 128, 2372, 442, "button"))
        p = detect_overworld_panel(_FRAME, els)
        self.assertIsNotNone(p)
        self.assertLess(p.season_row[3] - p.season_row[1], 60, p.season_row)
        self.assertEqual([r[0] for r in p.rows], ["Harbor", "Market"])

    def test_THE_ROW_IS_CONTIGUOUS_WITH_THE_ANCHOR(self):
        """Level with it is not enough. Dropping the right-of-frame prescreen was right — it
        was an absolute position — but it let anything at the season's height join the row: at
        one port a box on the far left came in and the span started at x 335 instead of 1862,
        so no tabs and no rows were found and the panel was thrown away."""
        els = list(PORT) + [_el("Language", 335, 400, 520, 432, "text")]
        p = detect_overworld_panel(_FRAME, els)
        self.assertIsNotNone(p)
        self.assertGreater(p.season_row[0], 1800, p.season_row)

    def test_a_season_word_with_no_panel_around_it_is_refused(self):
        """The substring test can hit text that is not the row, so the STRUCTURE decides: a
        real anchor has a tab strip above it or a list below it."""
        self.assertIsNone(detect_overworld_panel(_FRAME, [
            _el("Summer Festival is here!", 400, 500, 900, 540, "text"),
        ]))


class ItReportsStructureNotMeaning(unittest.TestCase):

    def test_it_does_not_name_the_tabs(self):
        """Which tab is Buildings is per-world and belongs to that world's context module.
        Stated as a test so the port's vocabulary never leaks into the shared element."""
        import inspect

        from vision.region_detectors import overworld_panel

        body = inspect.getsource(overworld_panel).split('"""', 2)[-1]
        for word in ("buildings", "players", "tasks"):
            self.assertNotIn(f'"{word}"', body.lower(),
                             f"the panel should not know about {word!r}")


if __name__ == "__main__":
    unittest.main()
