"""Whoever acts on a perception acts on the pixels it was read from.

`_refined_state` has always done `frame=getattr(res, "frame", None)`, and `PerceiveResult` had
no `frame` field — so it read None every time. Every activity that needed pixels captured its
own, milliseconds after the dispatcher's: the dispatcher classified frame A, routed on A, and
handed the activity a state derived from A, which then acted on frame B.

The screen that was routed on and the screen that was acted on were not the same screen. That
is the failure "centralize the observation" exists to prevent (Guiding Principle #1), and it
was structural rather than accidental — no test could catch it, because the plumbing was
written and simply carried nothing.
"""
import types
import unittest
from unittest import mock

from PIL import Image

FRAME = Image.new("RGB", (2400, 1080))


def _perceived(frame=FRAME, where="sea"):
    return types.SimpleNamespace(
        to_location_dict=lambda: {"location": where, "port": None, "detail": ""},
        sub_menu=None, scene_type=None, frame=frame)


class ThePerceptionCarriesItsFrame(unittest.TestCase):
    def test_the_result_has_a_frame_field(self):
        from brain.perceive import PerceiveResult
        self.assertIn("frame", PerceiveResult.__dataclass_fields__)

    def test_the_frame_is_not_part_of_what_makes_two_readings_equal(self):
        from brain.perceive import PerceiveResult
        a = PerceiveResult(state="sea", port=None, detail="", frame=FRAME)
        b = PerceiveResult(state="sea", port=None, detail="", frame=None)
        self.assertEqual(a, b)

    def test_it_does_not_appear_in_the_repr(self):
        """A log line with a PIL image in it helps nobody."""
        from brain.perceive import PerceiveResult
        self.assertNotIn("Image", repr(PerceiveResult(state="sea", port=None, detail="",
                                                      frame=FRAME)))

    def test_a_correction_keeps_the_frame_it_corrected(self):
        from brain.human_escalation import _build_corrected_result
        from brain.perceive import PerceiveResult

        original = PerceiveResult(state="sea", port=None, detail="", frame=FRAME)
        fixed = _build_corrected_result({"state": "village"}, original)
        self.assertIs(fixed.frame, FRAME, "a correction is about the SAME screen")


class TheStateHandedToAnActivityCarriesIt(unittest.TestCase):
    def test_refined_state_passes_the_frame_through(self):
        import brain.run_goal as rg

        with mock.patch("brain.perceive.perceive", return_value=_perceived()):
            state = rg._refined_state()
        self.assertIs(state.frame, FRAME)

    def test_an_activity_uses_it_instead_of_capturing_its_own(self):
        """Measured: one extra capture per SeaActivity tick before, none after."""
        import capture.adb_capture as cap
        from brain.activities.sea import ArriveAshore, SeaActivity

        captures = {"n": 0}

        def _counted(*a, **k):
            captures["n"] += 1
            return FRAME.copy()

        state = types.SimpleNamespace(state="sea", port=None, frame=FRAME)
        with mock.patch.object(cap, "capture_screen", _counted), \
             mock.patch("actions.sail_actions.capture_screen", _counted), \
             mock.patch("actions.sail_actions.read_sea_hud",
                        return_value={"supply_days": 9, "eta_days": 5}), \
             mock.patch("vision.sea_hud.read_speed", return_value=8.0):
            SeaActivity().work(ArriveAshore("trip"), state)

        self.assertEqual(captures["n"], 0,
                         "the activity captured a second frame the dispatcher already had")

    def test_a_missing_frame_still_falls_back(self):
        """A state with no frame must still work — the fallback is what kept the bot running
        while the plumbing carried nothing."""
        import capture.adb_capture as cap
        from brain.activities.sea import ArriveAshore, SeaActivity

        captures = {"n": 0}

        def _counted(*a, **k):
            captures["n"] += 1
            return FRAME.copy()

        state = types.SimpleNamespace(state="sea", port=None, frame=None)
        with mock.patch.object(cap, "capture_screen", _counted), \
             mock.patch("actions.sail_actions.capture_screen", _counted), \
             mock.patch("actions.sail_actions.read_sea_hud",
                        return_value={"supply_days": 9, "eta_days": 5}), \
             mock.patch("vision.sea_hud.read_speed", return_value=8.0):
            res = SeaActivity().work(ArriveAshore("trip"), state)

        self.assertGreaterEqual(captures["n"], 1)
        self.assertIsNotNone(res)


class HelpersThatTakeNoStateStillGetTheFrame(unittest.TestCase):
    """The village's handlers are called with (self, goal) and never see `state`, and most of
    the world map's helpers take no argument at all. Without a per-tick copy each one captured
    a fresh screen — several captures inside one tick, every one a DIFFERENT screen from the
    one that routed us there."""

    def _captures(self, activity, goal, frame, extra=()):
        import capture.adb_capture as cap

        n = {"c": 0}

        def _counted(*a, **k):
            n["c"] += 1
            return FRAME.copy()

        state = types.SimpleNamespace(state=self.WHERE, location=self.WHERE,
                                      frame=frame, port=None)
        patches = [mock.patch.object(cap, "capture_screen", _counted),
                   mock.patch("actions.sail_actions.capture_screen", _counted),
                   mock.patch("vision.omniparser.parse_fast_cached", return_value=[])]
        patches += list(extra)
        for p in patches:
            p.start()
        try:
            activity.work(goal, state)
        except Exception:
            pass
        finally:
            for p in patches:
                p.stop()
        return n["c"]


class TheVillageUsesTheTicksFrame(HelpersThatTakeNoStateStillGetTheFrame):
    WHERE = "village"

    def _extra(self):
        import brain.village_context as ctx
        return [mock.patch("brain.village_context.classify",
                           return_value=ctx.VILLAGE_TOP_MENU),
                mock.patch("actions.barter_panel._open_barter_panel", return_value=True)]

    def test_it_captures_nothing_when_the_state_carries_one(self):
        from brain.activities.village import Barter, VillageActivity
        self.assertEqual(
            self._captures(VillageActivity(), Barter("Birch Tree", "Svear"), FRAME,
                           self._extra()), 0)

    def test_it_still_captures_when_the_state_carries_none(self):
        from brain.activities.village import Barter, VillageActivity
        self.assertGreaterEqual(
            self._captures(VillageActivity(), Barter("Birch Tree", "Svear"), None,
                           self._extra()), 1)


class TheWorldMapUsesTheTicksFrame(HelpersThatTakeNoStateStillGetTheFrame):
    WHERE = "world_map"

    def test_the_frame_removes_a_capture(self):
        """One remains: `select_world_map_tab` TAPS the tab and looks for the control to tap.
        That is a primitive acting on its own screen, not a reading of ours."""
        from brain.activities.world_map import ChooseDestination, WorldMapActivity
        with_frame = self._captures(WorldMapActivity(),
                                    ChooseDestination("Svear", "village"), FRAME)
        without = self._captures(WorldMapActivity(),
                                 ChooseDestination("Svear", "village"), None)
        self.assertLess(with_frame, without)
