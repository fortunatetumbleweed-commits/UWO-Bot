"""Above the confidence floor the family CNN is trusted; below it, signatures decide.

The rule (user, 2026-08-26): "if the family CNN has confidence over 0.8 ... only when it is
lower we will check signatures."

It applies only to the families that ARE screens — sea, world_map, port_overworld. `chromed`
and `transient` are coarse groupings, so however confident the CNN is about them the cascade
still has to establish WHICH building, sub-menu or overlay, and they always fall through.

Why the floor matters, live 2026-08-26: on the world map the CNN said `world_map` at 0.99 and
was right, while a fingerprint claim and a title read both said otherwise and the fleet spent
its attempts re-perceiving a map that was open in front of it.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain import perceive as P


class _Verdict:
    def __init__(self, family, confidence):
        self.family, self.confidence = family, confidence


def _classify(family, conf):
    """Run the classifier head with a scripted family verdict; report whether it
    short-circuited (returned without consulting the fingerprint cascade)."""
    reached_cascade = {"yes": False}

    def _fingerprints(*_a, **_k):
        reached_cascade["yes"] = True
        return None

    with patch("vision.family_classifier.classify_family",
               return_value=_Verdict(family, conf)), \
         patch("vision.omniparser.get_omniparser") as omni, \
         patch("vision.screen_classifier.classify_screen", side_effect=_fingerprints), \
         patch("vision.ocr.read_port_name", return_value="Lisboa"):
        omni.return_value.yolo_available.return_value = True
        try:
            out = P._classify_nav_state_inner(object())
        except Exception:
            out = None
    return out, reached_cascade["yes"]


class TheFloor(unittest.TestCase):

    def test_the_floor_is_where_the_user_set_it(self):
        self.assertEqual(P._FAMILY_TRUSTED_MIN, 0.8)

    def test_a_confident_sea_verdict_is_trusted_without_signatures(self):
        out, cascade = _classify("sea", 0.99)
        self.assertEqual((out or {}).get("location"), "sea")
        self.assertFalse(cascade, "signatures must not be consulted above the floor")

    def test_a_confident_world_map_verdict_is_trusted(self):
        """The live case: world_map at 0.99, contradicted by a fingerprint and a title read."""
        out, cascade = _classify("world_map", 0.99)
        self.assertEqual((out or {}).get("location"), "world_map")
        self.assertFalse(cascade)

    def test_just_below_the_floor_the_signatures_are_consulted(self):
        """0.79 used to short-circuit under the old 0.7 floor; now it gets a check."""
        _out, cascade = _classify("sea", 0.79)
        self.assertTrue(cascade, "below the floor the cascade must run")

    def test_exactly_at_the_floor_is_trusted(self):
        out, cascade = _classify("sea", 0.80)
        self.assertEqual((out or {}).get("location"), "sea")
        self.assertFalse(cascade)


class CoarseFamiliesAlwaysFallThrough(unittest.TestCase):
    """`chromed` and `transient` are not screens. However sure the CNN is, something still has
    to say which building or which overlay."""

    def test_chromed_is_not_a_screen(self):
        _out, cascade = _classify("chromed", 0.99)
        self.assertTrue(cascade)

    def test_transient_is_not_a_screen(self):
        _out, cascade = _classify("transient", 0.99)
        self.assertTrue(cascade)
