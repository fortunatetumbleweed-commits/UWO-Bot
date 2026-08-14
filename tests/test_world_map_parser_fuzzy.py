"""Regression: the docked-fleet marker renders over the CURRENT port's label,
so OCR reads "Pont Royal" instead of "Port Royal".  The primary detection path
used exact-substring matching, which dropped it — making the home port
undetectable and sending pan_to_port oscillating around a target it could see
but not read.  parse_visible_ports now falls back to the fuzzy matcher.
"""
import types
import unittest
from unittest.mock import patch, MagicMock

from vision.world_map_parser import parse_visible_ports


def _el(label, cx, cy, etype="text"):
    e = types.SimpleNamespace(
        label=label, element_type=etype,
        cx=cx, cy=cy,
        x1=cx - 60, y1=cy - 20, x2=cx + 60, y2=cy + 20,
    )
    return e


class PontRoyalFuzzyFallbackTests(unittest.TestCase):
    def setUp(self):
        self.ports = {
            "port royal": {"name": "Port Royal", "x": 2504, "y": 2429},
            "havana": {"name": "Havana", "x": 2400, "y": 2100},
        }

    def _run(self, elements):
        fake_omni = MagicMock()
        fake_omni.yolo_available.return_value = True
        with patch("vision.omniparser.get_omniparser", return_value=fake_omni), \
             patch("vision.omniparser.parse_fast_cached", return_value=elements):
            frame = MagicMock(); frame.width = 2400; frame.height = 1080
            return parse_visible_ports(frame, self.ports, {})

    def test_pont_royal_resolves_to_port_royal(self):
        # OCR-corrupted single label — substring match fails, fuzzy catches it.
        vis = self._run([_el("Pont Royal", 1160, 638)])
        keys = {v.key for v in vis}
        self.assertIn("port royal", keys)
        vp = next(v for v in vis if v.key == "port royal")
        self.assertEqual((vp.pix_cx, vp.pix_cy), (1160, 638))

    def test_clean_label_still_matches(self):
        vis = self._run([_el("Havana", 900, 500)])
        self.assertIn("havana", {v.key for v in vis})

    def test_unrelated_label_not_matched(self):
        # A word that isn't close to any port must NOT resolve to one.
        vis = self._run([_el("Shipyard", 900, 500)])
        self.assertEqual(vis, [])


if __name__ == "__main__":
    unittest.main()
