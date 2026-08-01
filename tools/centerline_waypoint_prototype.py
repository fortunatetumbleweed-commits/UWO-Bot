"""Centerline-waypoint prototype.

Consumes the M6f centerline tree from
`tools.centerline_extraction_prototype` and picks a destination point
that's always in view — `LOOKAHEAD_PX` (=50) pixels ahead of the ship
along the centerline tree.

Pipeline
────────
1. Extract M6f tree (anchors + DT² geodesic edges).
2. Build a node-edge tree:
     - nodes  = root + dead-end anchors + junctions where 2+ paths share
                a common prefix
     - edges  = polylines (full pixel path) between adjacent tree nodes
3. Locate the ship: green icon centroid (`_ship_xy`) + principal axis
   (PCA on green pixels) for heading.  Front/back disambiguated by
   the half whose centroid sits further along the locally-traversed
   centerline edge.
4. Find the ship's nearest edge by projecting `ship_xy` onto every
   polyline.  That edge is "current."
5. Walk forward along the tree, accumulating arc-length distance,
   until LOOKAHEAD_PX is reached.  At each junction:
     - Enumerate outgoing edges (exclude the one we entered from).
     - Score each by bearing-relative-to-heading:
         * "behind" (cos(angle) < 0 vs heading) → priority 2, used only
            if no forward option exists.
         * forward options → priority 1, sorted by hug-side rule:
            HUG_LEFT  → pick the most-counter-clockwise (left of heading)
            HUG_RIGHT → pick the most-clockwise (right of heading)
6. Return the destination point + the walked path.

Output
──────
Renders side-by-side composites for every frame in
`centerline_extraction_prototype.DEMO_FRAMES` to /tmp/centerline_waypoint/.

Usage
─────
  python -m tools.centerline_waypoint_prototype
  python -m tools.centerline_waypoint_prototype --hug right
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import distance_transform_edt
from skimage.graph import route_through_array

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.centerline_extraction_prototype import (  # noqa: E402
    channel_mask_from_rgb, _frame_edge_anchors, _dead_end_anchors,
    _merge_with_priority, _perpendicular_landing, _ship_xy,
    DEMO_FRAMES, TEST_DIR,
)


LOOKAHEAD_PX = 50
HUG_SIDE     = "left"     # "left" | "right"


# ── Tree representation ────────────────────────────────────────────────

@dataclass
class TreeNode:
    id: str
    y: int
    x: int
    kind: str   # "root_edge" | "leaf_edge" | "leaf_dead_end" | "junction"

@dataclass
class TreeEdge:
    a: str             # node id
    b: str             # node id
    points: np.ndarray # (n, 2) array of (y, x) pixels along the edge

@dataclass
class Tree:
    nodes: dict[str, TreeNode]
    edges: list[TreeEdge]

    def neighbors(self, nid: str) -> list[tuple[str, TreeEdge]]:
        out = []
        for e in self.edges:
            if e.a == nid: out.append((e.b, e))
            elif e.b == nid: out.append((e.a, e))
        return out


def extract_tree(rgb: np.ndarray) -> tuple[Tree, np.ndarray]:
    """Returns (tree, mask).  Mask is in the trimmed coord frame."""
    img, mask = channel_mask_from_rgb(rgb)
    return extract_tree_from_mask(mask), mask


def extract_tree_from_mask(mask: np.ndarray) -> Tree:
    """Build the centerline Tree from a precomputed water mask.

    Same algorithm as `extract_tree`, but lets L3 planners that
    already have an L2 mask skip the RGB → mask step.  Returns just
    the Tree (the caller already has the mask).
    """
    H, W = mask.shape
    dt = distance_transform_edt(mask)
    cost = 1.0 / (dt * dt + 0.5); cost[~mask] = 1e6

    anchors = _merge_with_priority(_frame_edge_anchors(mask),
                                   _dead_end_anchors(mask, dt))
    if len(anchors) < 2:
        return Tree({}, [])
    root = sorted(anchors, key=lambda a: (a.kind != "edge", a.y, a.x))[0]
    paths = []
    for leaf in anchors:
        if leaf is root: continue
        p, _ = route_through_array(cost, (root.y, root.x), (leaf.y, leaf.x),
                                   fully_connected=True)
        p = np.array(p)
        p = _perpendicular_landing(p, leaf, H, W)
        if root.kind == "edge":
            p = _perpendicular_landing(p[::-1], root, H, W)[::-1]
        paths.append((p, leaf))

    # Find junctions = last-shared-pixel between path pairs.
    junctions = set()
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            pi, pj = paths[i][0], paths[j][0]
            last = None
            for k in range(min(len(pi), len(pj))):
                if (pi[k, 0], pi[k, 1]) == (pj[k, 0], pj[k, 1]):
                    last = (int(pi[k, 0]), int(pi[k, 1]))
                else:
                    break
            if last is not None:
                junctions.add(last)
    junctions.discard((root.y, root.x))

    nodes: dict[str, TreeNode] = {}
    pos2id: dict[tuple[int, int], str] = {}
    def add(y, x, kind) -> str:
        key = (y, x)
        if key in pos2id: return pos2id[key]
        nid = f"N{len(nodes)}"
        nodes[nid] = TreeNode(nid, y, x, kind)
        pos2id[key] = nid
        return nid
    add(root.y, root.x, f"root_{root.kind}")
    for jy, jx in junctions: add(jy, jx, "junction")
    for _, leaf in paths: add(leaf.y, leaf.x, f"leaf_{leaf.kind}")

    edges: list[TreeEdge] = []
    seen = set()
    for p, _ in paths:
        pts = [(int(p[k, 0]), int(p[k, 1])) for k in range(len(p))]
        bps = [0]
        for k, pt in enumerate(pts):
            if pt in pos2id and k not in (0, len(pts) - 1):
                bps.append(k)
        bps.append(len(pts) - 1)
        bps = sorted(set(bps))
        for a, b in zip(bps, bps[1:]):
            seg = pts[a:b + 1]
            fid, tid = pos2id[seg[0]], pos2id[seg[-1]]
            key = tuple(sorted((fid, tid)))
            if key in seen: continue
            seen.add(key)
            edges.append(TreeEdge(fid, tid, np.array(seg)))
    return Tree(nodes, edges)


# ── Ship pose ──────────────────────────────────────────────────────────


def estimate_ship_heading(rgb: np.ndarray) -> tuple[float, float] | None:
    """Returns a unit (dy, dx) vector for the ship's forward direction,
    by PCA on green pixels.  Sign is disambiguated using the brighter
    half (the bow tip tends to be brighter than the stern)."""
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    ys, xs = np.where(green)
    if len(ys) < 8:
        return None
    pts = np.column_stack([ys, xs]).astype(np.float64)
    cy, cx = pts.mean(axis=0)
    centered = pts - [cy, cx]
    cov = centered.T @ centered / len(pts)
    eigvals, eigvecs = np.linalg.eigh(cov)
    axis = eigvecs[:, -1]   # principal axis = (dy, dx)
    # Disambiguate front/back by brightness sum on each half.
    proj = centered @ axis
    front_half = proj > 0
    back_half = proj < 0
    bright = R + G + B
    fb = bright[ys[front_half], xs[front_half]].sum() if front_half.any() else 0
    bb = bright[ys[back_half],  xs[back_half]].sum()  if back_half.any() else 0
    if bb > fb:
        axis = -axis
    return float(axis[0]), float(axis[1])


# ── Geometry helpers ───────────────────────────────────────────────────


def project_point_onto_polyline(py: int, px: int,
                                poly: np.ndarray) -> tuple[float, int, float]:
    """Returns (distance², segment_index, t) where t ∈ [0,1] on segment."""
    best = (float("inf"), 0, 0.0)
    for i in range(len(poly) - 1):
        ay, ax = poly[i]
        by, bx = poly[i + 1]
        dy_, dx_ = by - ay, bx - ax
        L2 = dy_ * dy_ + dx_ * dx_
        if L2 == 0:
            t = 0.0
            cy, cx = ay, ax
        else:
            t = ((py - ay) * dy_ + (px - ax) * dx_) / L2
            t = max(0.0, min(1.0, t))
            cy = ay + t * dy_
            cx = ax + t * dx_
        d2 = (py - cy) ** 2 + (px - cx) ** 2
        if d2 < best[0]:
            best = (d2, i, t)
    return best


def find_ship_edge(tree: Tree, ship_xy: tuple[int, int]
                  ) -> tuple[TreeEdge | None, int, float]:
    """Returns (edge, segment_index, t) for the tree edge closest to
    the ship.  Edge polyline coords are oriented a→b."""
    sx, sy = ship_xy
    best = (float("inf"), None, 0, 0.0)
    for e in tree.edges:
        d2, i, t = project_point_onto_polyline(sy, sx, e.points)
        if d2 < best[0]:
            best = (d2, e, i, t)
    return best[1], best[2], best[3]


def polyline_length(poly: np.ndarray, start_i: int, start_t: float,
                   end_i: int | None = None, end_t: float | None = None
                   ) -> float:
    """Arc length along `poly` from (start_i, start_t) to the end (or
    optionally to (end_i, end_t))."""
    if end_i is None:
        end_i = len(poly) - 2
        end_t = 1.0
    # Start point.
    sy, sx = poly[start_i]
    ny, nx = poly[start_i + 1]
    cy = sy + start_t * (ny - sy)
    cx = sx + start_t * (nx - sx)
    total = 0.0
    # First partial segment.
    if start_i == end_i:
        ey = sy + end_t * (ny - sy)
        ex = sx + end_t * (nx - sx)
        return float(np.hypot(ey - cy, ex - cx))
    total += float(np.hypot(ny - cy, nx - cx))
    for i in range(start_i + 1, end_i):
        ay, ax = poly[i]
        by, bx = poly[i + 1]
        total += float(np.hypot(by - ay, bx - ax))
    ay, ax = poly[end_i]
    by, bx = poly[end_i + 1]
    fy = ay + end_t * (by - ay)
    fx = ax + end_t * (bx - ax)
    total += float(np.hypot(fy - ay, fx - ax))
    return total


def walk_polyline(poly: np.ndarray, start_i: int, start_t: float,
                 remaining: float, reverse: bool = False
                 ) -> tuple[tuple[int, int] | None, float, list[tuple[int, int]]]:
    """Walk `remaining` arc-length along `poly` starting from
    (start_i, start_t).  Returns (endpoint, walked_distance, trace).
    endpoint is None if we ran out of polyline before consuming
    `remaining` — caller handles by transitioning at the node."""
    p = poly[::-1] if reverse else poly
    # Re-map start_i/start_t under reverse.
    if reverse:
        n_seg = len(poly) - 1
        start_i = (n_seg - 1) - start_i
        start_t = 1.0 - start_t
    trace = []
    sy, sx = p[start_i]
    ny, nx = p[start_i + 1]
    cy = sy + start_t * (ny - sy)
    cx = sx + start_t * (nx - sx)
    trace.append((int(round(cy)), int(round(cx))))
    walked = 0.0
    seg_remain = float(np.hypot(ny - cy, nx - cx))
    if seg_remain >= remaining:
        frac = remaining / seg_remain if seg_remain > 0 else 0
        wy = cy + frac * (ny - cy)
        wx = cx + frac * (nx - cx)
        trace.append((int(round(wy)), int(round(wx))))
        return (int(round(wy)), int(round(wx))), remaining, trace
    walked += seg_remain
    trace.append((int(ny), int(nx)))
    for i in range(start_i + 1, len(p) - 1):
        ay, ax = p[i]
        by, bx = p[i + 1]
        L = float(np.hypot(by - ay, bx - ax))
        if walked + L >= remaining:
            frac = (remaining - walked) / L if L > 0 else 0
            wy = ay + frac * (by - ay)
            wx = ax + frac * (bx - ax)
            trace.append((int(round(wy)), int(round(wx))))
            return (int(round(wy)), int(round(wx))), remaining, trace
        walked += L
        trace.append((int(by), int(bx)))
    return None, walked, trace


# ── Junction choice ────────────────────────────────────────────────────


def _bearing_from_edge_start(e: TreeEdge, start_node: TreeNode
                            ) -> tuple[float, float]:
    """Unit (dy, dx) of the edge's local direction leaving `start_node`."""
    poly = e.points if (e.points[0, 0], e.points[0, 1]) == (start_node.y, start_node.x) \
                   else e.points[::-1]
    # Use ~3-pixel lookahead for stable bearing.
    i = min(3, len(poly) - 1)
    dy = poly[i, 0] - poly[0, 0]
    dx = poly[i, 1] - poly[0, 1]
    n = float(np.hypot(dy, dx))
    if n == 0: return (0.0, 0.0)
    return (dy / n, dx / n)


