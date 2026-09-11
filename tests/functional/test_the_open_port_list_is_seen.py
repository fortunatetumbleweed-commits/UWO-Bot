"""An open list must be SEEN, and a list that will not open must not become a different list.

Live 2026-09-08, frames 30-34 of trace_barter_cmd_2026-09-08T00-43-13. The port icon at
(66,171) opened the port list correctly — frame 32 shows it, search box and rows and all —
and the next tick failed to see it:

    world_map -> working {'did': 'opened the destination list'}
    _on_map
    'Samarai' not found in current world map view
    'Samarai' is not on screen — opening the list
    no icon rail detected — trying its CALIBRATED positions
    opening the port list — CALIBRATED point (70, 300) (attempt 4 of 2)

(70,300) is the GOODS icon. So the open port list was thrown away, the attempt bound printed
"4 of 2" without holding, and the leg stalled until the no-progress guard ended the mission.

TWO FAULTS, and this pins both.

WHY IT WAS NOT SEEN. `search_box_present` identifies the box by where it sits and how wide
it is. The map draws through that spot, so a label merges with the word:

    frame 30, list CLOSED : 'Kuching'   w=142
    frame 32, list OPEN   : 'KSearshg'  w=140      <- 'Kuching' bled over 'Search'

Same place, same width, only a garbled word between them. Neither test can separate those and
matching the garble would be guesswork — so the ROWS decide, which is what `_village_list_open`
has always keyed on. A list is a drawn column; a map is scattered labels.

WHY ANOTHER ICON IS WRONG (user, 2026-09-08: "if it can not see a list, it should not try
another icon, that is almost always wrong"). The rail's icons are not interchangeable
candidates for one thing — each opens a DIFFERENT list, and which one we want is known from
the goal. Tapping a second is not a retry, it is asking a different question.
"""
import os
import unittest

_STAGE = "tests/stage_suite/frames"


def _open(name):
    from PIL import Image
    p = f"{_STAGE}/{name}.png"
    if not os.path.exists(p):
        raise unittest.SkipTest(f"stage frame not available: {p}")
    return Image.open(p)


class TheListIsSeenByItsRows(unittest.TestCase):

    def test_an_open_port_list_is_seen(self):
        from actions.sail_actions import _port_list_open
        self.assertTrue(_port_list_open(_open("worldmap_port_list_open")))

    def test_a_bare_map_is_not_mistaken_for_one(self):
        from actions.sail_actions import _port_list_open
        self.assertFalse(_port_list_open(_open("worldmap_bare_no_list")))

    def test_the_search_box_alone_cannot_tell_them_apart(self):
        """The measurement the row rule exists for — asserted, not trusted."""
        from actions.sail_actions import search_box_present
        self.assertFalse(search_box_present(_open("worldmap_port_list_open")),
                         "if the box were readable here the rows would be belt and braces")
        self.assertFalse(search_box_present(_open("worldmap_bare_no_list")))


class ItNeverTapsADifferentIcon(unittest.TestCase):

    def _activity(self, taps):
        from brain.activities.world_map import WorldMapActivity
        act = WorldMapActivity(capture_fn=lambda: _open("worldmap_bare_no_list"),
                               tap_fn=lambda x, y: taps.append((x, y)))
        return act

    def test_a_list_that_will_not_open_is_reported_not_worked_around(self):
        import types
        from brain.activities.world_map import _MAX_LIST_TAPS
        taps = []
        act = self._activity(taps)
        goal = types.SimpleNamespace(kind="port", where="Samarai")
        for _ in range(5):
            act._open_list(goal)
        self.assertLessEqual(len(taps), _MAX_LIST_TAPS,
                             f"kept tapping: {taps}")
        self.assertEqual(len(set(taps)), 1,
                         f"tapped more than one rail position: {set(taps)}")


if __name__ == "__main__":
    unittest.main()
