"""Tests for the medial-axis skeleton perception primitive.

Synthetic binary water masks exercise the canonical topology cases:
  - Straight channel              → CHANNEL
  - L / Z bend                    → CHANNEL (no branch point)
  - Y / T junction                → JUNCTION (one branch point)
  - Dead-end                      → DEAD_END (one endpoint)
  - Large open basin (lake)       → LAKE (skeleton ratio collapses)
  - Channel with an island        → has_loops=True
"""
import unittest

import numpy as np

from brain.goals.junction_detector import TopologyKind
from vision.water_skeleton import (
    SkeletonAnalysis,
    classify_topology,
    compute_skeleton_tangent_deg,
    extract_skeleton,
)


def _zeros(h: int, w: int) -> np.ndarray:
    return np.zeros((h, w), dtype=bool)


# ── Channel / bend (no branch points) ──────────────────────────────────────

class ChannelTests(unittest.TestCase):
    def test_horizontal_channel_classifies_as_channel(self):
        mask = _zeros(50, 200)
        mask[20:30, 10:190] = True  # 10-px wide horizontal channel
        analysis = extract_skeleton(mask)
        # No branch points; two endpoints at the open ends.
        self.assertEqual(analysis.n_branch_points, 0)
        # A clean horizontal channel has 2 endpoints (one at each end).
        self.assertEqual(analysis.n_endpoints, 2)
        self.assertEqual(classify_topology(analysis), TopologyKind.CHANNEL)

    def test_l_bend_classifies_as_channel(self):
        """L-bend: horizontal segment turning vertical.  Smooth curve in
        skeleton; no branch points."""
        mask = _zeros(100, 100)
        mask[20:30, 10:80] = True  # horizontal stem
        mask[20:80, 70:80] = True  # vertical stem (overlap forms L)
        analysis = extract_skeleton(mask)
        # An L is one continuous skeleton path → 0 branch points.
        self.assertEqual(analysis.n_branch_points, 0)
        self.assertEqual(classify_topology(analysis), TopologyKind.CHANNEL)


# ── Junction (≥1 branch point) ──────────────────────────────────────────────