def _signed_angle_deg(from_vec: tuple[float, float],
                     to_vec: tuple[float, float]) -> float:
    """Signed angle from `from_vec` to `to_vec`, in degrees.  Positive =
    counter-clockwise in image coordinates (y down, x right) which means
    "left from heading"."""
    fy, fx = from_vec
    ty, tx = to_vec
    cross = fx * ty - fy * tx     # z-component of cross (in image coords)
    dot   = fx * tx + fy * ty
    return float(np.degrees(np.arctan2(cross, dot)))


def pick_next_edge_at_junction(tree: Tree, junc: TreeNode,
                              entry_edge: TreeEdge,
                              heading: tuple[float, float],
                              hug_side: str) -> TreeEdge | None:
    """Score outgoing edges (not entry_edge) and pick by hug rule.
    Returns None if no outgoing edges exist (terminal)."""
    candidates = [(nbr_id, e) for nbr_id, e in tree.neighbors(junc.id)
                  if e is not entry_edge]
    if not candidates:
        return None
    scored = []
    for nbr_id, e in candidates:
        b = _bearing_from_edge_start(e, junc)
        ang = _signed_angle_deg(heading, b)
        dot = heading[0] * b[0] + heading[1] * b[1]
        forward = dot > 0
        scored.append((nbr_id, e, b, ang, forward))
    forwards = [s for s in scored if s[4]]
    pool = forwards if forwards else scored   # fall back to "behind"
    # Sign flipped 2026-06-13 after the live Cairo→lake run took the
    # eastern Y arm when the user expected the western Y-tip arm.  Per
    # the user's mental model "left" = the WEST arm when heading south
    # (cartographic / map-view left, not nautical port-side hugging).
    # Picker semantic now: hug_side="left" → pick the branch that
    # rotates the bot clockwise in image coords (which is leftward in
    # the map view when y-up is north).
    if hug_side == "left":
        pool.sort(key=lambda s:  s[3])   # most negative (clockwise in image)
    else:
        pool.sort(key=lambda s: -s[3])   # most positive (counter-clockwise)
    return pool[0][1]


