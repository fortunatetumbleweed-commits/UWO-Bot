"""Medial-axis (skeleton) topology classification + tangent for the minimap.

Per-tick perception primitive — independent of past ticks.  Applies
Zhang-Suen thinning (Zhang & Suen 1984 CACM) to a binary water mask
to produce a single-pixel-wide centerline, then analyses the skeleton
to classify the local geometry as one of:

  CHANNEL  — single passage, possibly curved
  JUNCTION — skeleton has a branch point (≥3 neighbours at a pixel)
  DEAD_END — skeleton has a single endpoint (exactly 1 neighbour)
  LAKE     — open water; skeleton degenerates because there's no
             clear elongated medial axis

Properties exploited:
  - Bends produce smooth skeleton curves with NO branch points → CHANNEL.
  - Y-junctions produce a single branch point → JUNCTION.
  - Dead-ends produce one endpoint at the closed end → DEAD_END.
  - Lakes (open water filling most of the minimap) produce a very low
    `skeleton_pixels / water_pixels` ratio → LAKE.
  - Islands inside a navigable region produce skeleton loops; reported
    via `has_loops` (downstream consumer can treat as JUNCTION-with-cycle).

References:
  Zhang & Suen 1984 (CACM) — Zhang-Suen iterative thinning.
  Choset & Burdick 2000 (IJRR) — HGVG topology from medial axis.
  Beeson, Modayil, Kuipers 2010 (IJRR) — gateway/width gating.

Architectural note: the skeleton is stateless per-tick by design.
Cross-tick memory lives in `brain/goals/junction_graph.py` keyed by
HUD lat/lon cell — eye vs. brain.  Two ticks observing the same
junction from different bow angles produce different pixel-space
skeletons but map to the same place-graph node because the lat/lon
cell is the same.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np
from scipy.ndimage import convolve, label
from skimage.morphology import skeletonize

from brain.goals.junction_detector import TopologyKind


# ── Tunables ────────────────────────────────────────────────────────────────

# Pixels with strictly more skeleton neighbours than this in a 3×3
# window count as branch points.
_BRANCH_NEIGHBOR_MIN = 3

# Branches from an endpoint to the nearest branch point shorter than
# this are removed as noise spurs.  Couprie 2007 recommends 5-10 px
# at sub-300-px occupancy resolution; we sit in that band.
DEFAULT_SPUR_PRUNE_PX = 5

# When `skeleton_pixels / water_pixels` is below this, classify as
# LAKE — open water has too few medial-axis pixels relative to its
# area.  Empirically: a 50-px channel has ratio ≈ 1/width ≈ 0.05; an
# open round basin has ratio approaching 1 / sqrt(area) which is
# orders of magnitude smaller.
DEFAULT_LAKE_SKELETON_RATIO = 0.05

# Below this water-pixel count, classification is unreliable — we
# don't have enough mask to thin.  Falls back to CHANNEL (no signal).
MIN_WATER_PX_FOR_CLASSIFY = 50


# ── Public types ────────────────────────────────────────────────────────────

@dataclass
class SkeletonAnalysis:
    """Per-tick skeleton topology analysis.  See `extract_skeleton`."""
    skeleton:        np.ndarray             # binary 2-D mask
    branch_points:   List[Tuple[int, int]]  # (y, x) of branch pixels
    endpoints:       List[Tuple[int, int]]  # (y, x) of endpoint pixels
    has_loops:       bool                   # cycle present in skeleton
    n_skeleton_px:   int
    n_water_px:      int

    @property
    def n_branch_points(self) -> int:
        return len(self.branch_points)

    @property
    def n_endpoints(self) -> int:
        return len(self.endpoints)

    @property
    def skeleton_water_ratio(self) -> float:
        if self.n_water_px <= 0:
            return 0.0
        return self.n_skeleton_px / float(self.n_water_px)


# ── Skeleton extraction ─────────────────────────────────────────────────────

def extract_skeleton(
    water_mask: np.ndarray,
    prune_spurs_px: int = DEFAULT_SPUR_PRUNE_PX,
) -> SkeletonAnalysis:
    """Compute medial-axis skeleton + topology counts.

    Args:
      water_mask: 2-D array, True/non-zero = water.
      prune_spurs_px: short noise spurs (endpoint → nearest branch
        point distance below this) are pruned before counting.  Set
        to 0 to disable.
    """
    water_mask = water_mask.astype(bool)
    n_water_px = int(water_mask.sum())
    sk = skeletonize(water_mask).astype(bool)
    branch_points, endpoints = _find_topology_points(sk)
    if prune_spurs_px > 0:
        sk, branch_points, endpoints = _prune_short_spurs(
            sk, branch_points, endpoints, prune_spurs_px)
    has_loops = _has_loops(sk)
    return SkeletonAnalysis(
        skeleton=sk,
        branch_points=branch_points,
        endpoints=endpoints,
        has_loops=has_loops,
        n_skeleton_px=int(sk.sum()),
        n_water_px=n_water_px,
    )


# ── Topology classification ─────────────────────────────────────────────────

def classify_topology(
    analysis: SkeletonAnalysis,
    lake_skeleton_ratio: float = DEFAULT_LAKE_SKELETON_RATIO,
    min_water_px: int = MIN_WATER_PX_FOR_CLASSIFY,
) -> TopologyKind:
    """Map skeleton features to a TopologyKind.

    Decision tree (in order):
      1. Insufficient water visible → CHANNEL (no signal; safe default).
      2. skeleton_water_ratio below `lake_skeleton_ratio` → LAKE
         (open water; skeleton degenerates).
      3. ≥1 branch point → JUNCTION.
      4. exactly 1 endpoint → DEAD_END.
      5. default → CHANNEL.

    Loops in the skeleton (islands) are detectable via
    `analysis.has_loops`; the downstream consumer (TremauxPicker /
    JunctionGraph) decides how to encode the cycle in the place graph.
    """
    if analysis.n_water_px < min_water_px:
        return TopologyKind.CHANNEL
    if analysis.skeleton_water_ratio < lake_skeleton_ratio:
        return TopologyKind.LAKE
    if analysis.n_branch_points >= 1:
        return TopologyKind.JUNCTION
    if analysis.n_endpoints == 1:
        return TopologyKind.DEAD_END
    return TopologyKind.CHANNEL


# ── Skeleton tangent (world-frame steering substrate) ──────────────────────

# Default radius around the ship pixel for the local skeleton-tangent
# fit.  Wide enough to span ~one channel-width of skeleton curve so the
# PCA principal axis is stable; narrow enough to stay local to the
# ship's segment when the skeleton makes a sharp bend.
DEFAULT_TANGENT_NEIGHBORHOOD_PX = 40

# Minimum skeleton pixels inside the neighborhood for a usable PCA.
MIN_PIXELS_FOR_TANGENT = 4


def compute_skeleton_tangent_deg(
    analysis: SkeletonAnalysis,
    ship_xy: Tuple[float, float],
    neighborhood_px: int = DEFAULT_TANGENT_NEIGHBORHOOD_PX,
    bow_hint_deg: float | None = None,
) -> float | None:
    """World-frame compass tangent of the channel at the ship's position.

    Approach: gather skeleton pixels within `neighborhood_px` of
    `ship_xy`, run PCA on their (dx, dy) offsets, and convert the
    principal eigenvector to a compass bearing.  The minimap is N-up
    (`_compute_sectors` in minimap_navigation_view.py:875 — "0=N=image
    -up"), so pixel-up → world North, pixel-right → world East.

    The principal axis is undirected.  When `bow_hint_deg` is provided,
    we pick the orientation closer to the bow as a one-tick seed for
    downstream commitment logic (the §13.21 commit then snaps every
    subsequent tick).  When no hint is given, returns the axis with
    bearing in [0, 180) — caller picks orientation.

    Returns None when:
      - skeleton has too few pixels in the neighborhood (PCA undefined)
      - the principal eigenvalue is degenerate (no clear direction —
        e.g. inside a lake patch the skeleton hybrid missed)

    World-frame property: this function never reads `ship_heading_deg`
    or any bow-derived quantity, so a corrupted bow heading cannot
    poison the tangent — the whole point of migrating §13.21 away from
    shoreline-fit-in-bow-frame.  See
    `memory/project_skeleton_steering_migration.md`.
    """
    if analysis.skeleton is None or not analysis.skeleton.any():
        return None
    sx, sy = ship_xy
    ys, xs = np.where(analysis.skeleton)
    if ys.size == 0:
        return None
    dx = xs.astype(np.float64) - float(sx)
    dy = ys.astype(np.float64) - float(sy)
    r2 = dx * dx + dy * dy
    mask = r2 <= float(neighborhood_px) ** 2
    if int(mask.sum()) < MIN_PIXELS_FOR_TANGENT:
        return None
    dx_n = dx[mask] - dx[mask].mean()
    dy_n = dy[mask] - dy[mask].mean()
    # 2×2 covariance of the local skeleton offsets.
    cov = np.array([
        [(dx_n * dx_n).sum(), (dx_n * dy_n).sum()],
        [(dx_n * dy_n).sum(), (dy_n * dy_n).sum()],
    ]) / max(1, int(mask.sum()) - 1)
    eigvals, eigvecs = np.linalg.eigh(cov)
    # Degeneracy: if the two eigenvalues are nearly equal, the local
    # patch is round (no direction) — skip.
    if eigvals[0] >= 0.5 * eigvals[1]:
        return None
    principal = eigvecs[:, 1]  # eigvec for the larger eigenvalue
    px, py = float(principal[0]), float(principal[1])
    # Pixel-up = world N → compass bearing of vector (px, py):
    # compass = atan2(px, -py) — matching _compute_sectors line 890.
    bearing = (math.degrees(math.atan2(px, -py)) + 360.0) % 360.0
    # Resolve the ±180° axis ambiguity using the bow hint if given.
    if bow_hint_deg is not None:
        alt = (bearing + 180.0) % 360.0
        if _angdiff(alt, bow_hint_deg) < _angdiff(bearing, bow_hint_deg):
            bearing = alt
    else:
        # No hint → canonical 0..180 form so callers can post-select.
        if bearing >= 180.0:
            bearing -= 180.0
    return bearing


def _angdiff(a: float, b: float) -> float:
    """Shortest absolute compass-angle difference in degrees."""
    d = (a - b) % 360.0
    return min(d, 360.0 - d)


DEFAULT_LOOKAHEAD_PX = 20


def trace_skeleton_forward(
    analysis: SkeletonAnalysis,
    ship_xy: Tuple[float, float],
    forward_hint_deg: float,
    lookahead_px: int = DEFAULT_LOOKAHEAD_PX,
) -> Optional[Tuple[int, int]]:
    """Walk along the skeleton from the bot's nearest skeleton pixel,
    in the direction closest to `forward_hint_deg`.

    Returns the (x, y) pixel position reached after up to
    `lookahead_px` greedy steps, or None if the skeleton is empty.

    Phase 2 of the skeleton-steering migration uses this as the
    **per-tick waypoint source** — answers "where on the channel
    should I head next?" from current state, not from a stored
    target that goes stale (see
    `memory/project_skeleton_steering_migration.md`).

    Coordinates: minimap is N-up (`_compute_sectors` line 875),
    so compass `forward_hint_deg=0` corresponds to pixel direction
    (dx=0, dy=-1).
    """
    sk = analysis.skeleton
    if sk is None or not sk.any():
        return None
    sx, sy = ship_xy
    ys, xs = np.where(sk)
    if ys.size == 0:
        return None
    dx = xs.astype(np.float64) - float(sx)
    dy = ys.astype(np.float64) - float(sy)
    nearest_idx = int(np.argmin(dx * dx + dy * dy))
    cur = (int(xs[nearest_idx]), int(ys[nearest_idx]))

    rad = math.radians(forward_hint_deg)
    forward_pdx = math.sin(rad)
    forward_pdy = -math.cos(rad)

    visited = {cur}
    h, w = sk.shape
    for _ in range(lookahead_px):
        cx, cy = cur
        best_align = -2.0
        best_next = None
        for ddy in (-1, 0, 1):
            for ddx in (-1, 0, 1):
                if ddy == 0 and ddx == 0:
                    continue
                nx, ny = cx + ddx, cy + ddy
                if not (0 <= ny < h and 0 <= nx < w):
                    continue
                if not sk[ny, nx]:
                    continue
                if (nx, ny) in visited:
                    continue
                mag = math.hypot(ddx, ddy)
                cos_align = (ddx * forward_pdx + ddy * forward_pdy) / mag
                if cos_align > best_align:
                    best_align = cos_align
                    best_next = (nx, ny)
        if best_next is None:
            break
        cur = best_next
        visited.add(cur)
    return cur


# ── Internal helpers ────────────────────────────────────────────────────────

def _find_topology_points(
    skeleton: np.ndarray,
) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Find branch points (≥3 neighbours) and endpoints (exactly 1
    neighbour) via 3×3 convolution.  Centre pixel is excluded so the
    convolution sum is exactly the 8-connected neighbour count.
    """
    kernel = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.int32)
    sk_int = skeleton.astype(np.int32)
    neighbour_count = convolve(sk_int, kernel, mode="constant", cval=0)
    on_skeleton = skeleton.astype(bool)
    bp_mask = on_skeleton & (neighbour_count >= _BRANCH_NEIGHBOR_MIN)
    ep_mask = on_skeleton & (neighbour_count == 1)
    branch_points = [(int(y), int(x)) for y, x in zip(*np.where(bp_mask))]
    endpoints     = [(int(y), int(x)) for y, x in zip(*np.where(ep_mask))]
    return branch_points, endpoints


