"""Channel-centerline extraction prototype (a.k.a. M6f).

Standalone tool that turns a 381×184 minimap RGB crop into a small set
of smooth lines following the medial axis of the navigable channel,
with endpoints anchored at frame edges or dead-end tips.  Designed as
the geometric substrate for waypoint-picking in HugShoreGoal — replaces
the production skeleton-bearing path that's currently broken by ship/
sonar punch-holes in the production water_mask (see memory
`project_ship_sonar_punch_holes_in_water_mask.md`).

History
───────
Iterated 2026-06-10 from V8's sknw spur-prune approach through six
intermediate methods (M4c → M6e).  Each intermediate had a different
failure mode; M6f is the synthesis that passes the 10-frame test
corpus at `data/test_frames/channel_perception/` against the user's
hand-drawn ground truth at `data/test_frames/desired_path/`.

Pipeline
────────
1. **Trim** 6 px from top and 6 px from right.  Strips HUD chrome that
   would otherwise pollute V11's brightness threshold near those edges.
2. **V11 brightness channel mask** (`tools/perception_v10_channel_prototype.py::v11_brightness_channel_mask`).
3. **Restrict to ship's connected component**.  Disconnected off-disc
   dark blobs caught by the brightness threshold get dropped.
4. **Distance transform** (`scipy.ndimage.distance_transform_edt`).
5. **Anchor detection** — two sources:
   a. **Frame-edge anchors**: midpoint of the largest contiguous water
      run on each of the four frame edges (one anchor per edge max).
   b. **Dead-end anchors**: degree-1 nodes of the sknw graph after
      spur pruning at threshold 30, filtered to those with DT < 8
      (channel genuinely narrows there — distinguishes a real dead-
      end tip from a spur of a bank bump).
6. **DT² geodesic** Dijkstra from the topmost anchor to every other
   anchor.  Cost = 1 / (DT² + 0.5); paths prefer high-DT pixels
   (channel centerline).
7. **Perpendicular landing**: for each path ending at a frame-edge
   anchor, find the last point in the path that's ≥ 8 px inland;
   replace everything after with a straight perpendicular descent to
   the anchor.  Eliminates lateral wobble where the path approaches
   the edge through a wide flat-cost region.
8. **Unique-pixel dedup**: sort kept paths by raw length, longest
   first; drop any path contributing < 20 novel pixels beyond what's
   already covered.  Removes redundant overlaps from the root-to-each-
   leaf path expansion.

Output: list of (xs, ys) numpy arrays — one per centerline segment,
plus the anchor list for downstream consumers.

Test corpus
───────────
Demo block at the bottom runs against the 10 frames in
`data/test_frames/channel_perception/`.  See
`data/test_frames/desired_path/` for the hand-drawn ground truth used
to validate this prototype.

Known limitations
─────────────────
- Multi-channel deltas (t5_delta_apex) produce 3+ paths where the
  user's hand-drawn line shows just the main centerline.  Not wrong,
  just more detailed.
- "Real" dead-ends are detected by DT < 8 — a wider dead-end tip
  (>= 8 px from bank) gets dropped.  Tune `DEAD_END_DT_MAX` per region.
- The pipeline depends on V11's mask being clean of off-disc dark
  contamination, which works because of the top/right trim + ship-CC
  restriction.  Different minimap layouts may need adjustment.

Usage
─────
  python -m tools.centerline_extraction_prototype

  Reads frames from `data/test_frames/channel_perception/`.  Writes
  side-by-side composites to /tmp/centerline_m6f/.  Override
  TEST_FRAMES to point at other corpora.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

import numpy as np
import sknw
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import distance_transform_edt, label as cc_label
from skimage.morphology import skeletonize
from skimage.graph import route_through_array


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.perception_v10_channel_prototype import v11_brightness_channel_mask  # noqa: E402


# ── Tuneables ──────────────────────────────────────────────────────────

TRIM_TOP                = 0    # was 6 — defensive trim needed by the
TRIM_RIGHT              = 0    # older OmniParser-style MINIMAP_CROP that
                               # included overworld sliver + icon strip.
                               # Since 2026-07-24 MINIMAP_CROP is aligned
                               # to the disc rim, so any additional trim
                               # cuts into real disc content (e.g., clips
                               # the last lon/lat digit on the right).
SPUR_THRESHOLD          = 60
ANCHOR_MERGE_PX         = 14
DEAD_END_DT_MAX         = 8.0
EDGE_APPROACH_PX        = 8
EDGE_MARGIN_PX          = 6     # dead-ends within this many px of any frame
                                # edge are exits, not real dead-ends
# "Neighborhood is much wider than me" check for dead-end candidates.
# Skeleton spurs caused by mask holes (port markers, village icons,
# text overlays) look narrow at the tip but sit *inside* a wide water
# body — the max DT within NEIGHBORHOOD_PX of the candidate exceeds
# candidate DT by more than SPUR_WIDTH_RATIO.  Real narrow inlets
# have a slowly-tapering channel around them, so their neighborhood
# max DT is not that much bigger than their tip DT.
NEIGHBORHOOD_PX         = 20
SPUR_WIDTH_RATIO        = 2.5
# 2026-07-21: kept at 2.5.  A tighter 2.0 rejects too many legit
# narrow river dead-ends in sim (433 bounces vs 103 at 2.5).  The
# Cairo anchor spur that got through 2.5 (ratio 2.23) is now handled
# by the tactical layer's newly-visible LOCK gate instead —
# rejecting mode transitions to already-visible dead_ends.
MIN_EDGE_RUN_PX         = 6     # ignore edge water-runs shorter than this
MIN_UNIQUE_PX           = 20


class Anchor(NamedTuple):
    y: int
    x: int
    kind: str   # "edge" | "dead_end"


# ── Mask preparation ───────────────────────────────────────────────────


MASK_CLOSING_RADIUS = 7    # bridges gaps from village text overlays
                           # (~10-14 px wide) — text pixels inside the
                           # water are classified as land by V11, so
                           # closing before CC restriction keeps the
                           # river topology intact across text.
MASK_MIN_DT_PX = 5         # channel width >= 10 px required.  Filters
                           # narrow-water false-positives (bright icons
                           # like telescope/compass leaving thin
                           # water-classified pixels), and truly-narrow
                           # channels the ship physically can't thread.


def channel_mask_from_rgb(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """V11 brightness mask → morphological closing → restrict to ship CC
    → narrow-water filter → restrict to ship CC.  Returns
    (rgb, mask).  Pipeline steps:
      1. V11 brightness threshold
      2. binary_closing(disk(MASK_CLOSING_RADIUS)) — bridge text/sprite
         gaps in the mask (village labels, HUD text)
      3. Restrict to ship's connected component
      4. DT filter: keep pixels where distance-to-nearest-land >=
         MASK_MIN_DT_PX (channel width >= 2 × MIN_DT_PX)
      5. Re-restrict to ship's CC (in case narrow filter split the mask)

    The returned RGB is unchanged (the historical TRIM_TOP/RIGHT
    behaviour has been zeroed since MINIMAP_CROP is now disc-rim-
    precise as of 2026-07-24)."""
    from scipy.ndimage import distance_transform_edt
    from skimage.morphology import binary_closing, disk
    # Historical trim — TRIM_TOP/RIGHT default to 0 now (disc-rim
    # crop makes them unnecessary).  Kept for tools that override.
    img = rgb[TRIM_TOP:, :-TRIM_RIGHT] if TRIM_RIGHT > 0 and rgb.shape[1] > TRIM_RIGHT else \
          rgb[TRIM_TOP:] if TRIM_TOP > 0 else rgb
    raw, _ = v11_brightness_channel_mask(img)
    # Step 2: closing — bridge text/sprite gaps before CC ops
    closed = binary_closing(raw, disk(MASK_CLOSING_RADIUS))
    # Step 3: restrict to ship's CC (in closed mask)
    ship = _ship_xy(img)
    mask = _restrict_to_ship_cc(closed, ship)
    # Step 4: narrow-water filter
    dt = distance_transform_edt(mask)
    mask = mask & (dt >= MASK_MIN_DT_PX)
    # Step 5: re-restrict to ship's CC (narrow filter can split)
    mask = _restrict_to_ship_cc(mask, ship)
    return img, mask


def _ship_xy(rgb: np.ndarray) -> tuple[int, int] | None:
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    if not green.any():
        return None
    lab, _ = cc_label(green)
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    big = int(np.argmax(sizes))
    ys, xs = np.where(lab == big)
    return int(xs.mean()), int(ys.mean())


def _restrict_to_ship_cc(mask: np.ndarray,
                         ship_xy: tuple[int, int] | None) -> np.ndarray:
    if ship_xy is None:
        return mask
    sx, sy = ship_xy
    lab, n = cc_label(mask)
    if n == 0:
        return mask
    sid = int(lab[sy, sx]) if mask[sy, sx] else 0
    if sid == 0:
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        sid = int(np.argmax(sizes))
    return (lab == sid)


# ── Anchor detection ───────────────────────────────────────────────────


ANCHOR_SPUR_PRUNE_PX = 30      # spurs shorter than this get removed
ANCHOR_FRAME_MARGIN_PX = 3     # deg-1 node within this many px of any
                               # frame edge is classified as `edge`


def _anchors_from_pruned_skeleton(mask: np.ndarray) -> list[Anchor]:
    """Unified anchor extractor: skimage skeleton → sknw graph → prune
    spurs shorter than ANCHOR_SPUR_PRUNE_PX → surviving degree-1 nodes.

    Each surviving degree-1 node becomes an Anchor.  Classification:
      - within ANCHOR_FRAME_MARGIN_PX of any frame edge → kind='edge'
      - otherwise → kind='dead_end'

    Rationale (2026-07-24): the old two-function approach
    (_frame_edge_anchors on edge water-runs + _dead_end_anchors on
    skeleton leaves with DT filter) had inconsistent semantics:
      - _frame_edge_anchors counted every water-run midpoint, even
        sprite-fragmented runs → 3-5 anchors on the same physical edge
      - _dead_end_anchors was too restrictive (DT < 8) → filtered out
        real shore terminals in wide water
    The pruned-skeleton approach handles both uniformly.  Short spurs
    from NPC sprites or text artifacts (skeleton branches that dive
    into the water only briefly before rejoining the main line) are
    removed by the length-based prune.  Real exits and shore terminals
    have long skeleton arms that survive the prune."""
    if mask.sum() == 0:
        return []
    H, W = mask.shape
    skel = skeletonize(mask).astype(np.uint16)
    G = sknw.build_sknw(skel)
    G = _prune_spurs(G, ANCHOR_SPUR_PRUNE_PX)
    out: list[Anchor] = []
    for n in G.nodes:
        if G.degree(n) != 1:
            continue
        y, x = G.nodes[n].get("o", (0, 0))
        y, x = int(y), int(x)
        on_edge = (y <= ANCHOR_FRAME_MARGIN_PX
                   or y >= H - 1 - ANCHOR_FRAME_MARGIN_PX
                   or x <= ANCHOR_FRAME_MARGIN_PX
                   or x >= W - 1 - ANCHOR_FRAME_MARGIN_PX)
        # Snap edge anchors to the actual edge pixel for consistency
        # with existing callers that assume edge anchors sit at (0, x)
        # / (H-1, x) / (y, 0) / (y, W-1).
        if on_edge:
            if y <= ANCHOR_FRAME_MARGIN_PX:            y = 0
            elif y >= H - 1 - ANCHOR_FRAME_MARGIN_PX:  y = H - 1
            if x <= ANCHOR_FRAME_MARGIN_PX:            x = 0
            elif x >= W - 1 - ANCHOR_FRAME_MARGIN_PX:  x = W - 1
            out.append(Anchor(y, x, "edge"))
        else:
            out.append(Anchor(y, x, "dead_end"))
    return out


def _frame_edge_anchors(mask: np.ndarray) -> list[Anchor]:
    """Frame-edge anchors (backwards-compat wrapper).  Now derived from
    the pruned skeleton rather than raw edge water-runs — see
    `_anchors_from_pruned_skeleton`."""
    return [a for a in _anchors_from_pruned_skeleton(mask) if a.kind == "edge"]


def _merge_degree_2(G):
    merged = True
    while merged:
        merged = False
        for node in list(G.nodes):
            if G.degree(node) == 2:
                nbrs = list(G.neighbors(node))
                if len(nbrs) != 2 or nbrs[0] == nbrs[1]:
                    continue
                n1, n2 = nbrs
                d1 = G.get_edge_data(node, n1)
                d2 = G.get_edge_data(node, n2)
                if d1 is None or d2 is None:
                    continue
                pts1 = d1.get("pts", np.array([]).reshape(0, 2))
                pts2 = d2.get("pts", np.array([]).reshape(0, 2))
                o_pt = np.array(G.nodes[node].get("o", (0, 0))).reshape(1, 2)
                new_pts = np.vstack([pts1, o_pt, pts2])
                new_w = d1.get("weight", 0) + d2.get("weight", 0)
                G.remove_node(node)
                if not G.has_edge(n1, n2):
                    G.add_edge(n1, n2, pts=new_pts, weight=new_w)
                merged = True
                break
    return G


def _prune_spurs(G, threshold):
    while True:
        leaves = [n for n in G.nodes if G.degree(n) == 1]
        pruned = False
        for leaf in leaves:
            nbrs = list(G.neighbors(leaf))
            if not nbrs:
                continue
            d = G.get_edge_data(leaf, nbrs[0])
            if d and d.get("weight", 0) < threshold:
                G.remove_node(leaf)
                pruned = True
        G = _merge_degree_2(G)
        if not pruned:
            break
    return G


def _dead_end_anchors(mask: np.ndarray, dt: np.ndarray) -> list[Anchor]:
    """Dead-end anchors (backwards-compat wrapper).  Now derived from
    the same pruned skeleton as frame-edge anchors — see
    `_anchors_from_pruned_skeleton`.  The `dt` parameter is retained
    for signature compatibility but no longer used (the DT filter has
    moved into `channel_mask_from_rgb` where narrow water pixels are
    already excluded from the mask)."""
    return [a for a in _anchors_from_pruned_skeleton(mask) if a.kind == "dead_end"]


def _merge_with_priority(edge_anchors: list[Anchor],
                        dead_anchors: list[Anchor]) -> list[Anchor]:
    """Keep all edge anchors.  Drop interior anchors close to an edge
    anchor (same exit, just inset)."""
    kept = list(edge_anchors)
    for a in dead_anchors:
        if not any((a.y - k.y) ** 2 + (a.x - k.x) ** 2
                   < ANCHOR_MERGE_PX ** 2 for k in kept):
            kept.append(a)
    return kept


# ── Path computation ───────────────────────────────────────────────────


def _shortest_path(a: Anchor, b: Anchor, cost: np.ndarray) -> np.ndarray | None:
    try:
        path, _ = route_through_array(
            cost, (a.y, a.x), (b.y, b.x), fully_connected=True,
        )
    except (ValueError, IndexError):
        return None
    return np.array(path)


def _edge_kind(anchor: Anchor, H: int, W: int) -> str:
    if anchor.y == 0:     return "top"
    if anchor.y == H - 1: return "bottom"
    if anchor.x == 0:     return "left"
    if anchor.x == W - 1: return "right"
    return ""


def _dist_to_edge(y: int, x: int, H: int, W: int, edge: str) -> int:
    if edge == "top":    return y
    if edge == "bottom": return (H - 1) - y
    if edge == "left":   return x
    if edge == "right":  return (W - 1) - x
    return 0


def _perpendicular_landing(path_yx: np.ndarray, anchor: Anchor,
                          H: int, W: int) -> np.ndarray:
    """Drop the path's tail within EDGE_APPROACH_PX of the edge,
    replace with a perpendicular straight descent to the anchor."""
    if anchor.kind != "edge":
        return path_yx
    edge = _edge_kind(anchor, H, W)
    if not edge:
        return path_yx
    departure_idx = None
    for i in range(len(path_yx) - 1, -1, -1):
        y, x = int(path_yx[i, 0]), int(path_yx[i, 1])
        if _dist_to_edge(y, x, H, W, edge) >= EDGE_APPROACH_PX:
            departure_idx = i
            break
    if departure_idx is None:
        return np.array([[anchor.y, anchor.x]])
    head = path_yx[: departure_idx + 1]
    dy_, dx_ = int(head[-1, 0]), int(head[-1, 1])
    ts = np.linspace(0, 1, EDGE_APPROACH_PX + 1)
    ys = np.round(dy_ + ts * (anchor.y - dy_)).astype(np.int32)
    xs = np.round(dx_ + ts * (anchor.x - dx_)).astype(np.int32)
    tail = np.column_stack([ys, xs])
    return np.vstack([head, tail[1:]])


def _dedup_paths(paths: list[tuple[np.ndarray, np.ndarray]]
                ) -> list[tuple[np.ndarray, np.ndarray]]:
    """Greedy: keep paths whose unique-pixel contribution >= MIN_UNIQUE_PX."""
    if not paths:
        return paths
    path_pix = [set(zip(ys.tolist(), xs.tolist())) for xs, ys in paths]
    order = sorted(range(len(paths)), key=lambda i: -len(path_pix[i]))
    kept_idx: list[int] = []
    covered: set = set()
    for i in order:
        unique = path_pix[i] - covered
        if len(unique) >= MIN_UNIQUE_PX:
            kept_idx.append(i)
            covered |= path_pix[i]
    return [paths[i] for i in kept_idx]


# ── Public entry point ─────────────────────────────────────────────────


def extract_centerlines(
    mask: np.ndarray,
) -> tuple[list[tuple[np.ndarray, np.ndarray]], list[Anchor]]:
    """End-to-end: V11 channel mask → list of (xs, ys) centerline arrays
    + anchor list (with `.kind` ∈ {"edge", "dead_end"}).

    Caller responsibilities:
      - Compute `mask` via `channel_mask_from_rgb(rgb)` first.
      - The mask must be in the trimmed coordinate frame (i.e. the RGB
        from `channel_mask_from_rgb`, not the original 381×184 crop).
    """
    H, W = mask.shape
    dt = distance_transform_edt(mask)
    cost = 1.0 / (dt * dt + 0.5)
    cost[~mask] = 1e6

    edge_anchors = _frame_edge_anchors(mask)
    dead_anchors = _dead_end_anchors(mask, dt)
    anchors = _merge_with_priority(edge_anchors, dead_anchors)
    if len(anchors) < 2:
        return [], anchors

    root = sorted(anchors, key=lambda a: (a.kind != "edge", a.y, a.x))[0]
    raw_paths: list[np.ndarray] = []
    for leaf in anchors:
        if leaf is root:
            continue
        p = _shortest_path(root, leaf, cost)
        if p is None:
            continue
        p = _perpendicular_landing(p, leaf, H, W)
        if root.kind == "edge":
            p = _perpendicular_landing(p[::-1], root, H, W)[::-1]
        raw_paths.append(p)

    paths = [(p[:, 1], p[:, 0]) for p in raw_paths]
    paths = _dedup_paths(paths)
    return paths, anchors


# ── Demo runner ─────────────────────────────────────────────────────────


TEST_DIR = ROOT / "data/test_frames/channel_perception"
GT_DIR   = ROOT / "data/test_frames/desired_path"

DEMO_FRAMES = [
    ("t5_delta",     "v13_scenarios/tick_0005.png",              "tick_0005-path.png"),
    ("t100_clean",   "v13_scenarios/tick_0100.png",              "tick_0100-path.png"),
    ("t152_Nubia",   "v13_scenarios/tick_0152.png",              "tick_0152-path.png"),
    ("t300_bend",    "v13_scenarios/tick_0300.png",              "tick_0300-path.png"),
    ("t475_Y",       "v13_scenarios/tick_0475.png",              "tick_0475-path.png"),
    ("t500_deadend", "v13_scenarios/tick_0500.png",              "tick_0500-path.png"),
    ("t571_Bari",    "v13_scenarios/tick_0571_bari_village.png", "tick_0571_bari_village-path.png"),
    ("t750_Ubend",   "v13_scenarios/tick_0750_y_branch.png",     "tick_0750_y_branch-path.png"),
    ("t786_lake",    "v13_scenarios/tick_0786_lake_at_end.png",  "tick_0786_lake_at_end-path.png"),
    ("t83_dawn",     "hug_debug_specials/tick_0083.png",         "tick_0083-path.png"),
]


def _run_demo():
    out_dir = Path("/tmp/centerline_m6f")
    out_dir.mkdir(exist_ok=True)
    for tick_label, src_rel, gt_basename in DEMO_FRAMES:
        rgb = np.asarray(Image.open(TEST_DIR / src_rel).convert("RGB"))
        rgb_trimmed, mask = channel_mask_from_rgb(rgb)
        paths, anchors = extract_centerlines(mask)
        ne = sum(1 for a in anchors if a.kind == "edge")
        nd = sum(1 for a in anchors if a.kind == "dead_end")
        print(f"{tick_label:<14}  paths={len(paths):>2}  anchors=edge:{ne}/dead:{nd}")

        fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
        axes[0].imshow(rgb_trimmed); axes[0].axis("off")
        axes[0].set_title(f"{tick_label}  INPUT (trimmed)", fontsize=10)

        axes[1].imshow(rgb_trimmed, alpha=0.55)
        mo = np.zeros((*mask.shape, 4)); mo[mask] = [0, 0.4, 1.0, 0.30]
        axes[1].imshow(mo)
        for xs, ys in paths:
            axes[1].scatter(xs, ys, s=4, c="red", alpha=0.95)
        ex = np.array([a.x for a in anchors if a.kind == "edge"])
        ey = np.array([a.y for a in anchors if a.kind == "edge"])
        dx = np.array([a.x for a in anchors if a.kind == "dead_end"])
        dy = np.array([a.y for a in anchors if a.kind == "dead_end"])
        if ex.size: axes[1].scatter(ex, ey, s=80, c="yellow",
                                    edgecolors="black", linewidths=0.8, zorder=5)
        if dx.size: axes[1].scatter(dx, dy, s=80, c="cyan",
                                    edgecolors="black", linewidths=0.8, zorder=5)
        axes[1].axis("off")
        axes[1].set_title(f"M6f centerlines  paths={len(paths)}",
                          fontsize=10)

        gt_path = GT_DIR / gt_basename
        if gt_path.exists():
            axes[2].imshow(np.asarray(Image.open(gt_path).convert("RGB")))
        axes[2].axis("off")
        axes[2].set_title("hand-drawn ground truth", fontsize=10)

        fig.tight_layout()
        fig.savefig(out_dir / f"{tick_label}.png",
                    dpi=110, bbox_inches="tight")
        plt.close(fig)
    print(f"\nSaved {len(DEMO_FRAMES)} composites to {out_dir}")


if __name__ == "__main__":
    _run_demo()