# ── Walking the tree ───────────────────────────────────────────────────


def _other_end(e: TreeEdge, nid: str) -> str:
    return e.b if e.a == nid else e.a


def _polyline_oriented(e: TreeEdge, from_nid: str, tree: Tree) -> np.ndarray:
    """Edge polyline oriented so it starts at `from_nid`."""
    a_pt = (tree.nodes[e.a].y, tree.nodes[e.a].x)
    p0 = (int(e.points[0, 0]), int(e.points[0, 1]))
    if (tree.nodes[from_nid].y, tree.nodes[from_nid].x) == p0:
        return e.points
    return e.points[::-1]


def pick_waypoint(tree: Tree, ship_xy: tuple[int, int],
                 heading: tuple[float, float],
                 hug_side: str = HUG_SIDE,
                 lookahead: int = LOOKAHEAD_PX
                 ) -> tuple[tuple[int, int] | None, list[tuple[int, int]], list[str]]:
    """Returns (destination_point, walked_trace_pixels, visited_node_ids)."""
    edge, seg_i, t = find_ship_edge(tree, ship_xy)
    if edge is None:
        return None, [], []
    # Decide which end of the current edge to walk toward.
    poly = edge.points
    # Project ship onto edge; bearing at projection toward each end.
    # Forward end = end whose direction-from-projection is most aligned
    # with heading.
    py, px = ship_xy[1], ship_xy[0]   # (y, x)
    sy = poly[seg_i, 0] + t * (poly[seg_i + 1, 0] - poly[seg_i, 0])
    sx = poly[seg_i, 1] + t * (poly[seg_i + 1, 1] - poly[seg_i, 1])
    end_a = poly[0]
    end_b = poly[-1]
    def _align(end):
        v = (end[0] - sy, end[1] - sx)
        n = float(np.hypot(v[0], v[1]))
        if n == 0: return -1.0
        return (v[0] / n) * heading[0] + (v[1] / n) * heading[1]
    if _align(end_b) >= _align(end_a):
        forward_nid = edge.b if (poly[-1, 0], poly[-1, 1]) == (tree.nodes[edge.b].y, tree.nodes[edge.b].x) else edge.a
        reverse = False
    else:
        forward_nid = edge.a if (poly[0, 0], poly[0, 1]) == (tree.nodes[edge.a].y, tree.nodes[edge.a].x) else edge.b
        reverse = True

    # Walk forward along edge starting from projection.
    remaining = float(lookahead)
    dest, walked, trace = walk_polyline(poly, seg_i, t, remaining, reverse=reverse)
    visited = []
    current_edge = edge
    current_node = forward_nid
    while dest is None:
        # We hit a node — `current_node` is where we arrived.
        visited.append(current_node)
        node = tree.nodes[current_node]
        remaining -= walked
        if node.kind != "junction":
            # Hit a leaf — destination = leaf.
            return (node.y, node.x), trace, visited
        # Junction — pick next edge by hug rule.
        nxt = pick_next_edge_at_junction(tree, node, current_edge,
                                         heading, hug_side)
        if nxt is None:
            return (node.y, node.x), trace, visited
        next_poly = _polyline_oriented(nxt, current_node, tree)
        dest, walked, trace_seg = walk_polyline(next_poly, 0, 0.0, remaining,
                                                reverse=False)
        # Append trace skipping first point (duplicate of the junction).
        trace.extend(trace_seg[1:])
        current_edge = nxt
        current_node = _other_end(nxt, current_node)
    return dest, trace, visited


