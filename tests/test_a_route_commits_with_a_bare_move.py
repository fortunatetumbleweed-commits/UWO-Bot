# tests/test_a_route_commits_with_a_bare_move.py
#
# "The bot selected the route but did not tap the Move button, why is that" (user, 2026-09-03).
#
# Because a saved route's commit button says only `Move`, and `_find_destination_button`
# requires a VERB+NOUN pair — `Go to City`, `Move to Village`. Those are the port and village
# panels; the route panel's shorter label was never added. So:
#
#   destination_commit_control -> None
#   _commit_control_showing    -> False
#   classify                   -> route_list        (the rail is still visible behind the panel)
#   _on_route_list             -> "find the route and tap it" — already done
#
# and it re-tapped the row it had selected until the stall guard ended the mission, with 4,597
# Bambara Groundnut aboard and the route drawn on the map.
#
# THE NOUN IS NOT DECORATION. A bare `Move` is ALSO the sea-waypoint marker the game raises
# when a tap misses a port and lands on open water — `_has_destination_button_text` exists to
# tell those apart (sail_actions ~4537). So the word alone must never be enough. What makes it
# unambiguous is the ROUTE TAB: a saved route is selected and its panel is up, which is not a
# state a waypoint marker occurs in.
#
# Why this never showed before: every route sail that has EVER worked went through
# `actions/route_execution.py`, which finds the button with a plain substring match on "move".
# Across 183 recorded sessions, `sail_route done` appears zero times on the newer dispatcher
# path. The old implementation still runs on the resume path, which is exactly what hid it.

import unittest

from actions.sail_actions import _bare_move_control, destination_commit_control


def _el(label, x1, y1, x2, y2, etype="button"):
    from vision.omniparser import DetectedElement
    return DetectedElement(label=label, element_type=etype,
                           x1=x1, y1=y1, x2=x2, y2=y2, confidence=0.9)


# Measured on frame_0339 of trace_barter_cmd_2026-09-03T20-38-15.
_ROUTE_MOVE = _el("Move", 1028, 975, 1255, 1044)
_MY_LOCATION = _el("My Location", 35, 977, 228, 1040)
_INVEST = _el("Invest", 236, 977, 426, 1040)


class TheRoutePanelsButtonIsFound(unittest.TestCase):
    def test_the_bare_move_is_located(self):
        self.assertEqual(_bare_move_control([_ROUTE_MOVE]), (1141, 1009))

    def test_the_other_buttons_in_the_same_bar_are_not_it(self):
        """Both sit in the action bar on the very frame this was found on, which is why
        widening the verb list was not the fix."""
        self.assertIsNone(_bare_move_control([_MY_LOCATION, _INVEST]))

    def test_a_map_label_saying_move_is_not_a_button(self):
        """The sea-waypoint marker. Typed text, not a control."""
        self.assertIsNone(_bare_move_control([_el("Move", 1028, 975, 1255, 1044, "text")]))

    def test_a_move_above_the_action_bar_is_not_it(self):
        self.assertIsNone(_bare_move_control([_el("Move", 1028, 400, 1255, 470)]))

    def test_a_narrow_move_is_not_it(self):
        """A real commit control is wide; the route panel's measured 227px."""
        self.assertIsNone(_bare_move_control([_el("Move", 1028, 975, 1090, 1044)]))


class ItIsOptIn(unittest.TestCase):
    """The default must not change, or the sea-waypoint discrimination breaks."""

    def test_the_default_still_refuses_a_bare_move(self):
        self.assertIsNone(destination_commit_control(elements=[_ROUTE_MOVE]))

    def test_only_a_caller_that_asks_gets_it(self):
        self.assertEqual(
            destination_commit_control(elements=[_ROUTE_MOVE], allow_bare_move=True),
            (1141, 1009))

    def test_verb_noun_panels_are_untouched(self):
        """Bordeaux and Hutu both committed fine tonight; they must keep doing so."""
        for label in ("Go to City", "Move to Village", "Move to City"):
            with self.subTest(label=label):
                self.assertEqual(
                    destination_commit_control(elements=[_el(label, 1100, 980, 1400, 1040)]),
                    (1250, 1010))


class TheTabIsWhatDisambiguates(unittest.TestCase):
    def _classify(self, tab, elements):
        from unittest.mock import patch
        import brain.world_map_context as ctx
        from PIL import Image
        frame = Image.new("RGB", (2400, 1080))
        with patch("vision.omniparser.parse_fast_cached", return_value=elements), \
             patch.object(ctx, "_active_tab", return_value=tab), \
             patch.object(ctx, "_right_panel", return_value=None), \
             patch.object(ctx, "_commit_control_showing", return_value=False), \
             patch.object(ctx, "_search_box_showing", return_value=False, create=True):
            return ctx.classify(frame, elements=elements)

    def test_on_the_route_tab_a_bare_move_is_a_panel_awaiting_commit(self):
        got = self._classify("route", [_ROUTE_MOVE, _el("san to london", 40, 150, 450, 200)])
        self.assertEqual(got, "destination_panel")

    def test_on_the_port_tab_it_is_not(self):
        """A bare Move with no route selected is the sea-waypoint marker, and treating it as
        a commit would tap 'sail to this patch of open water'."""
        got = self._classify("port", [_ROUTE_MOVE])
        self.assertNotEqual(got, "destination_panel")


if __name__ == "__main__":
    unittest.main()
