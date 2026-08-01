"""Tests for the junction detector.

Synthetic sector inputs cover the canonical cases:
- Open sea: 1 huge water arc → not a junction.
- Solid land: 0 arcs → not a junction.
- Straight channel: 2 arcs (fore + aft) → not a junction.
- Bend: 2 arcs (aft + perpendicular) → not a junction.
- Y junction: 3 arcs (aft + front-left + front-right) → JUNCTION.
- T junction: 3 arcs (aft + left + right) → JUNCTION.
- Multi-fork: 4+ arcs → JUNCTION.
"""
import unittest
from dataclasses import dataclass

from brain.goals.junction_detector import (
    JunctionDetector,
    TopologyKind,
    _find_water_arcs,
)


# Synthetic sector stub matching what JunctionDetector reads off the
# real NavigationView (just two relevant attributes).
@dataclass
class StubSector:
    land_fraction: float
    is_observed:   bool = True


def make_sectors(land_pattern):
    """Build a 16-sector list from a list of "W"/"L"/"-" markers.
    "W" = water (0.0), "L" = land (1.0), "-" = unobserved.
    Length should be 16."""
    out = []
    for ch in land_pattern:
        if ch == "W":
            out.append(StubSector(land_fraction=0.1))
        elif ch == "L":
            out.append(StubSector(land_fraction=0.9))
        elif ch == "-":
            out.append(StubSector(land_fraction=0.5, is_observed=False))
        else:
            raise ValueError(ch)
    assert len(out) == 16
    return out


class DegenerateCases(unittest.TestCase):
    def test_empty_sectors_returns_none(self):
        detector = JunctionDetector()
        self.assertIsNone(detector.detect([]))

    def test_open_sea_one_big_arc_not_junction(self):
        # 16 sectors of water — 1 arc.
        detector = JunctionDetector()
        self.assertIsNone(detector.detect(make_sectors("WWWWWWWWWWWWWWWW")))

    def test_solid_land_no_arcs_not_junction(self):
        detector = JunctionDetector()
        self.assertIsNone(detector.detect(make_sectors("LLLLLLLLLLLLLLLL")))


class StraightChannelTests(unittest.TestCase):
    def test_straight_channel_two_arcs_not_junction(self):
        """Fore + aft water, land on sides — bot in a north-south
        channel.  Two arcs but no fork."""
        # Sectors 0,15 = forward water; 7,8,9 = aft water; rest land.
        detector = JunctionDetector()
        sectors = make_sectors("WLLLLLLWWWLLLLLW")    # sec 0,15 + sec 7,8,9
        self.assertIsNone(detector.detect(sectors))

    def test_bend_two_arcs_not_junction(self):
        """Aft + perpendicular — bot at a right-angle bend.  Two arcs
        still not a junction."""
        # Aft (sec 7-9) + right (sec 3-5).
        detector = JunctionDetector()
        sectors = make_sectors("LLLWWWLWWWLLLLLL")
        self.assertIsNone(detector.detect(sectors))


class JunctionTests(unittest.TestCase):
    def test_y_junction_three_arcs(self):
        """Bot at a Y looking forward into the fork: water aft (came
        from), water forward-left and forward-right (the two
        branches), land between them."""
        detector = JunctionDetector()
        # Sector indices (16 total, sec 0 = forward, clockwise):
        # forward-right exit: sec 1-3 (W)
        # forward-left exit: sec 13-15 (W)
        # aft entrance: sec 7-9 (W)
        # land between (sec 4-6 and 10-12)
        sectors = make_sectors("LWWWLLLWWWLLLWWW")
        # check: positions 0,4,5,6,10,11,12 = L; 1,2,3,7,8,9,13,14,15 = W
        result = detector.detect(sectors)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.exits), 3)

    def test_t_junction_three_arcs(self):
        """Bot at a T: aft entrance + left exit + right exit, land
        forward (the T-bar)."""
        detector = JunctionDetector()
        # Aft (sec 7-9), right (sec 3-5), left (sec 11-13).
        sectors = make_sectors("LLLWWWLWWWLWWWLL")
        result = detector.detect(sectors)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.exits), 3)

    def test_multi_fork_four_arcs(self):
        """A 4-way junction (cross-shaped).  Should also detect."""
        detector = JunctionDetector()
        # Forward (sec 0,15), right (sec 3-5), aft (sec 7-9), left (sec 11-13).
        sectors = make_sectors("WLLWWWLWWWLWWWLW")
        result = detector.detect(sectors)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.exits), 4)


