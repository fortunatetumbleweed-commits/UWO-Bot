"""A recorded frame carries the perception the bot used — on captures, not only on taps.

`record_tap` has attached perception since 2026-09-01, for the reason its own docstring
gives: "THE VIEWER MUST SHOW WHAT THE BOT SAW, NOT WHAT A SECOND LOOK SEES. Re-parsing a
frame in the report can SUCCEED WHERE THE LIVE RUN FAILED, and then the misread you opened
the report to find is the one thing it cannot show you."

A plain capture did not, and plain captures are most of a session: 375 of the 401 frames in
the Berber run of 2026-09-05 carried none, so the viewer showed nothing for them or re-parsed
them on demand (user, 2026-09-05: "the viewer currently does not attach the omniparser result
from the log, please add that when it is available").

Two properties hold this together, and both are tested below:

  * IDENTITY, NOT RESEMBLANCE. The attachment happens only when the repository's held
    observation IS this frame object. The game animates every frame, so two captures of one
    unchanged screen differ by pixels and would defeat any similarity test.

  * IT NEVER PAYS FOR INFERENCE. The elements come out of OmniParser's own frame cache. A
    recorder that re-parsed would change the run it exists to describe — and would reintroduce
    exactly the second look this feature exists to avoid.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import types
import unittest
from unittest import mock

from PIL import Image

from actions import action_trace


def _element(label, cx, cy):
    e = types.SimpleNamespace()
    e.to_dict = lambda label=label, cx=cx, cy=cy: {"label": label, "cx": cx, "cy": cy}
    return e


class _Recorder(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._saved = (action_trace._dir, action_trace._idx)
        action_trace._dir, action_trace._idx = self.tmp, 0
        self.frame = Image.new("RGB", (40, 20))

    def tearDown(self):
        action_trace._dir, action_trace._idx = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, *, held, cached, perceived=None):
        """Record a capture with the repository holding `held` and the parse cache `cached`."""
        obs = None if held is None else types.SimpleNamespace(frame=held)
        fake_screen = types.SimpleNamespace(current_if_valid=lambda: obs)
        cache = {} if cached is None else {id(cached): (cached, [_element("Almond", 1450, 557)])}
        fake_perceive = types.SimpleNamespace(
            _PERCEIVE_LAST_FRAME=perceived,
            _PERCEIVE_LAST_RESULT=types.SimpleNamespace(state="village", detail="Village"))
        with mock.patch("actions.perception.screen", lambda: fake_screen), \
             mock.patch.dict("vision.omniparser._FRAME_CACHE", cache, clear=True), \
             mock.patch.dict("sys.modules", {}), \
             mock.patch("brain.perceive._PERCEIVE_LAST_FRAME", perceived, create=True), \
             mock.patch("brain.perceive._PERCEIVE_LAST_RESULT",
                        fake_perceive._PERCEIVE_LAST_RESULT, create=True), \
             mock.patch("actions.sail_actions._ocr_frame", lambda f, c: [("Almond", 0.99, 1450, 557)]):
            action_trace.record_capture(self.frame)
        return self.tmp / "frame_0000.json"


class ThePerceptionIsAttached(_Recorder):

    def test_a_perceived_capture_gets_its_side_car(self):
        side = self._record(held=self.frame, cached=self.frame, perceived=self.frame)
        self.assertTrue(side.exists(), "the viewer would have had to re-parse this frame")
        data = json.loads(side.read_text())
        self.assertEqual(data["omni"], [{"label": "Almond", "cx": 1450, "cy": 557}])

    def test_the_frame_itself_is_still_saved(self):
        self._record(held=self.frame, cached=self.frame, perceived=self.frame)
        self.assertTrue((self.tmp / "frame_0000.png").exists())

    def test_the_nav_state_rides_along(self):
        side = self._record(held=self.frame, cached=self.frame, perceived=self.frame)
        self.assertEqual(json.loads(side.read_text())["state"], "village")


class ItAttachesNothingItCannotVouchFor(_Recorder):
    """A wrong attachment is worse than none: it would show a reading of a different screen
    beside this frame, which is precisely the confusion the viewer exists to remove."""

    def test_a_frame_the_repository_does_not_hold_gets_nothing(self):
        other = Image.new("RGB", (40, 20))
        self.assertFalse(self._record(held=other, cached=self.frame).exists())

    def test_no_observation_at_all_gets_nothing(self):
        self.assertFalse(self._record(held=None, cached=self.frame).exists())

    def test_an_UNPARSED_frame_is_not_parsed_now(self):
        """The whole point: no inference in the recorder. No cache entry, no side-car."""
        self.assertFalse(self._record(held=self.frame, cached=None).exists())

    def test_a_stale_cache_id_is_not_trusted(self):
        """id() is reused once a frame is freed — OmniParser pins the frame reference for
        this reason, and so must anything reading its cache."""
        other = Image.new("RGB", (40, 20))
        cache = {id(self.frame): (other, [_element("WRONG", 0, 0)])}
        obs = types.SimpleNamespace(frame=self.frame)
        with mock.patch("actions.perception.screen", lambda: types.SimpleNamespace(
                current_if_valid=lambda: obs)), \
             mock.patch.dict("vision.omniparser._FRAME_CACHE", cache, clear=True), \
             mock.patch("brain.perceive._PERCEIVE_LAST_FRAME", self.frame, create=True):
            action_trace.record_capture(self.frame)
        self.assertFalse((self.tmp / "frame_0000.json").exists(),
                         "attached another frame's elements to this one")

    def test_recording_never_raises(self):
        """A recorder that can fail a run is worse than a gap in a report."""
        with mock.patch("actions.perception.screen", side_effect=RuntimeError("boom")):
            action_trace.record_capture(self.frame)
        self.assertTrue((self.tmp / "frame_0000.png").exists())


if __name__ == "__main__":
    unittest.main()
