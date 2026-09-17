"""The search box and its list are ONE component, so they are detected as one.

THE FACT (user, 2026-09-16): *"the list and box are one component, they always show
together, the list can be empty if everything is filtered out, but it is still there."*

WHAT IT COST TO ASK TWICE. `_village_list_open` was `search_box_present(...) and any village
row`, so an unlucky parse of EITHER half denied the whole rail. Both halves have now failed
live, in opposite directions:

  * the ROW half, 2026-08-24 — a search narrowed to ONE result read as "no list", so the
    caller hunted for the icon again and toggled the panel shut.
  * the BOX half, 2026-09-16 at Svear — the typed query replaced the field in the parse, so
    the rail read as closed from the moment the search SUCCEEDED. Measured on the frames:

        319  before the box was tapped   box=True   list_open=True
        321  AT input_text               box=True   list_open=True
        322  after typing                box=False  list_open=False
        323  AT the 1st icon re-tap      box=False  list_open=False

    Empty, the field parses as a 386-wide BUTTON. Holding 'svea', a 64-wide TEXT. The width
    test kept the first and threw the second. The mission died one leg from the village with
    'Svear Village' on screen, tapping the icon four times at a rail already open.

WHY THE HUTU RUN SURVIVED THE SAME BUG: parsing luck. On its frame the filled box came back
386 wide, on Svear's 64 — same box, same place, same kind of query.

WHY EARLIER SVEAR RUNS SURVIVED IT: a different accident. The screen classified as MAP_OPEN,
which routes to `_on_map` — a handler that just looks for the place and taps it, with no gate
about which list is open. It found the filtered row at (157,200) every time. The classifier
becoming MORE right about the screen is what removed the safety net.
"""
from __future__ import annotations

import types
import unittest

from vision.region_detectors.destination_rail import detect_destination_rail


class _Frame:
    width, height = 2400, 1080


def _el(cx, cy, w, h, kind="button", label=""):
    return types.SimpleNamespace(element_type=kind, label=label, cx=cx, cy=cy,
                                 x1=cx - w // 2, x2=cx + w // 2,
                                 y1=cy - h // 2, y2=cy + h // 2)


# Measured off the live frames named in the docstring.
CLOSE_X      = _el(522, 142,  52,  60, "icon", "icon")
EMPTY_FIELD  = _el(302, 141, 386,  60, "button", "Search")
FILLED_WIDE  = _el(302, 142, 386,  60, "button", "hutu")     # Hutu's lucky parse
FILLED_NARROW= _el(302, 146,  64,  24, "text",   "svea")     # Svear's unlucky one
PIN_A        = _el(472, 125,  95, 112, "icon", "icon")       # map port pins, 2026-09-01
PIN_B        = _el(557, 128,  65,  84, "icon", "icon")


class TheRailIsFoundHoweverItsBoxParses(unittest.TestCase):

    def test_the_empty_rail(self):
        self.assertIsNotNone(detect_destination_rail(_Frame(), [EMPTY_FIELD, CLOSE_X]))

    def test_A_QUERY_PARSED_AS_BARE_TEXT_IS_STILL_THE_RAIL(self):
        """The Svear frame. This is the whole reason the detector exists."""
        rail = detect_destination_rail(_Frame(), [FILLED_NARROW, CLOSE_X])
        self.assertIsNotNone(rail)
        self.assertEqual(rail.typed_query, "svea")

    def test_the_same_box_parsed_wide_is_the_same_rail(self):
        """Hutu's frame. Identical screen, luckier parse — and it must not matter."""
        rail = detect_destination_rail(_Frame(), [FILLED_WIDE, CLOSE_X])
        self.assertIsNotNone(rail)
        self.assertEqual(rail.typed_query, "hutu")

    def test_an_empty_box_reports_no_query(self):
        rail = detect_destination_rail(_Frame(), [EMPTY_FIELD, CLOSE_X])
        self.assertEqual(rail.typed_query, "")


class ARailNeedsBothHalvesOfItsHead(unittest.TestCase):
    """Not the old conjunction returning. That one demanded the field be WIDE and the rows be
    SEVERAL. These two are only asked to exist."""

    def test_MAP_PINS_ARE_NOT_A_RAIL(self):
        """Two port pins are icons in the same band as the close-X. On 2026-09-01 they
        classified a bare map as a list and the bot typed 'Barc' into the map, twice."""
        self.assertIsNone(detect_destination_rail(_Frame(), [PIN_A, PIN_B]))

    def test_a_field_with_no_dismiss_control_is_not_a_rail(self):
        self.assertIsNone(detect_destination_rail(_Frame(), [EMPTY_FIELD]))

    def test_the_bare_map_is_not_a_rail(self):
        tabs = [_el(858, 52, 100, 40, "button", t) for t in ("Port", "Explore", "Route")]
        self.assertIsNone(detect_destination_rail(_Frame(), tabs))


class ARowlessRailIsStillARail(unittest.TestCase):
    """THE CASE THE OLD TEST COULD NOT EXPRESS. "There is no rail" and "the rail is open and
    nothing matched" both came back False, and they call for opposite responses — only the
    first justifies tapping the icon open."""

    def test_a_query_that_matches_nothing_still_finds_the_rail(self):
        rail = detect_destination_rail(_Frame(), [_el(302, 146, 70, 24, "text", "zzzz"),
                                                  CLOSE_X])
        self.assertIsNotNone(rail)
        self.assertEqual(rail.typed_query, "zzzz")


class OneBadElementDoesNotBlindIt(unittest.TestCase):

    def test_an_element_without_coordinates_is_skipped(self):
        """Reading `e.cx` straight off would raise, and the guard would turn that into
        'no rail' — going blind on a malformed neighbour is the wrong failure."""
        junk = types.SimpleNamespace(label="junk", element_type="text")
        self.assertIsNotNone(detect_destination_rail(_Frame(), [junk, EMPTY_FIELD, CLOSE_X]))


if __name__ == "__main__":
    unittest.main()


class TheActivityAsksTheRailAndTheTab(unittest.TestCase):
    """Identity comes from the LIT TAB, not from the rail's contents. The tab was read
    correctly on every tick of the failing loop, reporting Explore each time, while the
    contents test said the list was shut."""

    def _open_for(self, path, kind):
        from PIL import Image

        import brain.activities.world_map as W

        act = W.WorldMapActivity()
        act._frame = lambda: Image.open(path)
        return act._list_is_open_for(kind)

    S16 = "data/sessions/trace_barter_cmd_2026-09-16T19-36-05"
    S15 = "data/sessions/trace_barter_cmd_2026-09-15T16-53-46"

    def setUp(self):
        import pathlib

        if not pathlib.Path(f"{self.S16}/frame_0323.png").exists():
            self.skipTest("session frames are not in the repo")

    def test_THE_FRAME_THAT_ENDED_THE_RUN_READS_OPEN(self):
        self.assertTrue(self._open_for(f"{self.S16}/frame_0323.png", "village"))

    def test_the_unfiltered_rail_reads_open(self):
        self.assertTrue(self._open_for(f"{self.S16}/frame_0016.png", "village"))

    def test_the_bare_map_reads_shut(self):
        self.assertFalse(self._open_for(f"{self.S16}/frame_0012.png", "village"))

    def test_A_PORT_RAIL_IS_NOT_A_VILLAGE_RAIL(self):
        """The tab decides, so the same rail answers differently for the two kinds."""
        self.assertTrue(self._open_for(f"{self.S15}/frame_0055.png", "port"))
        self.assertFalse(self._open_for(f"{self.S15}/frame_0055.png", "village"))