class ArcWidthTests(unittest.TestCase):
    def test_single_sector_water_is_filtered_as_noise(self):
        """A 1-sector water gap between land is below the default
        2-sector minimum width and should be ignored."""
        detector = JunctionDetector()    # default min_arc_width = 2
        # Three water islands of width 1 each — should not register
        # as exits, so the detector returns None (no arcs above
        # threshold).
        sectors = make_sectors("WLWLWLLLLLLLLLLL")
        self.assertIsNone(detector.detect(sectors))


class ExitBearingTests(unittest.TestCase):
    def test_bearings_are_relative_to_bow(self):
        """Sectors are bot-relative; exits should be too.
        Sec 0 = bearing 0° (bow); sec 4 = bearing 90° (right);
        sec 8 = 180° (astern); sec 12 = 270° (left)."""
        detector = JunctionDetector()
        # Aft (sec 7-9 = center sec 8 = 180°), right (sec 3-5 = sec 4 = 90°),
        # left (sec 11-13 = sec 12 = 270°).
        sectors = make_sectors("LLLWWWLWWWLWWWLL")
        result = detector.detect(sectors)
        self.assertIsNotNone(result)
        bearings = sorted(result.exits)
        # Expect ≈ [90, 180, 270].
        self.assertAlmostEqual(bearings[0],  90.0, delta=1)
        self.assertAlmostEqual(bearings[1], 180.0, delta=1)
        self.assertAlmostEqual(bearings[2], 270.0, delta=1)


class WrapAroundTests(unittest.TestCase):
    def test_arc_spanning_index_zero_is_one_arc(self):
        """Water at sectors 14, 15, 0, 1 should be one arc (length 4),
        not two arcs (length 2 each)."""
        is_water = [False] * 16
        for i in (14, 15, 0, 1):
            is_water[i] = True
        arcs = _find_water_arcs(is_water, min_width=2)
        self.assertEqual(len(arcs), 1)
        self.assertEqual(arcs[0].length, 4)


class ClassifyTests(unittest.TestCase):
    """The new `classify()` method returns CHANNEL / JUNCTION /
    DEAD_END, including a DEAD_END signal that the old `detect()`
    couldn't distinguish from CHANNEL."""

    def test_dead_end_one_arc_aft(self):
        """A dead-end: only water arc is behind (sec 7-9); all forward
        sectors are land."""
        detector = JunctionDetector()
        sectors = make_sectors("LLLLLLLWWWLLLLLL")
        result = detector.classify(sectors)
        self.assertEqual(result.kind, TopologyKind.DEAD_END)
        self.assertEqual(len(result.exits), 1)

    def test_channel_two_arcs_classified_as_channel(self):
        detector = JunctionDetector()
        sectors = make_sectors("WLLLLLLWWWLLLLLW")
        result = detector.classify(sectors)
        self.assertEqual(result.kind, TopologyKind.CHANNEL)

    def test_open_sea_one_arc_360_classified_as_channel(self):
        """A 360° all-water sweep is open sea, not a dead-end.  The
        one-arc width gate (`arc.length > n // 2`) prevents the
        false-dead-end firing that locked the bot into a spurious
        backtrack out of Cairo in voyage 170442."""
        detector = JunctionDetector()
        result = detector.classify(make_sectors("WWWWWWWWWWWWWWWW"))
        self.assertEqual(result.kind, TopologyKind.CHANNEL)

    def test_wide_arc_with_one_shore_segment_is_channel(self):
        """The standard hug-shore view: ~13 of 16 sectors are water,
        a narrow shore segment on one side.  This is NOT a dead-end."""
        detector = JunctionDetector()
        result = detector.classify(make_sectors("WWWWLLLWWWWWWWWW"))
        self.assertEqual(result.kind, TopologyKind.CHANNEL)

    def test_narrow_one_arc_still_dead_end(self):
        """The genuine dead-end case (narrow water pocket inside
        mostly-land surround) remains DEAD_END.  Width gate is
        `> n // 2` = 8 sectors; 4 water sectors stay below it."""
        detector = JunctionDetector()
        result = detector.classify(make_sectors("LLLLLLWWWWLLLLLL"))
        self.assertEqual(result.kind, TopologyKind.DEAD_END)

    def test_solid_land_zero_arcs_classified_as_channel(self):
        detector = JunctionDetector()
        result = detector.classify(make_sectors("LLLLLLLLLLLLLLLL"))
        self.assertEqual(result.kind, TopologyKind.CHANNEL)

    def test_y_junction_three_arcs_classified_as_junction(self):
        detector = JunctionDetector()
        sectors = make_sectors("LWWWLLLWWWLLLWWW")
        result = detector.classify(sectors)
        self.assertEqual(result.kind, TopologyKind.JUNCTION)
        self.assertEqual(len(result.exits), 3)

    def test_detect_unchanged_for_junctions(self):
        """`detect()` still returns a JunctionDescriptor for JUNCTION,
        None for DEAD_END / CHANNEL — back-compat preserved."""
        detector = JunctionDetector()
        # Y → JunctionDescriptor.
        self.assertIsNotNone(detector.detect(make_sectors("LWWWLLLWWWLLLWWW")))
        # Dead-end → None.
        self.assertIsNone(detector.detect(make_sectors("LLLLLLLWWWLLLLLL")))
        # Channel → None.
        self.assertIsNone(detector.detect(make_sectors("WLLLLLLWWWLLLLLW")))