def _prune_short_spurs(
    skeleton: np.ndarray,
    branch_points: List[Tuple[int, int]],
    endpoints: List[Tuple[int, int]],
    min_length: int,
) -> Tuple[np.ndarray, List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Walk from each endpoint along the skeleton; if the walk reaches
    a branch point in fewer than min_length steps, remove those
    pre-branch pixels.  Returns the pruned skeleton + refreshed
    branch / endpoint lists.
    """
    if not endpoints:
        return skeleton, branch_points, endpoints
    sk = skeleton.copy()
    bp_set = set(branch_points)
    for ep in endpoints:
        # Skip endpoints already pruned by a previous spur removal.
        if not sk[ep[0], ep[1]]:
            continue
        path = _walk_along_skeleton(sk, ep, bp_set, max_steps=min_length)
        if not path:
            continue
        # If the walk ended at a branch point AND the path is short,
        # remove all path pixels except the branch point itself.
        last = path[-1]
        if last in bp_set and len(path) - 1 < min_length:
            for (y, x) in path[:-1]:
                sk[y, x] = False
    new_bp, new_ep = _find_topology_points(sk)
    return sk, new_bp, new_ep


def _walk_along_skeleton(
    skeleton: np.ndarray,
    start: Tuple[int, int],
    stop_set,
    max_steps: int,
) -> List[Tuple[int, int]]:
    """Walk from start along the skeleton, taking the unique unvisited
    skeleton neighbour at each step, until reaching a stop pixel or
    `max_steps` steps.  Returns the path including start (and stop if
    reached).
    """
    path = [start]
    visited = {start}
    current = start
    for _ in range(max_steps):
        next_pixels = []
        cy, cx = current
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                ny, nx = cy + dy, cx + dx
                if (0 <= ny < skeleton.shape[0]
                        and 0 <= nx < skeleton.shape[1]
                        and skeleton[ny, nx]
                        and (ny, nx) not in visited):
                    next_pixels.append((ny, nx))
        if not next_pixels:
            return path
        # Spurs are 1-px wide → at most one valid neighbour mid-walk.
        # If multiple, we've hit a branch — return the branch as the
        # endpoint of this walk so the caller can decide.
        nxt = next_pixels[0]
        path.append(nxt)
        visited.add(nxt)
        if nxt in stop_set:
            return path
        current = nxt
    return path


def _has_loops(skeleton: np.ndarray) -> bool:
    """Detect cycles in the skeleton via the Euler-characteristic-style
    test: count the number of holes in the inverted skeleton image
    minus the unbounded background.  A simply-connected tree has 0
    holes; a single loop has 1 hole; an island in a channel has 1
    hole.
    """
    if not skeleton.any():
        return False
    inverted = ~skeleton.astype(bool)
    _, n_inv_components = label(inverted)
    # Subtract 1 for the unbounded exterior region.
    n_holes = n_inv_components - 1
    return n_holes > 0
