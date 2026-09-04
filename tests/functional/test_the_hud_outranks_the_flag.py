"""The sea HUD prints the destination, so the goal must not guess at it.

`SailToGoal._destination_selected` is a CONCLUSION drawn earlier in the voyage, and CLAUDE.md
forbids trusting one: it cannot be checked by looking. The sea HUD CAN be — `Barcelona / ETA
1 d`, bottom-centre — so the same question has an observation to answer it.

Live 2026-08-26 is what the flag alone cost. Seville → Barcelona: the departure worked, a
post-tap check misread the departure cinematic as failure, the flag stayed False, and
`_handle_sea` re-opened the world map MID-VOYAGE and re-targeted the port the fleet had just
left. It sailed back, bought nothing, and started again. `frame_0020.png` from that very run
reads `Barcelona ETA 1d`.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain.goals.sail_to import SailPhase, SailToGoal


def _goal(destination="Barcelona"):
    g = SailToGoal(destination=destination)
    g._destination_selected = False
    return g


def _at_sea():
    return types.SimpleNamespace(state="sea", location="sea", port=None, frame=object())


class TheHudDecidesWhetherToReSelect(unittest.TestCase):

    def _run(self, *, hud_says):
        opened = []
        g = _goal()
        with patch("vision.region_detectors.sea_destination.bound_for",
                   return_value=hud_says), \
             patch.object(SailToGoal, "_action_open_world_map",
                          side_effect=lambda: opened.append(1) or "opened"), \
             patch.object(SailToGoal, "_action_sailing", side_effect=lambda s: "sailing"):
            out = g._handle_sea(_at_sea())
        return g, out, opened

    def test_a_fleet_already_bound_is_left_alone(self):
        g, out, opened = self._run(hud_says=True)
        self.assertEqual(out, "sailing")
        self.assertEqual(opened, [], "re-opening the world map mid-voyage is the bug")
        self.assertTrue(g._destination_selected)

    def test_a_fleet_bound_nowhere_still_selects_a_destination(self):
        """The check must not swallow the case it exists beside."""
        g, out, opened = self._run(hud_says=False)
        self.assertEqual(out, "opened")
        self.assertEqual(len(opened), 1)

    def test_an_unreadable_hud_changes_nothing(self):
        """A failed read is NOT 'no destination' — it is no answer, and the goal proceeds
        exactly as it did before this check existed."""
        opened = []
        g = _goal()
        with patch("vision.region_detectors.sea_destination.bound_for",
                   side_effect=RuntimeError("ocr down")), \
             patch.object(SailToGoal, "_action_open_world_map",
                          side_effect=lambda: opened.append(1) or "opened"), \
             patch.object(SailToGoal, "_action_sailing", side_effect=lambda s: "sailing"):
            out = g._handle_sea(_at_sea())
        self.assertEqual(out, "opened")

    def test_the_flag_alone_still_shortcuts(self):
        """When the goal already knows, the HUD is not consulted — no extra OCR per tick."""
        asked = []
        g = _goal()
        g._destination_selected = True
        with patch("vision.region_detectors.sea_destination.bound_for",
                   side_effect=lambda *a, **k: asked.append(1) or True), \
             patch.object(SailToGoal, "_action_sailing", side_effect=lambda s: "sailing"):
            g._handle_sea(_at_sea())
        self.assertEqual(asked, [])


class AgainstTheFrameFromTheFailedRun(unittest.TestCase):

    FRAME = "data/sessions/trace_barter_cmd_2026-08-26T18-14-16/frame_0020.png"

    def test_the_goal_would_not_have_re_navigated(self):
        import os
        if not os.path.exists(self.FRAME):
            self.skipTest("trace frame not available")
        from PIL import Image
        opened = []
        g = _goal("Barcelona")
        state = types.SimpleNamespace(state="sea", location="sea", port=None,
                                      frame=Image.open(self.FRAME))
        with patch.object(SailToGoal, "_action_open_world_map",
                          side_effect=lambda: opened.append(1) or "opened"), \
             patch.object(SailToGoal, "_action_sailing", side_effect=lambda s: "sailing"):
            out = g._handle_sea(state)
        self.assertEqual(out, "sailing")
        self.assertEqual(opened, [], "this is the exact frame the old code re-navigated on")


if __name__ == "__main__":
    unittest.main()