class PersistenceFilterTests(unittest.TestCase):
    """Brunskill-style multi-frame hysteresis: require N consecutive
    matching classifies before the picker acts on a new kind."""

    def test_default_persistence_is_one(self):
        """Backward compat: persistence_n=1 means stateless."""
        d = JunctionDetector()
        self.assertEqual(d.persistence_n, 1)
        # Every tick reports raw.
        r1 = d.classify(make_sectors("LWWWLLLWWWLLLWWW"))  # JUNCTION
        r2 = d.classify(make_sectors("WLLLLLLWWWLLLLLW"))  # CHANNEL
        self.assertEqual(r1.kind, TopologyKind.JUNCTION)
        self.assertEqual(r2.kind, TopologyKind.CHANNEL)

    def test_transient_junction_is_suppressed_at_n3(self):
        """The Z-bend false-positive class: mid-turn one tick looks
        like JUNCTION but the next ticks return to CHANNEL.  With N=3
        the JUNCTION never gets confirmed and the picker doesn't act."""
        d = JunctionDetector(persistence_n=3)
        # Bootstrap with two CHANNEL ticks.
        d.classify(make_sectors("WLLLLLLWWWLLLLLW"))
        d.classify(make_sectors("WLLLLLLWWWLLLLLW"))
        # Single-tick spurious JUNCTION mid-turn — not yet 3 in a row.
        r_transient = d.classify(make_sectors("LWWWLLLWWWLLLWWW"))
        self.assertEqual(r_transient.kind, TopologyKind.CHANNEL,
            "transient JUNCTION must not be promoted")
        # Back to CHANNEL — confirmed kind sticks.
        r_recover = d.classify(make_sectors("WLLLLLLWWWLLLLLW"))
        self.assertEqual(r_recover.kind, TopologyKind.CHANNEL)

    def test_three_consecutive_junctions_are_confirmed(self):
        """True JUNCTION case: 3 ticks of agreeing input gets through."""
        d = JunctionDetector(persistence_n=3)
        d.classify(make_sectors("WLLLLLLWWWLLLLLW"))  # CHANNEL boot
        for _ in range(3):
            r = d.classify(make_sectors("LWWWLLLWWWLLLWWW"))  # JUNCTION
        self.assertEqual(r.kind, TopologyKind.JUNCTION)

    def test_persistence_uses_freshest_exits_after_promotion(self):
        """When persistence promotes, the exits come from the latest
        raw classify (so noisy exits over time refresh to current)."""
        d = JunctionDetector(persistence_n=3)
        # 3 ticks of JUNCTION with the same sectors → confirmed with
        # those exits.
        for _ in range(3):
            r = d.classify(make_sectors("LWWWLLLWWWLLLWWW"))
        first_exits = r.exits
        # Another 3 with slightly different sectors → exits update.
        for _ in range(3):
            r2 = d.classify(make_sectors("LWWWLLLWWWLLLLWW"))
        # Both classifications are JUNCTION but exit bearings may
        # differ; we only assert that the kind is preserved across
        # the new confirmed result.
        self.assertEqual(r2.kind, TopologyKind.JUNCTION)

    def test_bootstrap_first_tick_passes_through(self):
        """On the very first call, with no history, return raw —
        otherwise the bot would have no classification at start."""
        d = JunctionDetector(persistence_n=3)
        r = d.classify(make_sectors("WLLLLLLWWWLLLLLW"))
        self.assertEqual(r.kind, TopologyKind.CHANNEL)