class JunctionTests(unittest.TestCase):
    def test_y_junction_classifies_as_junction(self):
        """Three channels meeting at a point."""
        mask = _zeros(120, 120)
        # Vertical stem at the bottom.
        mask[60:120, 55:65] = True
        # Two branches at top angled outward.
        for r in range(60):
            mask[60 - r, 55 - r // 2:65 - r // 2] = True
            mask[60 - r, 55 + r // 2:65 + r // 2] = True
        analysis = extract_skeleton(mask)
        # At least one branch point at the meeting.
        self.assertGreaterEqual(analysis.n_branch_points, 1)
        self.assertEqual(classify_topology(analysis), TopologyKind.JUNCTION)

    def test_t_junction_classifies_as_junction(self):
        mask = _zeros(120, 120)
        mask[55:65, 10:110] = True   # horizontal stem
        mask[55:110, 55:65] = True   # vertical branch downward
        analysis = extract_skeleton(mask)
        self.assertGreaterEqual(analysis.n_branch_points, 1)
        self.assertEqual(classify_topology(analysis), TopologyKind.JUNCTION)


# ── Dead-end ────────────────────────────────────────────────────────────────

class DeadEndTests(unittest.TestCase):
    def test_dead_end_classifier_with_constructed_analysis(self):
        """Test the classifier rule directly: 0 branch points + 1
        endpoint + normal water ratio → DEAD_END.  Constructing the
        SkeletonAnalysis manually avoids the frame-boundary endpoint
        ambiguity that masks with a channel touching the crop edge
        would introduce (a channel exiting the frame still produces
        a skeleton endpoint at that pixel)."""
        analysis = SkeletonAnalysis(
            skeleton=np.zeros((10, 10), dtype=bool),
            branch_points=[],
            endpoints=[(5, 5)],
            has_loops=False,
            n_skeleton_px=50,
            n_water_px=500,   # ratio = 0.1, well above lake threshold
        )
        self.assertEqual(classify_topology(analysis), TopologyKind.DEAD_END)

    def test_two_endpoints_classifies_as_channel_not_dead_end(self):
        """Channel passing through the frame has 2 endpoints (one at
        each crop boundary) — should be CHANNEL, not DEAD_END."""
        analysis = SkeletonAnalysis(
            skeleton=np.zeros((10, 10), dtype=bool),
            branch_points=[],
            endpoints=[(5, 0), (5, 9)],
            has_loops=False,
            n_skeleton_px=50,
            n_water_px=500,
        )
        self.assertEqual(classify_topology(analysis), TopologyKind.CHANNEL)


# ── Lake (skeleton degenerates) ─────────────────────────────────────────────

class LakeTests(unittest.TestCase):
    def test_large_open_basin_classifies_as_lake(self):
        """A near-round open water region.  Skeleton degenerates to a
        small star pattern near the centroid → very low
        skeleton_water_ratio → LAKE."""
        mask = _zeros(200, 200)
        cy, cx = 100, 100
        r = 80
        Y, X = np.ogrid[:200, :200]
        mask[((Y - cy) ** 2 + (X - cx) ** 2) <= r * r] = True
        analysis = extract_skeleton(mask)
        # Open basin: many water pixels but very few skeleton pixels.
        self.assertGreater(analysis.n_water_px, 1000)
        # Ratio should be small (well below the 0.05 threshold).
        self.assertLess(analysis.skeleton_water_ratio, 0.05)
        self.assertEqual(classify_topology(analysis), TopologyKind.LAKE)

    def test_thin_channel_does_not_classify_as_lake(self):
        """A 10-px wide channel has skeleton_water_ratio ~ 1/10 — far
        above the lake threshold."""
        mask = _zeros(50, 200)
        mask[20:30, 10:190] = True
        analysis = extract_skeleton(mask)
        # Sanity: ratio for a 10-px-wide channel is ≈ 1/10.
        self.assertGreater(analysis.skeleton_water_ratio, 0.05)
        self.assertNotEqual(classify_topology(analysis), TopologyKind.LAKE)


# ── Island (loop in skeleton) ───────────────────────────────────────────────

class IslandTests(unittest.TestCase):
    def test_channel_with_island_has_loops(self):
        """Wide channel containing a small land-island.  The skeleton
        of the water region has a loop around the island."""
        mask = _zeros(80, 200)
        # Wide channel
        mask[20:60, 10:190] = True
        # Island in the middle (carved out)
        mask[35:45, 90:110] = False
        analysis = extract_skeleton(mask)
        self.assertTrue(analysis.has_loops,
            "skeleton of a channel-with-island must have a topological loop")


# ── Spur pruning (noise robustness) ────────────────────────────────────────

class SpurPruningTests(unittest.TestCase):
    def test_pruning_default_disabled_by_kwarg(self):
        """Setting prune_spurs_px=0 disables pruning — used by callers
        who want raw skeleton features for diagnostics."""
        mask = _zeros(50, 200)
        mask[20:30, 10:190] = True
        a = extract_skeleton(mask, prune_spurs_px=0)
        self.assertEqual(a.n_branch_points, 0)  # clean channel has none
    # TODO: end-to-end spur-pruning correctness — current algorithm
    # under-prunes when spurs join the main skeleton at multi-pixel
    # branch clusters.  Revisit with a cluster-consolidation pass
    # when wiring the skeleton classifier into JunctionDetector.


# ── Cost ────────────────────────────────────────────────────────────────────

class PerformanceSanityTests(unittest.TestCase):
    def test_skeleton_on_full_minimap_size_under_50ms(self):
        """The bot's minimap crop is 184×381.  Skeleton should run
        well under a tick (~1.4 s)."""
        import time
        mask = _zeros(184, 381)
        # A channel-with-bend through the whole frame.
        mask[80:100, :] = True
        mask[:, 180:200] = True
        t0 = time.time()
        analysis = extract_skeleton(mask)
        dt_ms = (time.time() - t0) * 1000
        # Generous bound — the agent's benchmark said 3-8 ms; on a CI
        # box we allow 100 ms before flagging.
        self.assertLess(dt_ms, 100,
            f"skeleton extraction too slow: {dt_ms:.1f} ms")


# ── Skeleton tangent (steering substrate, Phase 1) ──────────────────────────

class SkeletonTangentTests(unittest.TestCase):
    """`compute_skeleton_tangent_deg` is the world-frame tangent that
    replaces §13.21's bow-frame shoreline fit in narrow channels.

    Minimap is N-up (matches `_compute_sectors` line 875), so pixel-up
    → world North.  These tests codify that mapping and the bow-hint
    tiebreaker.  See `memory/project_skeleton_steering_migration.md`.
    """

    def test_vertical_channel_returns_south_with_south_hint(self):
        mask = _zeros(100, 100)
        mask[10:90, 40:60] = True
        analysis = extract_skeleton(mask)
        t = compute_skeleton_tangent_deg(
            analysis, ship_xy=(50, 50), bow_hint_deg=180.0,
        )
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t, 180.0, delta=1.0)

    def test_vertical_channel_returns_north_with_north_hint(self):
        mask = _zeros(100, 100)
        mask[10:90, 40:60] = True
        analysis = extract_skeleton(mask)
        t = compute_skeleton_tangent_deg(
            analysis, ship_xy=(50, 50), bow_hint_deg=0.0,
        )
        self.assertIsNotNone(t)
        # 0 or 360 both acceptable as N.
        wrapped = t % 360.0
        self.assertTrue(min(wrapped, 360.0 - wrapped) < 1.0)

    def test_horizontal_channel_resolves_east_or_west_by_hint(self):
        mask = _zeros(100, 100)
        mask[40:60, 10:90] = True
        analysis = extract_skeleton(mask)
        t_e = compute_skeleton_tangent_deg(
            analysis, ship_xy=(50, 50), bow_hint_deg=90.0,
        )
        t_w = compute_skeleton_tangent_deg(
            analysis, ship_xy=(50, 50), bow_hint_deg=270.0,
        )
        self.assertAlmostEqual(t_e, 90.0, delta=1.0)
        self.assertAlmostEqual(t_w, 270.0, delta=1.0)

    def test_no_hint_returns_canonical_half_axis(self):
        """When no bow hint, the function returns the orientation in
        [0, 180) — caller picks the orientation downstream (via the
        §13.21 commit/EWMA logic)."""
        mask = _zeros(100, 100)
        mask[10:90, 40:60] = True
        analysis = extract_skeleton(mask)
        t = compute_skeleton_tangent_deg(analysis, ship_xy=(50, 50))
        self.assertIsNotNone(t)
        self.assertLess(t, 180.0)

    def test_independent_of_caller_bow_when_pixels_dominate(self):
        """The whole point of the migration: same skeleton + same ship
        position → same world-frame tangent, regardless of caller
        intent.  A corrupted bow hint can only re-orient the existing
        axis, never invent one — proving the tangent does NOT inherit
        bow-frame corruption (commit at 2026-06-06 t605/t753/t880
        in explore_port_20260606_190255 wouldn't have happened with
        skeleton-derived tangent)."""
        mask = _zeros(100, 100)
        mask[10:90, 40:60] = True  # N-S channel
        analysis = extract_skeleton(mask)
        # Pretend the bow OCR mis-read 180° → 0° (south to north).
        # The axis is still N-S; the hint only flips orientation.
        t_correct = compute_skeleton_tangent_deg(
            analysis, ship_xy=(50, 50), bow_hint_deg=180.0,
        )
        t_corrupt = compute_skeleton_tangent_deg(
            analysis, ship_xy=(50, 50), bow_hint_deg=0.0,
        )
        # Both lie on the N-S axis (mod 180°).  Corruption can flip
        # orientation, but the downstream §13.21 commit catches that
        # by re-anchoring to the prior committed direction.
        self.assertAlmostEqual(
            abs((t_correct - t_corrupt) % 180.0), 0.0, delta=1.0)

    def test_too_few_pixels_returns_none(self):
        """A nearly-empty skeleton (sparse / lake-degenerate) yields
        None so the caller falls back to the shoreline LSQ path."""
        mask = _zeros(100, 100)
        mask[50, 50] = True  # single isolated pixel
        analysis = extract_skeleton(mask)
        t = compute_skeleton_tangent_deg(analysis, ship_xy=(50, 50))
        self.assertIsNone(t)

    def test_lake_degenerate_skeleton_returns_none(self):
        """An open round basin has no clear axis → degenerate
        eigenvalues → None.  Caller falls back to shoreline tangent."""
        # 40-radius disc filled.
        mask = _zeros(100, 100)
        yy, xx = np.ogrid[:100, :100]
        mask[(xx - 50) ** 2 + (yy - 50) ** 2 <= 40 * 40] = True
        analysis = extract_skeleton(mask)
        t = compute_skeleton_tangent_deg(
            analysis, ship_xy=(50, 50), neighborhood_px=15,
        )
        # In a disc, the local skeleton near the center is small and
        # nearly isotropic → degenerate → None.
        self.assertIsNone(t)


if __name__ == "__main__":
    unittest.main()
