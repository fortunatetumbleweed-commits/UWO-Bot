"""Three tabs out of four is an incomplete READ, not a screen without the tab.

LIVE 2026-08-30, mid-mission for Hutu Village. `select_world_map_tab('explore')` had already
succeeded ninety seconds earlier and the village's base tab had been read off it. Then one
glance came back ['port', 'route', 'trade'] — 'explore' missing — and the function returned
False on the spot. The activity reported blocked, the runner had nothing left to ask for, and
a mission that was doing fine ended over a single flaky read of a strip that was plainly there.

The function's own retry loop exists for exactly this ("the map was not ready"). A SHORT row
gets another look; a COMPLETE row that lacks the tab is a different screen and stops at once.
"""
import unittest
from unittest.mock import patch

import actions.sail_actions as sail


class _Frame:
    width, height = 2400, 1080


def _row(*names):
    """The strip as `_world_map_tab_strip` returns it: (name, cx, cy, y2)."""
    return [(n, 400 + i * 300, 50, 70) for i, n in enumerate(names)]


class AShortRowIsLookedAtAgain(unittest.TestCase):
    def _run(self, rows, lit="explore"):
        """Drive select_world_map_tab with a scripted sequence of strip reads."""
        seen = iter(rows)
        last = [rows[-1]]

        def strip(_frame):
            try:
                last[0] = next(seen)
            except StopIteration:
                pass
            return last[0]

        with patch.object(sail, "_world_map_tab_strip", side_effect=strip), \
             patch.object(sail, "_TAB_SETTLE_S", 0), \
             patch("capture.adb_capture.capture_screen", return_value=_Frame()), \
             patch("actions.adb_actions.tap"), \
             patch.object(sail, "active_world_map_tab", return_value=None), \
             patch.object(sail, "selected_tab_index", return_value=0), \
             patch("vision.omniparser.get_omniparser") as gp:
            gp.return_value.yolo_available.return_value = True
            return sail.select_world_map_tab("explore")

    def test_a_missing_tab_in_a_short_row_is_retried_and_found(self):
        short = _row("port", "route", "trade")          # the read that killed the run
        full  = _row("explore", "port", "route", "trade")
        self.assertTrue(self._run([short, full, full, full, full]),
                        "the second look sees the tab that was always there")

    def test_a_complete_row_without_the_tab_stops_at_once(self):
        # Four tabs read, none of them 'explore' — a different screen. Waiting cannot help.
        complete = _row("alpha", "beta", "gamma", "delta")
        self.assertFalse(self._run([complete] * 5))


if __name__ == "__main__":
    unittest.main()
