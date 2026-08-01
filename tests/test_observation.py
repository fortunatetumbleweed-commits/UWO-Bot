"""Tests for brain.observation — singleton, carry-forward, decay."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from brain import observation as obs


def fake_scene(scene_kind: str, confidence: str = "high"):
    """Duck-typed SceneModel-lookalike."""
    return type("FakeScene", (), {
        "scene_kind": scene_kind, "confidence": confidence,
    })()


class BotObservationTests(unittest.TestCase):

    def setUp(self):
        # Redirect disk persistence to a tmp file so tests can't read
        # or pollute the real memory/knowledge/state/last_settlement.json.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_path = obs._SETTLEMENT_PATH
        obs._SETTLEMENT_PATH = Path(self._tmp.name) / "last_settlement.json"
        self.addCleanup(self._restore_path)
        obs.reset()

    def _restore_path(self):
        obs._SETTLEMENT_PATH = self._orig_path

    def test_first_update_with_clear_scene(self):
        o = obs.update(tick=1, scene=fake_scene("port_overworld"),
                       detected_settlement="Amsterdam")
        self.assertEqual(o.tick, 1)
        self.assertEqual(o.scene_source, "detected")
        self.assertEqual(o.last_known_base_scene, "port_overworld")
        self.assertEqual(o.last_known_settlement, "Amsterdam")
        self.assertEqual(o.last_known_base_scene_age_ticks, 0)
        self.assertEqual(o.last_known_settlement_age_ticks, 0)
        self.assertIs(obs.current(), o)
        self.assertFalse(o.is_departed)

    def test_singleton_persists_across_updates(self):
        obs.update(tick=1, scene=fake_scene("port_overworld"))
        o2 = obs.update(tick=2, scene=fake_scene("building:market"))
        self.assertIs(obs.current(), o2)
        self.assertEqual(o2.last_known_base_scene, "building:market")

    def test_carry_forward_when_scene_obscured(self):
        """Overlay obscures scene → reuse previous base_scene."""
        obs.update(tick=1, scene=fake_scene("port_overworld"),
                   detected_settlement="Amsterdam")
        # Tick 2: no scene detected (obscured by overlay)
        o2 = obs.update(tick=2, scene=None)
        self.assertEqual(o2.last_known_base_scene, "port_overworld")
        self.assertEqual(o2.scene_source, "remembered")
        self.assertEqual(o2.last_known_base_scene_age_ticks, 1)
        # Settlement also carried forward
        self.assertEqual(o2.last_known_settlement, "Amsterdam")
        self.assertEqual(o2.last_known_settlement_age_ticks, 1)

    def test_low_confidence_treated_as_no_detection(self):
        obs.update(tick=1, scene=fake_scene("port_overworld"))
        o2 = obs.update(tick=2,
                        scene=fake_scene("building:market", confidence="low"))
        # low-confidence scene must NOT overwrite known good state
        self.assertEqual(o2.last_known_base_scene, "port_overworld")
        self.assertEqual(o2.scene_source, "remembered")

    def test_age_keeps_incrementing_until_re_detected(self):
        obs.update(tick=1, scene=fake_scene("port_overworld"))
        obs.update(tick=2, scene=None)
        o3 = obs.update(tick=3, scene=None)
        self.assertEqual(o3.last_known_base_scene_age_ticks, 2)

    def test_re_detection_resets_age(self):
        obs.update(tick=1, scene=fake_scene("port_overworld"))
        obs.update(tick=2, scene=None)
        o3 = obs.update(tick=3, scene=fake_scene("port_overworld"))
        self.assertEqual(o3.scene_source, "detected")
        self.assertEqual(o3.last_known_base_scene_age_ticks, 0)

    def test_unknown_when_no_history(self):
        o = obs.update(tick=1, scene=None)
        self.assertIsNone(o.last_known_base_scene)
        self.assertEqual(o.scene_source, "unknown")

    def test_record_action_attaches_to_current(self):
        obs.update(tick=1, scene=fake_scene("port_overworld"))
        obs.record_action(obs.ActionRecord(name="tap", target="Harbor",
                                           at_tick=1))
        cur = obs.current()
        self.assertIsNotNone(cur.last_action)
        self.assertEqual(cur.last_action.name, "tap")

    def test_action_carries_forward_when_not_replaced(self):
        obs.update(tick=1, scene=fake_scene("port_overworld"))
        obs.record_action(obs.ActionRecord(name="tap", target="Harbor",
                                           at_tick=1))
        o2 = obs.update(tick=2, scene=fake_scene("building:harbor"))
        self.assertIsNotNone(o2.last_action)
        self.assertEqual(o2.last_action.target, "Harbor")

    def test_explicit_action_replaces_carry_forward(self):
        obs.update(tick=1, scene=fake_scene("port_overworld"))
        obs.record_action(obs.ActionRecord(name="tap", target="A", at_tick=1))
        o2 = obs.update(tick=2, scene=fake_scene("port_overworld"),
                        last_action=obs.ActionRecord(name="swipe",
                                                     target="B", at_tick=2))
        self.assertEqual(o2.last_action.name, "swipe")
        self.assertEqual(o2.last_action.target, "B")

    def test_is_departed_tracks_base_scene(self):
        """Bot is departed when on sea/world_map; not when at settlement."""
        o1 = obs.update(tick=1, scene=fake_scene("port_overworld"),
                        detected_settlement="Tripoli")
        self.assertFalse(o1.is_departed)

        o2 = obs.update(tick=2, scene=fake_scene("sea"))
        self.assertTrue(o2.is_departed)
        # Settlement carried forward as voyage origin
        self.assertEqual(o2.last_known_settlement, "Tripoli")

        o3 = obs.update(tick=3, scene=fake_scene("world_map"))
        self.assertTrue(o3.is_departed)
        self.assertEqual(o3.last_known_settlement, "Tripoli")

        o4 = obs.update(tick=4, scene=fake_scene("port_overworld"),
                        detected_settlement="Berber")
        self.assertFalse(o4.is_departed)
        self.assertEqual(o4.last_known_settlement, "Berber")

    def test_detected_settlement_persists_to_disk(self):
        """A fresh detection writes the settlement to disk."""
        obs.update(tick=1, scene=fake_scene("port_overworld"),
                   detected_settlement="Tripoli")
        self.assertTrue(obs._SETTLEMENT_PATH.exists())
        data = json.loads(obs._SETTLEMENT_PATH.read_text())
        self.assertEqual(data["name"], "Tripoli")

    def test_disk_seeds_first_update_when_no_detection(self):
        """A new process at sea inherits the prior session's settlement."""
        # Simulate a prior session having saved Tripoli to disk.
        obs._SETTLEMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        obs._SETTLEMENT_PATH.write_text(json.dumps({"name": "Tripoli"}))
        obs.reset()    # clear in-memory state, force re-load on next update
        # First update of the new process: bot is at sea, no detected port.
        o = obs.update(tick=1, scene=fake_scene("sea"))
        self.assertEqual(o.last_known_settlement, "Tripoli")
        self.assertTrue(o.is_departed)

    def test_disk_does_not_overwrite_fresh_detection(self):
        """When this tick detects a settlement, disk value is ignored."""
        obs._SETTLEMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        obs._SETTLEMENT_PATH.write_text(json.dumps({"name": "Tripoli"}))
        obs.reset()
        o = obs.update(tick=1, scene=fake_scene("port_overworld"),
                       detected_settlement="Lisbon")
        self.assertEqual(o.last_known_settlement, "Lisbon")
        # And disk now reflects Lisbon (changed).
        data = json.loads(obs._SETTLEMENT_PATH.read_text())
        self.assertEqual(data["name"], "Lisbon")

    def test_disk_save_skipped_when_value_unchanged(self):
        """Repeated detections of the same settlement should not rewrite disk every tick."""
        obs.update(tick=1, scene=fake_scene("port_overworld"),
                   detected_settlement="Tripoli")
        mtime1 = obs._SETTLEMENT_PATH.stat().st_mtime
        # Wait one full second so any rewrite would change mtime perceptibly
        # (st_mtime resolution can be 1s on some filesystems).
        import time as _t
        _t.sleep(1.1)
        obs.update(tick=2, scene=fake_scene("port_overworld"),
                   detected_settlement="Tripoli")
        mtime2 = obs._SETTLEMENT_PATH.stat().st_mtime
        self.assertEqual(mtime1, mtime2)

    def test_missing_disk_file_is_silent(self):
        """No persisted file → first update with no detection yields None."""
        # _SETTLEMENT_PATH points at a tmp dir with no file yet
        o = obs.update(tick=1, scene=fake_scene("sea"))
        self.assertIsNone(o.last_known_settlement)

    def test_reset_clears_singleton(self):
        obs.update(tick=1, scene=fake_scene("port_overworld"))
        obs.reset()
        self.assertIsNone(obs.current())

    def test_overlay_auto_extracted_from_scene(self):
        """When scene carries .overlay and no overlay is passed explicitly,
        update() reads it from scene."""
        class FakeOverlay:
            kind = "modal_dialog"
            bbox = (100, 200, 800, 700)
        s = type("SceneWithOverlay", (), {
            "scene_kind":  "building:harbor",
            "confidence":  "high",
            "overlay":     FakeOverlay(),
        })()
        o = obs.update(tick=1, scene=s)
        self.assertIsNotNone(o.overlay)
        self.assertEqual(o.overlay.kind, "modal_dialog")

    def test_explicit_overlay_overrides_scene_overlay(self):
        """An explicit overlay argument wins over scene.overlay."""
        class A:
            kind = "from_scene"
        class B:
            kind = "explicit"
        s = type("SceneWithOverlay", (), {
            "scene_kind": "building:harbor", "confidence": "high",
            "overlay": A(),
        })()
        o = obs.update(tick=1, scene=s, overlay=B())
        self.assertEqual(o.overlay.kind, "explicit")


if __name__ == "__main__":
    unittest.main()