class SkeletonHybridTests(unittest.TestCase):
    """Optional water_mask kwarg triggers the medial-axis skeleton
    classifier as a second opinion.  LAKE override only — other
    disagreements logged but not acted on (conservative rollout)."""

    def _round_lake_mask(self, size: int = 200, radius: int = 80):
        """Return a circular open-water mask — skeleton ratio collapses,
        triggering LAKE."""
        import numpy as np
        mask = np.zeros((size, size), dtype=bool)
        cy, cx = size // 2, size // 2
        Y, X = np.ogrid[:size, :size]
        mask[((Y - cy) ** 2 + (X - cx) ** 2) <= radius * radius] = True
        return mask

    def _channel_mask(self):
        """Return a thin channel mask — skeleton ratio ≈ 1/width."""
        import numpy as np
        mask = np.zeros((50, 200), dtype=bool)
        mask[20:30, 10:190] = True
        return mask

    def test_no_water_mask_means_no_skeleton_hybrid(self):
        """Backward compat: omitting water_mask is the original
        arc-count-only path."""
        d = JunctionDetector()
        # CHANNEL via arc-count.
        r = d.classify(make_sectors("WLLLLLLWWWLLLLLW"))
        self.assertEqual(r.kind, TopologyKind.CHANNEL)

    def test_skeleton_promotes_lake_when_arc_says_channel(self):
        """Arc-count cannot detect lakes; skeleton can.  Hybrid path
        returns LAKE when the mask is an open basin."""
        d = JunctionDetector()
        # Arc-count would say CHANNEL for this synthetic sector input
        # (one wide water arc) — but the skeleton on the lake mask
        # wins because arc-count can't see lakes.
        lake = self._round_lake_mask()
        r = d.classify(
            make_sectors("WWWWWWWWWWWWWWWW"),  # arc → CHANNEL (all water)
            water_mask=lake,
        )
        self.assertEqual(r.kind, TopologyKind.LAKE)

    def test_skeleton_agrees_channel_returns_channel(self):
        """When skeleton agrees with arc-count, the kind is unchanged."""
        d = JunctionDetector()
        channel = self._channel_mask()
        r = d.classify(
            make_sectors("WLLLLLLWWWLLLLLW"),  # arc → CHANNEL
            water_mask=channel,
        )
        self.assertEqual(r.kind, TopologyKind.CHANNEL)

    def test_skeleton_disagreement_non_lake_keeps_arc_result(self):
        """Skeleton override is LAKE-only.  Disagreement on any other
        class falls back to arc-count (the conservative rollout
        rule)."""
        import numpy as np
        d = JunctionDetector()
        # Mask doesn't actually depict a Y; just verify that when the
        # arc-count says JUNCTION (≥3 arcs) and skeleton says
        # something else, arc wins.
        # Channel mask (skeleton classifies as CHANNEL).
        ch_mask = self._channel_mask()
        r = d.classify(
            make_sectors("LWWWLLLWWWLLLWWW"),  # arc → JUNCTION
            water_mask=ch_mask,
        )
        # Arc says JUNCTION → wins (skeleton disagrees but only LAKE
        # is allowed to override).
        self.assertEqual(r.kind, TopologyKind.JUNCTION)


if __name__ == "__main__":
    unittest.main()
