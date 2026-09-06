"""A panel for another place is CLOSED, not merely refused.

Live 2026-09-06, the Seville leg. A stale Marseille Location Info panel was already open when
the world-map activity took control. Refusing to commit it was right — sailing from there
goes somewhere nobody chose — but nothing then closed it, so `sail_runner` met the identical
screen on its retry and the leg died:

    the open Location Info panel is not 'Seville' — not committing a departure we did not ask
    could not set a course for 'Seville' after 2 attempts

The branch's own comment said "hand back and let the map be searched again", which does not
follow from handing back: the search cannot run while the panel covers it.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.activities.world_map import _MAX_PANEL_CLOSES, ChooseDestination, WorldMapActivity
from brain.dispatcher import BLOCKED, WORKING


class TheWrongPanelIsClosedSoTheMapCanBeSearched(unittest.TestCase):

    def _activity(self, backs):
        return WorldMapActivity(panel_for_fn=lambda where: False,
                                back_fn=lambda: backs.append("back"))

    def test_it_closes_the_panel_and_hands_back_WORKING(self):
        backs = []
        act = self._activity(backs)
        res = act._on_location_info(ChooseDestination(where="Seville", kind="port"))
        self.assertEqual(res.status, WORKING)
        self.assertEqual(backs, ["back"], "the one action that changes the screen")
        self.assertEqual(res.observed["did"], "closed another place's panel")

    def test_it_does_NOT_commit_the_other_place(self):
        """The refusal this branch has always made, and must keep making."""
        committed = []
        act = WorldMapActivity(panel_for_fn=lambda where: False, back_fn=lambda: None,
                               commit_fn=lambda where: committed.append(where) or True)
        act._on_location_info(ChooseDestination(where="Seville", kind="port"))
        self.assertEqual(committed, [], "a departure nobody asked for")

    def test_a_panel_that_will_not_close_is_REPORTED_not_ground_at(self):
        backs = []
        act = self._activity(backs)
        goal = ChooseDestination(where="Seville", kind="port")
        for _ in range(_MAX_PANEL_CLOSES):
            self.assertEqual(act._on_location_info(goal).status, WORKING)
        res = act._on_location_info(goal)
        self.assertEqual(res.status, BLOCKED)
        self.assertEqual(len(backs), _MAX_PANEL_CLOSES, "bounded, not endless")

    def test_the_count_belongs_to_THIS_destination(self):
        """Guiding Principle #4 — the registry reuses one activity, so a count kept across
        goals reports the previous search's attempts as this one's."""
        backs = []
        act = self._activity(backs)
        for _ in range(_MAX_PANEL_CLOSES + 1):
            act._on_location_info(ChooseDestination(where="Seville", kind="port"))
        self.assertEqual(act._panel_closes, _MAX_PANEL_CLOSES)
        # `work()` resets the counters BEFORE it perceives, so a new destination has cleared
        # them by the time the real map read runs — which is all this asserts. Letting that
        # read fail on a stand-in frame keeps the test off the perception pipeline.
        try:
            act.work(ChooseDestination(where="Lisboa", kind="port"),
                     types.SimpleNamespace(state="world_map", frame=object()))
        except Exception:
            pass
        self.assertEqual(act._panel_closes, 0, "a new destination is a fresh search")


if __name__ == "__main__":
    unittest.main()
