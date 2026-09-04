"""The rail's search box is identified by where it sits AND by being a box.

`search_box_present` is the test for "is the destination list open", and it looked only at
POSITION: any element of any kind inside a window counted. Its docstring explains why position
rather than wording —

    AN EMPTY BOX READS 'Search'; A FILLED ONE READS WHAT YOU TYPED. Every test for an open
    list looked for the word, so typing into the box destroyed the evidence that the list was
    open.

— which is right, and stops one step short. Where alone is not what.

Live 2026-09-01 the world map opened with NO list on it, two port PINS landed in the window,
and the screen classified as `destination_list`. `_on_list` then tapped a search box that was
not there and typed 'Barc' into the map, twice, exhausting its typing budget; it fell through
to scrolling a list it had never opened and the leg failed six scrolls later:

    [sail] the course did not take: 'Barcelona' is not in the port list — looked, typed and
    scrolled

The same file states the rule two functions down, for the village list: "identified by WHAT IS
THERE ... not by where anything sits". Measured on the frames:

    the box, empty   'Search'  393x99      the box, filled  'svea'  384x63
    the pins          icon      95x112                      icon     65x84

Nearly three hundred pixels of margin, and the filled box is as wide as the empty one — which
is the property the docstring was reaching for: the one thing a query cannot change.
"""
import types
import unittest

from actions.sail_actions import (_SEARCH_BOX_MIN_W, _SEARCH_BOX_X, _SEARCH_BOX_Y,
                                  search_box_present)


def _el(cx, cy, w, h, kind="button", label=""):
    return types.SimpleNamespace(element_type=kind, label=label, cx=cx, cy=cy,
                                 x1=cx - w // 2, x2=cx + w // 2,
                                 y1=cy - h // 2, y2=cy + h // 2)


IN_X = (_SEARCH_BOX_X[0] + _SEARCH_BOX_X[1]) // 2
IN_Y = (_SEARCH_BOX_Y[0] + _SEARCH_BOX_Y[1]) // 2

# Measured off the live frames.
EMPTY_BOX = _el(302, 160, 393, 99, "button", "Search")
FILLED_BOX = _el(302, 141, 384, 63, "button", "svea")
PIN_A = _el(472, 125, 95, 112, "icon", "icon")
PIN_B = _el(557, 128, 65, 84, "icon", "icon")


class ABoxIsFoundWhateverItSays(unittest.TestCase):
    def test_the_empty_box(self):
        self.assertTrue(search_box_present(None, [EMPTY_BOX]))

    def test_the_box_holding_a_query(self):
        """The whole point of not keying on the word."""
        self.assertTrue(search_box_present(None, [FILLED_BOX]))

    def test_it_is_still_found_beside_other_things(self):
        self.assertTrue(search_box_present(None, [PIN_A, EMPTY_BOX, PIN_B]))


class PinsAreNotABox(unittest.TestCase):
    def test_the_live_failure(self):
        self.assertFalse(search_box_present(None, [PIN_A, PIN_B]),
                         "these two classified a bare map as an open list")

    def test_one_pin_alone_is_not_a_box(self):
        self.assertFalse(search_box_present(None, [PIN_A]))

    def test_a_wide_thing_outside_the_window_is_not_it(self):
        far = _el(IN_X, _SEARCH_BOX_Y[1] + 400, 393, 99, "button", "Search")
        self.assertFalse(search_box_present(None, [far]), "position still matters too")


class TheCutoffSitsInTheGap(unittest.TestCase):
    def test_it_is_above_every_pin_and_below_every_box(self):
        self.assertGreater(_SEARCH_BOX_MIN_W, 95, "the widest pin measured 95")
        self.assertLess(_SEARCH_BOX_MIN_W, 384, "the narrowest box measured 384")

    def test_a_pin_would_have_to_triple_in_width_to_fool_it(self):
        fat_pin = _el(IN_X, IN_Y, _SEARCH_BOX_MIN_W - 1, 112, "icon", "icon")
        self.assertFalse(search_box_present(None, [fat_pin]))


class TheClassifierFollows(unittest.TestCase):
    """`world_map_context` asks this question to answer "is the list open?"."""

    def _classify(self, elements):
        from unittest import mock
        from brain import world_map_context as ctx
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=elements):
            return ctx.classify(object(), elements=elements)

    def test_a_bare_map_is_not_a_destination_list(self):
        # Three tab words make it read as the map; the pins must not promote it to a list.
        tabs = [_el(700, 50, 100, 40, "button", t) for t in ("Port", "Explore", "Route")]
        self.assertNotEqual(self._classify(tabs + [PIN_A, PIN_B]), "destination_list")

    def test_an_open_list_still_is_one(self):
        tabs = [_el(700, 50, 100, 40, "button", t) for t in ("Port", "Explore", "Route")]
        self.assertEqual(self._classify(tabs + [EMPTY_BOX]), "destination_list")


if __name__ == "__main__":
    unittest.main()


class TheParseReturnsEitherTheBoxOrTheWordInIt(unittest.TestCase):
    """Both are the box. Live 2026-09-01, requiring the width alone rejected one of them.

    'Search' came back as a 393x99 BUTTON on one frame and as a 96x32 TEXT on another — the
    same field, parsed two ways. With only the width test, the second read as "no box", so a
    list that WAS open was judged closed; `_on_list` then re-tapped the rail, which toggles an
    open list SHUT, and the run opened and closed it on every tick:

        [world_map] the port list is not open — opening it before reading it
        [world_map] opening the port list — CALIBRATED point (69, 180)

    What actually separates the field from the pins is that a pin is an ICON. Width and the
    word are the two ways the field itself shows up.
    """

    WORD = _el(304, 144, 96, 32, "text", "Search")        # measured live
    BUTTON = _el(302, 160, 393, 99, "button", "Search")   # measured on frame_0120

    def test_the_box_parsed_as_a_button(self):
        self.assertTrue(search_box_present(None, [self.BUTTON]))

    def test_the_box_parsed_as_the_word_inside_it(self):
        self.assertTrue(search_box_present(None, [self.WORD]),
                        "this is what made an open list read as closed")

    def test_a_narrow_map_label_is_still_not_a_box(self):
        # 'Far Sev' measured 140 wide, 'asablanca' 167 — neither is wide, neither says search.
        for label, w in (("Far Sev", 140), ("asablanca", 167), ("Malaga", 120)):
            self.assertFalse(search_box_present(None, [_el(IN_X, IN_Y, w, 40, "text", label)]),
                             label)

    def test_a_pin_is_rejected_however_wide(self):
        """An icon is never the field, even one big enough to pass the width test."""
        fat = _el(IN_X, IN_Y, 400, 112, "icon", "icon")
        self.assertFalse(search_box_present(None, [fat]))
