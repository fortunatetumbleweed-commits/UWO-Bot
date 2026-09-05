"""The report must show the parse the RUN paid for, whichever collaborator looked.

Live 2026-09-05, Berber. `_capture_with_perception` required the repository's held
observation to BE `_PERCEIVE_LAST_FRAME`, so any look taken after the tick's perceive
disqualified the frame. In a barter that is nearly every frame — the panel reader takes its
own observations and each supersedes the perceive. 27 observations in that run, only 8 of
them `perceive`; the report came out "66 frames: 1 live, 65 unread" while 65 of those frames
had a parse sitting in the cache the run had already paid for.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import actions.action_trace as T


class _Elem:
    def __init__(self, name):
        self.name = name

    def to_dict(self):
        return {"name": self.name}


class _Repo:
    def __init__(self, obs):
        self._obs = obs

    def current_if_valid(self):
        return self._obs


def _obs(frame):
    return types.SimpleNamespace(frame=frame, generation=7, taken_at=0.0)


class TheTraceAttachesTheObservationTheBotActedOn(unittest.TestCase):

    def setUp(self):
        # Distinct objects: the panel reader's frame is NOT the dispatcher's perceive.
        self.panel_frame = _Elem("panel-frame")     # any object with identity
        self.perceive_frame = _Elem("perceive-frame")
        self.captured = _Elem("the-new-capture")

    def _run(self, *, held, last_perceive, omni_for, ocr_for=()):
        """Drive _capture_with_perception with caches keyed by identity, as production is."""
        omni_cache = {id(f): (f, [_Elem("Exchange")]) for f in omni_for}
        ocr_cache = {id(f): (f, object()) for f in ocr_for}
        # `from brain import perceive` resolves the PACKAGE ATTRIBUTE, so patching
        # sys.modules does nothing here — set the real module's globals instead.
        import brain.perceive as real_perceive
        with mock.patch.dict("sys.modules", {
            "capture.adb_capture": types.SimpleNamespace(capture_screen=lambda: self.captured),
            "actions.perception": types.SimpleNamespace(screen=lambda: _Repo(held)),
            "vision.omniparser": types.SimpleNamespace(_FRAME_CACHE=omni_cache),
            "actions.sail_actions": types.SimpleNamespace(
                _OCR_CACHE=ocr_cache,
                _ocr_frame=lambda f, c: [("Exchange", 0.99, 10, 20)]),
        }), \
             mock.patch.object(real_perceive, "_PERCEIVE_LAST_FRAME", last_perceive), \
             mock.patch.object(real_perceive, "_PERCEIVE_LAST_RESULT",
                               types.SimpleNamespace(state="village", detail="Village")):
            return T._capture_with_perception()

    def test_a_panel_readers_observation_is_attached(self):
        """THE CASE THAT WAS LOST: valid observation, parsed, but not the dispatcher's."""
        frame, perception = self._run(held=_obs(self.panel_frame),
                                      last_perceive=self.perceive_frame,
                                      omni_for=[self.panel_frame])
        self.assertIs(frame, self.captured)
        self.assertIsNotNone(perception, "the run parsed this frame and acted on it")
        self.assertEqual(perception["omni"], [{"name": "Exchange"}])

    def test_it_does_not_caption_it_with_another_frames_verdict(self):
        """state/detail describe the frame the DISPATCHER classified — not this one."""
        _, perception = self._run(held=_obs(self.panel_frame),
                                  last_perceive=self.perceive_frame,
                                  omni_for=[self.panel_frame])
        self.assertIsNone(perception["state"])
        self.assertIsNone(perception["detail"])

    def test_the_dispatchers_own_look_still_carries_its_state(self):
        _, perception = self._run(held=_obs(self.perceive_frame),
                                  last_perceive=self.perceive_frame,
                                  omni_for=[self.perceive_frame])
        self.assertEqual(perception["state"], "village")
        self.assertEqual(perception["detail"], "Village")

    def test_nothing_valid_held_attaches_nothing(self):
        """Something has acted — no observation describes the screen we are about to tap."""
        _, perception = self._run(held=None, last_perceive=self.perceive_frame,
                                  omni_for=[self.perceive_frame])
        self.assertIsNone(perception)

    def test_an_unparsed_frame_is_never_parsed_to_fill_the_report(self):
        """Recording what happened must not change what the run costs."""
        _, perception = self._run(held=_obs(self.panel_frame),
                                  last_perceive=self.perceive_frame,
                                  omni_for=[])          # nothing in the cache
        self.assertIsNone(perception)

    def test_ocr_is_attached_only_when_it_too_was_cached(self):
        _, without = self._run(held=_obs(self.panel_frame), last_perceive=None,
                               omni_for=[self.panel_frame])
        self.assertEqual(without["ocr"], [], "omni parsed, text never read — a fact about the run")
        _, with_ocr = self._run(held=_obs(self.panel_frame), last_perceive=None,
                                omni_for=[self.panel_frame], ocr_for=[self.panel_frame])
        self.assertEqual(with_ocr["ocr"], [{"text": "Exchange", "conf": 0.99, "cx": 10, "cy": 20}])

    def test_a_recycled_id_is_not_mistaken_for_the_frame(self):
        """CPython reuses an address the moment an image is freed, so the cache stores the
        frame alongside and identity is what decides (live 2026-08-21)."""
        impostor = _Elem("someone-elses-frame")
        cache = {id(self.panel_frame): (impostor, [_Elem("Wrong")])}
        with mock.patch.dict("sys.modules", {
            "capture.adb_capture": types.SimpleNamespace(capture_screen=lambda: self.captured),
            "actions.perception": types.SimpleNamespace(
                screen=lambda: _Repo(_obs(self.panel_frame))),
            "vision.omniparser": types.SimpleNamespace(_FRAME_CACHE=cache),
            "actions.sail_actions": types.SimpleNamespace(_OCR_CACHE={}, _ocr_frame=None),
        }):
            _, perception = T._capture_with_perception()
        self.assertIsNone(perception, "an address match is not the frame")


if __name__ == "__main__":
    unittest.main()
