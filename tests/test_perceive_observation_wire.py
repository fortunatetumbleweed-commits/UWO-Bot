"""Wire-up test: perceive() publishes a BotObservation."""
import unittest
from unittest.mock import patch

from brain import observation as obs
from brain.perceive import PerceiveResult, _publish_observation


class PublishObservationTests(unittest.TestCase):

    def setUp(self):
        obs.reset()

    def test_publishes_basic_fields(self):
        r = PerceiveResult(
            state="port_overworld", port="Amsterdam",
            detail="port_overworld", flow=None, flow_step=None,
            sub_menu=None, interruptors=[], confidence="high",
        )
        _publish_observation(r, frame=object())
        cur = obs.current()
        self.assertIsNotNone(cur)
        self.assertEqual(cur.last_known_settlement, "Amsterdam")
        self.assertEqual(cur.last_known_base_scene, "port_overworld")
        self.assertEqual(cur.scene_source, "detected")

    def test_compound_scene_kind_for_building(self):
        """state=building, detail=harbor → scene_kind=building:harbor."""
        r = PerceiveResult(
            state="building", port="Diu", detail="harbor",
            flow=None, flow_step=None, sub_menu=None,
            interruptors=[], confidence="high",
        )
        _publish_observation(r, frame=object())
        self.assertEqual(obs.current().last_known_base_scene,
                         "building:harbor")

    def test_low_confidence_falls_back_to_remembered(self):
        # tick 1: solid detection
        _publish_observation(PerceiveResult(
            state="port_overworld", port="Amsterdam", detail="x",
            flow=None, flow_step=None, sub_menu=None,
            interruptors=[], confidence="high",
        ), frame=object())
        # tick 2: low-confidence verdict
        _publish_observation(PerceiveResult(
            state="building", port=None, detail="market",
            flow=None, flow_step=None, sub_menu=None,
            interruptors=[], confidence="low",
        ), frame=object())
        cur = obs.current()
        self.assertEqual(cur.last_known_base_scene, "port_overworld")
        self.assertEqual(cur.scene_source, "remembered")
        # Settlement carried forward too
        self.assertEqual(cur.last_known_settlement, "Amsterdam")

    def test_auto_increments_tick(self):
        _publish_observation(PerceiveResult(
            state="port_overworld", port="A", detail="x",
            flow=None, flow_step=None, sub_menu=None,
            interruptors=[], confidence="high",
        ), frame=object())
        _publish_observation(PerceiveResult(
            state="sea", port=None, detail="x",
            flow=None, flow_step=None, sub_menu=None,
            interruptors=[], confidence="high",
        ), frame=object())
        self.assertEqual(obs.current().tick, 2)


if __name__ == "__main__":
    unittest.main()