# ── Rendering ──────────────────────────────────────────────────────────


def render(rgb: np.ndarray, mask: np.ndarray, tree: Tree,
          ship_xy, heading, dest, walked_trace, hug_side, out_path):
    fig, ax = plt.subplots(figsize=(12, 6.5))
    ax.imshow(rgb, alpha=0.55)
    mo = np.zeros((*mask.shape, 4)); mo[mask] = [0, 0.4, 1.0, 0.20]
    ax.imshow(mo)
    for e in tree.edges:
        ax.plot(e.points[:, 1], e.points[:, 0], c="red", lw=1.6, alpha=0.55)
    for n in tree.nodes.values():
        c = "magenta" if n.kind == "junction" else (
            "yellow" if "edge" in n.kind else "cyan")
        ax.scatter([n.x], [n.y], s=110, c=c,
                   edgecolors="black", linewidths=0.9, zorder=4)
    # Walked path in bright green.
    if walked_trace:
        ts = np.array(walked_trace)
        ax.plot(ts[:, 1], ts[:, 0], c="lime", lw=3.0, alpha=0.95, zorder=6)
    # Ship.
    if ship_xy is not None:
        sx, sy = ship_xy
        ax.scatter([sx], [sy], s=320, c="lime",
                   edgecolors="black", linewidths=1.5, marker="o", zorder=7)
        if heading is not None:
            hy, hx = heading
            ax.arrow(sx, sy, hx * 28, hy * 28, head_width=8,
                     fc="lime", ec="black", lw=1.2, zorder=8,
                     length_includes_head=True)
    # Destination.
    if dest is not None:
        dy, dx = dest
        ax.scatter([dx], [dy], s=520, c="orange",
                   edgecolors="black", linewidths=1.8, marker="*", zorder=9)
        ax.annotate(f"WAYPOINT\n({LOOKAHEAD_PX}px)", (dx, dy),
                    color="black", fontsize=9, fontweight="bold",
                    xytext=(12, 4), textcoords="offset points",
                    bbox=dict(boxstyle="round,pad=0.3", fc="orange",
                              ec="black", alpha=0.85))
    ax.set_title(f"{out_path.stem}  —  hug={hug_side}  "
                 f"lookahead={LOOKAHEAD_PX}px",
                 fontsize=12)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hug", choices=("left", "right"), default=HUG_SIDE)
    ap.add_argument("--lookahead", type=int, default=LOOKAHEAD_PX)
    args = ap.parse_args()
    out_dir = Path("/tmp/centerline_waypoint")
    out_dir.mkdir(exist_ok=True)
    print(f"hug={args.hug}  lookahead={args.lookahead}px\n")
    for label, src_rel, _ in DEMO_FRAMES:
        rgb_raw = np.asarray(Image.open(TEST_DIR / src_rel).convert("RGB"))
        tree, mask = extract_tree(rgb_raw)
        rgb = rgb_raw[6:, :-6] if rgb_raw.shape[1] > 6 else rgb_raw   # match trim
        ship = _ship_xy(rgb)
        heading = estimate_ship_heading(rgb)
        if tree.nodes and ship is not None and heading is not None:
            dest, walked, visited = pick_waypoint(
                tree, ship, heading, args.hug, args.lookahead)
        else:
            dest, walked, visited = None, [], []
        print(f"  {label:<14}  nodes={len(tree.nodes):>2} edges={len(tree.edges):>2}  "
              f"ship={ship} hdg=({heading[0]:+.2f},{heading[1]:+.2f}) "
              f"dest={dest}  visited={visited}")
        render(rgb, mask, tree, ship, heading, dest, walked,
               args.hug, out_dir / f"{label}.png")
    print(f"\nSaved composites to {out_dir}")


if __name__ == "__main__":
    main()
