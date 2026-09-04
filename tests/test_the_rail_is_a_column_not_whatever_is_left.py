"""The left icon rail is a COLUMN. Anything else near the edge is the map showing through.

The rail's icons carry no label — OmniParser returns bare 'icon's — so position is the only
identity they have, and `_explore_left_icons` said so in its docstring:

    A COLUMN of similar icons at the frame's left edge — the same structural test the port
    tab strip uses, turned on its side.

The code did no such test. It filtered `element_type == "icon"` inside a box and returned
whatever was there. On a map panned so Iberia sits at the left edge, PORT PINS qualify.

Live 2026-09-01, with no rail on screen at all, it returned

    [(13, 222), (25, 275), (190, 341), (40, 530)]

`_open_list` tapped the first. It was FARO's flag — box (0,198)-(27,247), half off the frame,
its label "Far" reading right beside it. Faro's City Info panel opened, the commit control was
found on it and pressed, and the game asked "Moving to Faro after Auto Supply. Continue?" for
a fleet that had asked for Barcelona.

Two properties separate them, and the numbers are not close:

    real rail    cx = 70, 70, 71     spread 1     x1 = 39, 37, 36
    the pins     cx = 13, 25, 190,40 spread 177   x1 =  0,  0, 95,  8

A rail is DRAWN at one x. And a control is drawn wholly on screen — only a port sliding off
the edge of the map is clipped to x1 = 0.
"""
import types
import unittest

from actions.sail_actions import (_RAIL_COLUMN_TOL_PX, _RAIL_NOT_CLIPPED_PX,
                                  _explore_left_icons)


def _icon(cx, cy, x1=None, w=64, h=90):
    """An OmniParser-shaped icon element."""
    x1 = cx - w // 2 if x1 is None else x1
    return types.SimpleNamespace(element_type="icon", label="icon",
                                 cx=cx, cy=cy, x1=x1, y1=cy - h // 2,
                                 x2=x1 + w, y2=cy + h // 2)


# Measured off the live frames.
RAIL = [_icon(70, 184, x1=39, w=63, h=93),
        _icon(70, 301, x1=37, w=66, h=84),
        _icon(71, 536, x1=36, w=70, h=100)]

PINS = [_icon(13, 222, x1=0, w=27, h=49),      # Faro, clipped by the frame edge
        _icon(25, 275, x1=0, w=50, h=57),
        _icon(190, 341, x1=95, w=191, h=66),
        _icon(40, 530, x1=8, w=65, h=55)]


class ARailIsFound(unittest.TestCase):
    def test_the_real_rail_still_reads(self):
        self.assertEqual(_explore_left_icons(None, RAIL),
                         [(70, 184), (70, 301), (71, 536)])

    def test_it_comes_back_top_to_bottom(self):
        got = _explore_left_icons(None, list(reversed(RAIL)))
        self.assertEqual([cy for _cx, cy in got], sorted(cy for _cx, cy in got))


class TheMapIsNotARail(unittest.TestCase):
    def test_the_live_failure_yields_nothing(self):
        self.assertEqual(_explore_left_icons(None, PINS), [],
                         "tapping the first of these opened Faro")

    def test_faros_pin_is_never_the_answer(self):
        self.assertNotIn((13, 222), _explore_left_icons(None, PINS))

    def test_a_lone_icon_at_the_edge_is_not_a_strip(self):
        self.assertEqual(_explore_left_icons(None, [_icon(70, 184, x1=39)]), [])

    def test_scattered_icons_are_not_a_column(self):
        scattered = [_icon(40, 200, x1=8), _icon(190, 341, x1=95), _icon(120, 500, x1=88)]
        self.assertEqual(_explore_left_icons(None, scattered), [])


class TheTwoTestsAreIndependent(unittest.TestCase):
    """Either property alone rejects the pins; keeping both is not redundancy for its own
    sake, because each answers a different way of being wrong."""

    def test_a_clipped_icon_is_dropped_even_in_a_column(self):
        # A port sliding off the map can line up with the rail by chance. It is still not
        # a control: controls are drawn wholly on screen.
        with_clipped = RAIL + [_icon(70, 420, x1=0, w=27, h=49)]
        self.assertNotIn((70, 420), _explore_left_icons(None, with_clipped))

    def test_unclipped_icons_that_do_not_line_up_are_dropped(self):
        self.assertEqual(_explore_left_icons(None, [_icon(40, 200, x1=8),
                                                    _icon(150, 400, x1=118)]), [])

    def test_the_column_tolerance_is_wide_against_the_measurement(self):
        # The rail's own spread is 1px; the nearest pins sit 12 apart. The cutoff has room
        # on both sides, so it is not a number tuned to one capture.
        self.assertGreaterEqual(_RAIL_COLUMN_TOL_PX, 3)
        self.assertLessEqual(_RAIL_COLUMN_TOL_PX, 20)
        self.assertLess(_RAIL_NOT_CLIPPED_PX, 36, "the real rail starts at x1=36")


class TheBiggestColumnWins(unittest.TestCase):
    def test_a_stray_pair_does_not_beat_the_rail(self):
        got = _explore_left_icons(None, RAIL + [_icon(150, 250, x1=118),
                                                _icon(152, 600, x1=120)])
        self.assertEqual(got, [(70, 184), (70, 301), (71, 536)])


if __name__ == "__main__":
    unittest.main()
