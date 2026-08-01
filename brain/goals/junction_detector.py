"""Junction detector — heuristic perception layer over the 16-sector
mini-map navigation view.

See `docs/destination_generator_design.md` § 3c for the design
context.  In short: Trémaux-style DFS (§ 3b) needs the bot to
*recognize* "I'm at a junction" and *enumerate the exits*.  When
junction detection is confident, the picker uses Trémaux; otherwise
it falls back to Yamauchi (§ 3d).

This module is the heuristic detector — group consecutive sectors
with low `land_fraction` into "water arcs"; if three or more distinct
arcs exist (the entrance plus at least two forward exits), the bot
is at a junction.

Why ≥ 3 arcs:
- 1 arc: solid wall — not navigable.
- 2 arcs: straight channel or sharp bend — not a fork.
- 3+ arcs: fork (Y, T, multi-branch).

A Y-fork looked at from the trunk has three arcs visible — the
entrance behind, plus two distinct exits in front.  A T-fork has
the entrance plus two side exits.  Either case is captured by the
"≥ 3 distinct water arcs" rule.

The module is pure logic — no perception, no I/O.  Caller feeds in
the sector list (from `vision/minimap_navigation_view`) and reads
either a `JunctionDescriptor` or `None`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence, Tuple


# ── Tunables ────────────────────────────────────────────────────────────────

# Sectors with `land_fraction` below this are considered water.
# 0.5 = "more water than land" — handles partial occlusion of the
# channel without classifying a marginal-shore sector as water.
DEFAULT_LAND_FRACTION_THRESHOLD = 0.5

# Minimum width of a water arc to count as a real exit.  Single-
# sector water (1 sector = 22.5° at 16-sector resolution) is usually
# perceptual noise — narrow channels between visible obstacles.  An
# arc has to span at least this many sectors to register.
DEFAULT_MIN_ARC_WIDTH_SECTORS = 2

# Number of distinct water arcs required for "I'm at a junction."
# See module docstring.
MIN_ARCS_FOR_JUNCTION = 3


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WaterArc:
    """A contiguous run of low-land sectors, in sector indices.

    `start` is the index of the first sector in the run (in
    increasing-index order modulo the sector count); `length` is the
    number of sectors the arc spans.
    """
    start:  int
    length: int


@dataclass(frozen=True)
class JunctionDescriptor:
    """Multiple water arcs were detected.

    `exits` lists the *relative bearings* (degrees from bow, 0-360,
    clockwise) of the centerline of each arc.  The caller converts
    to absolute compass bearings using the bot's current heading.
    """
    exits: Tuple[float, ...]


class TopologyKind(str, Enum):
    """Per-tick topological classification of the local water/land
    geometry.  Drives the TremauxPicker state machine — see § 5a in
    docs/destination_generator_design.md.
    """
    CHANNEL  = "channel"      # 0 or 2 water arcs — straight or bend.
    JUNCTION = "junction"     # ≥ 3 water arcs — fork (Y, T, multi-way).
    DEAD_END = "dead_end"     # exactly 1 water arc — only the entrance.
    LAKE     = "lake"         # open water — skeleton degenerates to a
                              # small centroidal blob.  Treated as a
                              # terminal "explored leaf" by downstream
                              # planners (see vision/water_skeleton.py
                              # for the skeleton-ratio detection rule).


@dataclass(frozen=True)
class TopologyDescriptor:
    """Output of `JunctionDetector.classify()`.

    `kind`  — classification of the local geometry.
    `exits` — relative bearings of each water arc (degrees from bow).
              For JUNCTION: one bearing per exit.
              For DEAD_END: a single bearing pointing at the entrance.
              For CHANNEL: typically two (forward + aft); the picker
              treats this as "not a decision point" regardless.
    """
    kind:  TopologyKind
    exits: Tuple[float, ...]


# ── Detector ────────────────────────────────────────────────────────────────

@dataclass
class JunctionDetector:
    """Pure sector-grouping detector.

    Construction:
        JunctionDetector()  # defaults
        JunctionDetector(land_fraction_threshold=0.4, min_arc_width_sectors=3)
        JunctionDetector(persistence_n=3)  # require 3 consecutive matching classifies

    Use:
        result = detector.detect(sectors)
        if result is not None:
            for bearing in result.exits:
                # `bearing` is degrees from bow, 0-360, clockwise
                ...

    **Persistence filter (Brunskill et al. 2007 IROS hysteresis):**
    `persistence_n=1` is the original stateless behaviour — every tick's
    classification is reported as-is.  `persistence_n > 1` requires N
    consecutive ticks to agree on a kind before that kind is reported;
    until then the previously-confirmed kind sticks.  This is the
    canonical fix for transient mid-turn openings being mis-classified
    as JUNCTION (Z-bend false-positive class) and for single-tick noise
    flipping CHANNEL ↔ DEAD_END.  Tune higher (5-10) if your sensing
    is noisier; tune to 1 to opt out (e.g. for tests that need exact
    per-tick behaviour).
    """
    land_fraction_threshold: float = DEFAULT_LAND_FRACTION_THRESHOLD
    min_arc_width_sectors:   int   = DEFAULT_MIN_ARC_WIDTH_SECTORS
    persistence_n:           int   = 1

    # Persistence-filter state.  Init=False so a default-constructed
    # detector starts clean; reused across classify() calls.
    _recent_kinds: list = field(default_factory=list, init=False)
    _confirmed:    Optional[TopologyDescriptor] = field(
        default=None, init=False)

    def detect(self, sectors: Sequence) -> Optional[JunctionDescriptor]:
        """Backward-compat wrapper around `classify()`.

        Returns a `JunctionDescriptor` when the local geometry is a
        JUNCTION; returns None for CHANNEL or DEAD_END.  New code
        should use `classify()` to also get DEAD_END signals.
        """
        result = self.classify(sectors)
        if result.kind == TopologyKind.JUNCTION:
            return JunctionDescriptor(exits=result.exits)
        return None

    def classify(
        self,
        sectors: Sequence,
        water_mask=None,
    ) -> TopologyDescriptor:
        """Classify the local topology.

        `water_mask` (optional 2-D boolean numpy array, True where water)
        triggers the **skeleton hybrid path**:
          - Skeleton classifier (vision.water_skeleton) runs on the mask.
          - **LAKE override** — if skeleton says LAKE but arc-count
            says anything else, return LAKE.  Arc-count is structurally
            unable to detect lakes (no shore arcs to count), so this
            is a pure capability addition, not a behaviour change for
            the other classes.
          - Other disagreements (CHANNEL vs JUNCTION vs DEAD_END) are
            **logged at DEBUG only**.  Arc-count's verdict still wins.
            This gives us replay-comparison data on real captured
            voyages without changing behaviour; once we trust the
            skeleton classifier we can promote it to deciding vote on
            more classes.
        """
        raw = self._raw_classify(sectors)
        if water_mask is not None:
            raw = self._apply_skeleton_hybrid(raw, water_mask)
        if self.persistence_n <= 1:
            # Pure pass-through: keep the old per-tick semantics so
            # existing tests + non-stateful callers see no change.
            self._confirmed = raw
            return raw
        # Persistence filter.  Track the last N raw kinds; only update
        # the confirmed classification when N of them agree.
        self._recent_kinds.append(raw.kind)
        if len(self._recent_kinds) > self.persistence_n:
            self._recent_kinds = self._recent_kinds[-self.persistence_n:]
        if (len(self._recent_kinds) >= self.persistence_n
                and all(k == raw.kind for k in self._recent_kinds)):
            # N consecutive agreeing classifies — promote raw as the
            # new confirmed result (uses freshest exits).
            self._confirmed = raw
        elif self._confirmed is None:
            # Bootstrap: nothing confirmed yet, return raw on first tick.
            self._confirmed = raw
        return self._confirmed

    def _apply_skeleton_hybrid(
        self,
        arc_result: TopologyDescriptor,
        water_mask,
    ) -> TopologyDescriptor:
        """Run the medial-axis skeleton classifier on `water_mask`,
        combine with the arc-count result.

        Hybrid rules (conservative first cut):
          - LAKE override.  Arc-count cannot detect lakes (no shore
            arcs to count).  If skeleton says LAKE → return LAKE.
          - Other disagreements logged at DEBUG, arc-count wins.

        See `vision/water_skeleton.py` for the skeleton classifier
        and `docs/exploration_navigation_layers.md` for the
        perception/mapping/planning split this lives in.
        """
        try:
            from vision.water_skeleton import (
                classify_topology,
                extract_skeleton,
            )
        except Exception:
            # Skeleton dependencies missing — skip silently and let
            # the arc-count result stand.  Production envs always have
            # the dependencies; this guard is for partial-install tests.
            return arc_result
        try:
            analysis = extract_skeleton(water_mask)
            skel_kind = classify_topology(analysis)
        except Exception as e:
            # Bad mask shape / numerical issue → fall through to arc.
            try:
                from loguru import logger
                logger.debug(
                    f"[junction_detector] skeleton classify failed: {e}"
                )
            except Exception:
                pass
            return arc_result
        if skel_kind != arc_result.kind:
            try:
                from loguru import logger
                logger.debug(
                    f"[junction_detector] arc={arc_result.kind.value} "
                    f"vs skeleton={skel_kind.value}  "
                    f"(branch_pts={analysis.n_branch_points} "
                    f"endpoints={analysis.n_endpoints} "
                    f"ratio={analysis.skeleton_water_ratio:.3f})"
                )
            except Exception:
                pass
            if skel_kind == TopologyKind.LAKE:
                # Arc-count is structurally unable to detect LAKE;
                # trust skeleton here, keep arc-derived exits even
                # though they're unlikely to be meaningful inside a
                # lake (no shore arcs).
                return TopologyDescriptor(TopologyKind.LAKE, arc_result.exits)
        return arc_result

    def _raw_classify(self, sectors: Sequence) -> TopologyDescriptor:
        """Classify the local water/land geometry.

        Counts water arcs after applying the width threshold:
          - 0 or 2 arcs → CHANNEL (straight, bend, or fully-surrounded
                                    water).
          - 1 arc:
              · width > half the sectors → CHANNEL (open water with
                                                     one shore segment,
                                                     e.g. shore hugging).
              · width ≤ half the sectors → DEAD_END (narrow water
                                                     pocket in mostly-
                                                     land surround).
          - 3+ arcs    → JUNCTION (fork).

        The width gate on the 1-arc branch is essential — without it,
        the standard hug-shore view (most of 360° water, one shore
        segment) is mis-classified as DEAD_END and triggers a
        spurious backtrack.  See voyage explore_port_20260604_170442
        where the bot picked false junction → false dead-end → stuck
        in backtrack mode within the first 30 ticks out of Cairo.
        """
        if not sectors:
            return TopologyDescriptor(TopologyKind.CHANNEL, ())
        n = len(sectors)
        is_water = [
            s.is_observed and s.land_fraction < self.land_fraction_threshold
            for s in sectors
        ]
        arcs = _find_water_arcs(is_water, self.min_arc_width_sectors)
        sector_arc_deg = 360.0 / n
        bearings = tuple(
            ((arc.start + (arc.length - 1) / 2.0) * sector_arc_deg) % 360.0
            for arc in arcs
        )
        if len(arcs) >= MIN_ARCS_FOR_JUNCTION:
            return TopologyDescriptor(TopologyKind.JUNCTION, bearings)
        if len(arcs) == 1:
            if arcs[0].length > n // 2:
                # Wide water arc — open sea / shore-hugging geometry,
                # not a dead-end.  Treat as CHANNEL.
                return TopologyDescriptor(TopologyKind.CHANNEL, bearings)
            return TopologyDescriptor(TopologyKind.DEAD_END, bearings)
        return TopologyDescriptor(TopologyKind.CHANNEL, bearings)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _find_water_arcs(
    is_water: Sequence[bool],
    min_width: int,
) -> Tuple[WaterArc, ...]:
    """Find connected runs of True values in `is_water`, treating
    the sequence as circular.  Drops runs shorter than `min_width`.
    Returns arcs in ascending-start order; if a single run wraps
    across index 0 it's returned as one arc anchored at the
    pre-wrap index.
    """
    n = len(is_water)
    if not any(is_water):
        return ()
    if all(is_water):
        # Entire sphere is water — one arc spanning everything.
        return (WaterArc(start=0, length=n),)

    # Find the first False index so we can scan from a known boundary.
    # This anchors the scan so a wrap-around run is captured in one
    # piece.
    anchor = next(i for i, w in enumerate(is_water) if not w)

    arcs = []
    i = (anchor + 1) % n
    visited = 0
    while visited < n:
        if is_water[i]:
            start = i
            length = 0
            while visited < n and is_water[i]:
                length += 1
                i = (i + 1) % n
                visited += 1
            if length >= min_width:
                arcs.append(WaterArc(start=start, length=length))
        else:
            i = (i + 1) % n
            visited += 1
    return tuple(arcs)
