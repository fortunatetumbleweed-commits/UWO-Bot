"""Shore-hug waypoint picker — unified primitive for river / lake / coast.

Replaces the M6f centerline approach with a single geometric rule:

  waypoint = LOOKAHEAD_PX ahead of ship in forward direction,
             offset SAFE_DISTANCE_PX away from the chosen shore

Properties:
  - In a narrow river: SAFE_DISTANCE_PX > half channel-width, so the
    waypoint lands roughly on the centerline.  Identical behavior to
    M6f for the narrow case.
  - In open water with one coast: waypoint sits SAFE_DISTANCE_PX off
    the chosen shore.  Bot follows the coast.
  - In a strait: bot follows the chosen wall, passes through, emerges.
  - At a Y junction: bot continues hugging the same shore → the
    "chosen Y arm" is whichever side has the same bank continuous.
  - In a lake: bot follows the perimeter on the chosen side.

Coordinate conventions:
  - image coords: y-down, x-right
  - bearing_deg: 0=N, 90=E, 180=S, 270=W (CW from north)
  - "left" semantic: cartographic LEFT (north-up map view) — when
    heading south, left = west.  This matches the picker's flipped
    convention (commit 01a3a52).  In nautical terms this would be
    "starboard hug" for a south-heading ship.

Test corpus
───────────
Runs against `data/test_frames/channel_perception/v13_scenarios/`.
Renders composites side-by-side showing the M6f result vs the new
shore-hug result for each frame.

Usage
─────
  python -m tools.shore_waypoint_picker
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.centerline_extraction_prototype import (  # noqa: E402
    channel_mask_from_rgb, _ship_xy,
)
from tools.centerline_waypoint_prototype import (  # noqa: E402
    estimate_ship_heading, extract_tree, pick_waypoint as m6f_pick_waypoint,
    LOOKAHEAD_PX,
)


# ── Tuneables ──────────────────────────────────────────────────────────

DEFAULT_LOOKAHEAD = 50           # px ahead to look
DEFAULT_SAFE_DISTANCE = 25       # px to stay away from chosen shore
MAX_RAY_DISTANCE = 250           # px - cap on how far to search for shore
SHORE_VIEW_RADIUS = 130          # px - how far around ship to trace shore
SHORE_BEHIND_TOLERANCE = 30      # px - allow shore points slightly behind ship


# ── Bearing → image-vec helpers ────────────────────────────────────────


def bearing_to_image_vec(bearing_deg: float) -> tuple[float, float]:
    """World bearing (0=N, 90=E, CW) → image unit vector (dy, dx)."""
    r = math.radians(bearing_deg)
    return (-math.cos(r), math.sin(r))


def image_vec_to_bearing(dy: float, dx: float) -> float:
    return math.degrees(math.atan2(dx, -dy)) % 360.0


# ── Ray cast to find shore ─────────────────────────────────────────────


def cast_ray_to_land(mask: np.ndarray, start_xy: tuple[float, float],
                    bearing_deg: float,
                    max_distance: int = MAX_RAY_DISTANCE
                    ) -> tuple[int, int] | None:
    """March a ray from `start_xy` toward `bearing_deg` until we hit a
    land pixel.  Returns (px, py) image coords of the first land pixel
    hit, or None if the ray exits the frame still in water.
    """
    sx, sy = start_xy
    dy, dx = bearing_to_image_vec(bearing_deg)
    H, W = mask.shape
    for step in range(1, max_distance):
        py = int(round(sy + step * dy))
        px = int(round(sx + step * dx))
        if not (0 <= py < H and 0 <= px < W):
            return None
        if not mask[py, px]:
            return (px, py)
    return None


# ── Shore polyline extraction ──────────────────────────────────────────


def _find_containing_contour(contours, target_xy: tuple[float, float]
                            ) -> tuple[np.ndarray, int] | None:
    """Find the contour (and index within it) of the point closest to
    target_xy.  Returns (contour, index) or None.

    `target_xy` is (x, y) in image coords; skimage contours have rows
    of (y, x).  We convert internally.
    """
    tx, ty = target_xy
    best = (float("inf"), None, None)
    for contour in contours:
        # contour is shape (n, 2) with rows (y, x)
        dys = contour[:, 0] - ty
        dxs = contour[:, 1] - tx
        d2s = dys * dys + dxs * dxs
        i = int(np.argmin(d2s))
        if d2s[i] < best[0]:
            best = (float(d2s[i]), contour, i)
    if best[1] is None:
        return None
    return best[1], best[2]


def _walk_contour_by_distance(contour: np.ndarray, start_idx: int,
                             step: int, target_distance: float
                             ) -> tuple[int, list[int]]:
    """Walk along `contour` starting at `start_idx` in direction `step`
    (+1 forward through the list, -1 backward).  Stop after
    `target_distance` pixels of cumulative arc length.

    Returns (end_idx, indices_walked).  indices_walked is in order
    from start to end.
    """
    n = len(contour)
    walked_idxs = [start_idx]
    walked_dist = 0.0
    idx = start_idx
    prev = contour[idx]
    safety_cap = n + 1
    while walked_dist < target_distance and safety_cap > 0:
        safety_cap -= 1
        next_idx = (idx + step) % n
        curr = contour[next_idx]
        d = math.hypot(curr[1] - prev[1], curr[0] - prev[0])
        walked_dist += d
        walked_idxs.append(next_idx)
        prev = curr
        idx = next_idx
        if idx == start_idx:
            break   # closed the loop
    return idx, walked_idxs


def extract_shore_path(mask: np.ndarray,
                      ship_xy: tuple[int, int] | None,
                      heading_deg: float,
                      side: str = "port",
                      walk_distance_px: int = 80,
                      safe_distance_px: int = DEFAULT_SAFE_DISTANCE,
                      walk_direction_deg: float | None = None,
                      ) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Ship-anchored shore-walk picker (v2).

    Algorithm:
      1. Treat the ship as fixed at frame center (the bot is always
         centered on the minimap, regardless of icon occlusion).
      2. Cast a ray from ship toward the chosen shore direction
         (west=270° for hug-left, east=90° for hug-right).  Find the
         first land pixel hit — this is the NEAREST shore on the
         chosen side.
      3. Find the contour (connected polyline of bank pixels) that
         contains this nearest point.  This is the actual shore the
         bot will follow — not other contours from peninsulas / islands
         / sprite holes.
      4. Walk along that contour by walk_distance_px in the direction
         that aligns most with `walk_direction_deg` (default:
         `heading_deg`).  When the bot has a persistent
         commit_direction that differs from the per-tick bow heading
         (e.g. just after a collision-bounce), pass the commit value
         here to keep the picker walking the SAME direction along the
         shore the bot was already following.  Without this argument
         the picker would happily reverse direction every time the
         bow rotates, which is the t150→t151 failure pattern from the
         2026-06-17 voyage.
      5. Offset the walked path by safe_distance_px perpendicular to
         the shore, INTO water.

    Returns (shore_pts, path_pts):
      shore_pts: walked bank pixels (the actual shore line followed)
      path_pts:  same points offset into water — the bot's path

    `ship_xy` is accepted for legacy callers but if None the frame
    center is used (recommended: pass None to rely on the
    "ship-at-center" invariant).
    """
    from skimage.measure import find_contours

    H, W = mask.shape
    if ship_xy is None:
        ship_xy = (W // 2, H // 2)
    sx, sy = ship_xy

    # CARTOGRAPHIC: hug-left = west (270°), hug-right = east (90°).
    # BOW-RELATIVE nautical convention.
    # side="port"      = bot's left = bearing heading - 90°
    # side="starboard" = bot's right = bearing heading + 90°
    # Heading south, port = east bank; heading north, port = west bank.
    shore_bearing = (heading_deg - 90) % 360 if side == "port" \
                   else (heading_deg + 90) % 360

    # 1. Find nearest shore pixel by casting a ray from ship.
    nearest = cast_ray_to_land(mask, (sx, sy), shore_bearing)
    if nearest is None:
        return [], []

    # 2. Find the contour containing the nearest shore pixel.
    contours = find_contours(mask.astype(float), 0.5)
    if not contours:
        return [], []
    hit = _find_containing_contour(contours, nearest)
    if hit is None:
        return [], []
    contour, anchor_idx = hit
    if len(contour) < 4:
        return [], []

    # 3. Walk along contour from anchor_idx in BOTH directions; choose
    # the one whose net displacement aligns with the walk-direction
    # bearing (commit_direction when supplied, else fall back to
    # heading_deg for legacy callers).
    walk_bearing = walk_direction_deg if walk_direction_deg is not None \
                   else heading_deg
    fy, fx = bearing_to_image_vec(walk_bearing)
    end_fwd, idxs_fwd = _walk_contour_by_distance(
        contour, anchor_idx, +1, walk_distance_px)
    end_bwd, idxs_bwd = _walk_contour_by_distance(
        contour, anchor_idx, -1, walk_distance_px)
    pt_fwd = contour[end_fwd]
    pt_bwd = contour[end_bwd]
    anchor_pt = contour[anchor_idx]
    dot_fwd = (pt_fwd[1] - anchor_pt[1]) * fx + \
              (pt_fwd[0] - anchor_pt[0]) * fy
    dot_bwd = (pt_bwd[1] - anchor_pt[1]) * fx + \
              (pt_bwd[0] - anchor_pt[0]) * fy
    chosen_idxs = idxs_fwd if dot_fwd >= dot_bwd else idxs_bwd

    shore_pts: list[tuple[int, int]] = []
    for i in chosen_idxs:
        cy, cx = contour[i]
        shore_pts.append((int(round(cy)), int(round(cx))))

    # 4. Offset shore points perpendicular to shore (toward water).
    # The inward direction = opposite of shore_bearing (since shore is
    # on the chosen side, inward goes back across the ship's side).
    inward_bearing = (shore_bearing + 180) % 360
    iny, inx = bearing_to_image_vec(inward_bearing)
    candidate_distances = [safe_distance_px, 20, 15, 10, 5]
    seen: set[int] = set()
    candidate_distances = [d for d in candidate_distances
                          if not (d in seen or seen.add(d))]
    path_pts: list[tuple[int, int]] = []
    for tried_distance in candidate_distances:
        path_pts = []
        for sp_y, sp_x in shore_pts:
            py = sp_y + tried_distance * iny
            px = sp_x + tried_distance * inx
            py_i = int(round(py)); px_i = int(round(px))
            if not (0 <= py_i < H and 0 <= px_i < W):
                continue
            if not mask[py_i, px_i]:
                continue
            path_pts.append((py_i, px_i))
        if path_pts:
            break

    return shore_pts, path_pts


# ── The picker ─────────────────────────────────────────────────────────


def pick_shore_waypoint(
    mask: np.ndarray,
    ship_xy: tuple[int, int],
    heading_deg: float,
    side: str = "port",
    lookahead_px: int = DEFAULT_LOOKAHEAD,
    safe_distance_px: int = DEFAULT_SAFE_DISTANCE,
) -> tuple[int, int] | None:
    """Pick a waypoint pixel using the shore-hug rule.

    side="port"      = bot's left side (heading - 90°)
    side="starboard" = bot's right side (heading + 90°)

    Returns (y, x) in image coords (matches M6f picker's return shape)
    or None if no shore visible on the chosen side.
    """
    sx, sy = ship_xy

    # 1. Project the future ship position lookahead_px ahead in
    #    heading direction.
    dy_fwd, dx_fwd = bearing_to_image_vec(heading_deg)
    future_x = sx + lookahead_px * dx_fwd
    future_y = sy + lookahead_px * dy_fwd

    # 2. Cast a ray from the future position toward the chosen shore.
    # BOW-RELATIVE nautical convention.
    shore_bearing = (heading_deg - 90) % 360 if side == "port" \
                   else (heading_deg + 90) % 360
    shore_pt = cast_ray_to_land(mask, (future_x, future_y), shore_bearing)
    if shore_pt is None:
        # No shore visible on chosen side — return future position itself
        # (open water; just keep heading forward).
        if not (0 <= int(future_y) < mask.shape[0]
                and 0 <= int(future_x) < mask.shape[1]):
            return None
        if not mask[int(future_y), int(future_x)]:
            return None    # future is on land — abort
        return (int(round(future_y)), int(round(future_x)))

    # 3. Step SAFE_DISTANCE_PX back from shore toward water.
    away_bearing = (shore_bearing + 180) % 360
    dy_away, dx_away = bearing_to_image_vec(away_bearing)
    spx, spy = shore_pt
    wp_x = spx + safe_distance_px * dx_away
    wp_y = spy + safe_distance_px * dy_away

    return (int(round(wp_y)), int(round(wp_x)))


# ── Rendering ──────────────────────────────────────────────────────────


def render_compare(rgb: np.ndarray, mask: np.ndarray,
                  ship_xy, heading_vec, m6f_wp, shore_wp,
                  shore_bearing, future_pos,
                  out_path: Path, title: str):
    fig, axes = plt.subplots(1, 2, figsize=(20, 6))
    for ax, label, wp in [
        (axes[0], "M6f centerline", m6f_wp),
        (axes[1], "shore-hug (new)", shore_wp),
    ]:
        ax.imshow(rgb, alpha=0.6)
        mo = np.zeros((*mask.shape, 4)); mo[mask] = [0, 0.4, 1.0, 0.20]
        ax.imshow(mo)
        if ship_xy is not None:
            sx, sy = ship_xy
            ax.scatter([sx], [sy], s=180, c="lime",
                       edgecolors="black", linewidths=1.4, zorder=5)
            if heading_vec is not None:
                hy, hx = heading_vec
                ax.arrow(sx, sy, hx * 25, hy * 25, head_width=8,
                         fc="lime", ec="black", lw=1.1, zorder=6,
                         length_includes_head=True)
        if wp is not None:
            wy, wx = wp
            ax.scatter([wx], [wy], s=180, c="orange",
                       edgecolors="black", linewidths=1.4,
                       marker="*", zorder=7)
            ax.annotate(f"wp ({wx},{wy})", (wx, wy), color="black",
                        fontsize=9, fontweight="bold",
                        xytext=(8, 6), textcoords="offset points",
                        bbox=dict(boxstyle="round,pad=0.2", fc="orange",
                                  ec="black", alpha=0.8))
        # Add shore-ray on the right panel.
        if label.startswith("shore") and future_pos is not None:
            fx, fy = future_pos
            ax.scatter([fx], [fy], s=60, c="white",
                       edgecolors="black", linewidths=0.9, zorder=6)
            # Ray from future position to wp's shore origin.
            if wp is not None:
                wy, wx = wp
                dy, dx = bearing_to_image_vec(shore_bearing)
                # Approximate shore endpoint by walking from future
                # toward shore_bearing.
                for step in range(1, 250):
                    py = int(round(fy + step * dy))
                    px = int(round(fx + step * dx))
                    if not (0 <= py < mask.shape[0]
                            and 0 <= px < mask.shape[1]):
                        break
                    if not mask[py, px]:
                        ax.plot([fx, px], [fy, py], c="red", lw=1.2,
                                alpha=0.6)
                        ax.scatter([px], [py], s=40, c="red",
                                   edgecolors="black", linewidths=0.8,
                                   zorder=5)
                        break
        ax.set_title(f"{label}", fontsize=11)
        ax.axis("off")
    fig.suptitle(title, fontsize=12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


# ── Demo runner ────────────────────────────────────────────────────────

TEST_DIR = ROOT / "data/test_frames/channel_perception"
DEMO_FRAMES = [
    ("t5_delta",     "v13_scenarios/tick_0005.png"),
    ("t100_clean",   "v13_scenarios/tick_0100.png"),
    ("t152_Nubia",   "v13_scenarios/tick_0152.png"),
    ("t300_bend",    "v13_scenarios/tick_0300.png"),
    ("t475_Y",       "v13_scenarios/tick_0475.png"),
    ("t500_deadend", "v13_scenarios/tick_0500.png"),
    ("t571_Bari",    "v13_scenarios/tick_0571_bari_village.png"),
    ("t750_Ubend",   "v13_scenarios/tick_0750_y_branch.png"),
    ("t786_lake",    "v13_scenarios/tick_0786_lake_at_end.png"),
    ("t83_dawn",     "hug_debug_specials/tick_0083.png"),
]


def _draw_panel(ax, rgb, mask, ship, heading_vec, heading_deg,
               m6f_tree, m6f_wp, shore_pts, path_pts, shore_wp,
               panel_title):
    """Render one panel showing M6f tree + shore-hug path side overlay.
    Combined view so user can compare in a single frame."""
    ax.imshow(rgb, alpha=0.6)
    mo = np.zeros((*mask.shape, 4)); mo[mask] = [0, 0.4, 1.0, 0.20]
    ax.imshow(mo)

    # M6f tree edges in red.
    for e in m6f_tree.edges:
        ax.plot(e.points[:, 1], e.points[:, 0], c="red", lw=1.2, alpha=0.6)
    # M6f tree nodes.
    for n in m6f_tree.nodes.values():
        c = ("magenta" if n.kind == "junction"
             else "yellow" if "edge" in n.kind else "cyan")
        ax.scatter([n.x], [n.y], s=60, c=c,
                   edgecolors="black", linewidths=0.7, zorder=4)

    # Shore polyline in pink (the bank pixels).
    if shore_pts:
        ys = [p[0] for p in shore_pts]; xs = [p[1] for p in shore_pts]
        ax.plot(xs, ys, c="hotpink", lw=2.0, alpha=0.85)
        ax.scatter(xs, ys, s=18, c="hotpink",
                   edgecolors="black", linewidths=0.4, zorder=4)
    # Shore-hug PATH (shore offset by safe distance) in lime green.
    if path_pts:
        ys = [p[0] for p in path_pts]; xs = [p[1] for p in path_pts]
        ax.plot(xs, ys, c="limegreen", lw=2.6, alpha=0.95)
        ax.scatter(xs, ys, s=22, c="limegreen",
                   edgecolors="black", linewidths=0.5, zorder=5)

    # Ship.
    if ship is not None:
        sx, sy = ship
        ax.scatter([sx], [sy], s=180, c="lime",
                   edgecolors="black", linewidths=1.4, zorder=6)
        if heading_vec is not None:
            hy, hx = heading_vec
            ax.arrow(sx, sy, hx * 22, hy * 22, head_width=7,
                     fc="lime", ec="black", lw=1.0, zorder=7,
                     length_includes_head=True)

    # M6f waypoint marker (red star).
    if m6f_wp is not None:
        wy, wx = m6f_wp
        ax.scatter([wx], [wy], s=240, c="red", edgecolors="black",
                   linewidths=1.4, marker="*", zorder=8)
    # Shore-hug waypoint marker (orange star).
    if shore_wp is not None:
        wy, wx = shore_wp
        ax.scatter([wx], [wy], s=240, c="orange", edgecolors="black",
                   linewidths=1.4, marker="*", zorder=8)

    ax.set_title(panel_title, fontsize=9)
    ax.axis("off")


def _main():
    out_dir = Path("/tmp/shore_picker")
    out_dir.mkdir(exist_ok=True)
    print(f"out: {out_dir}\n")

    # Single mega-image: 5 cols × 2 rows = 10 frames in one figure.
    cols = 5; rows = (len(DEMO_FRAMES) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 5, rows * 3.5))

    for idx, (label, src_rel) in enumerate(DEMO_FRAMES):
        ax = axes[idx // cols][idx % cols]
        rgb_raw = np.asarray(Image.open(TEST_DIR / src_rel).convert("RGB"))
        rgb, mask = channel_mask_from_rgb(rgb_raw)
        ship = _ship_xy(rgb)
        heading_vec = estimate_ship_heading(rgb)
        if ship is None or heading_vec is None:
            ax.imshow(rgb)
            ax.set_title(f"{label} — no ship/heading", fontsize=9)
            ax.axis("off")
            continue
        heading_deg = image_vec_to_bearing(*heading_vec)

        tree, _ = extract_tree(rgb_raw)
        m6f_wp, _, _ = m6f_pick_waypoint(tree, ship, heading_vec, "left",
                                         LOOKAHEAD_PX) \
                      if tree.nodes else (None, [], [])
        shore_pts, path_pts = extract_shore_path(
            mask, ship, heading_deg, "port")
        shore_wp = pick_shore_waypoint(mask, ship, heading_deg, "port")

        _draw_panel(ax, rgb, mask, ship, heading_vec, heading_deg,
                   tree, m6f_wp, shore_pts, path_pts, shore_wp,
                   f"{label}  hdg={heading_deg:.0f}°  "
                   f"shore={len(shore_pts)}px  path={len(path_pts)}px")
        print(f"  {label:<14} hdg={heading_deg:>5.0f}°  "
              f"m6f_wp={m6f_wp}  shore_wp={shore_wp}  "
              f"shore_pts={len(shore_pts)}  path_pts={len(path_pts)}")

    # Per-frame individual renders for legibility.
    for label, src_rel in DEMO_FRAMES:
        rgb_raw = np.asarray(Image.open(TEST_DIR / src_rel).convert("RGB"))
        rgb, mask = channel_mask_from_rgb(rgb_raw)
        ship = _ship_xy(rgb)
        heading_vec = estimate_ship_heading(rgb)
        if ship is None or heading_vec is None: continue
        heading_deg = image_vec_to_bearing(*heading_vec)
        tree, _ = extract_tree(rgb_raw)
        m6f_wp, _, _ = m6f_pick_waypoint(tree, ship, heading_vec, "left",
                                         LOOKAHEAD_PX) \
                      if tree.nodes else (None, [], [])
        shore_pts, path_pts = extract_shore_path(
            mask, ship, heading_deg, "port")
        shore_wp = pick_shore_waypoint(mask, ship, heading_deg, "port")
        fig_i, ax_i = plt.subplots(figsize=(13, 6.5))
        _draw_panel(ax_i, rgb, mask, ship, heading_vec, heading_deg,
                   tree, m6f_wp, shore_pts, path_pts, shore_wp,
                   f"{label}  hdg={heading_deg:.0f}°  "
                   f"shore_pts={len(shore_pts)}  path_pts={len(path_pts)}\n"
                   f"red star = M6f waypoint  |  orange star = shore-hug waypoint  |  "
                   f"pink = shore line  |  lime = bot's path (shore offset inward)")
        fig_i.tight_layout()
        fig_i.savefig(out_dir / f"{label}.png", dpi=120, bbox_inches="tight")
        plt.close(fig_i)

    # Legend at the figure level.
    legend_handles = [
        plt.Line2D([], [], color="lime", marker="o", linestyle="None",
                   markersize=10, label="ship + heading arrow"),
        plt.Line2D([], [], color="red", lw=2, label="M6f centerline tree"),
        plt.Line2D([], [], color="yellow", marker="o", linestyle="None",
                   markersize=8, label="M6f frame-edge anchor"),
        plt.Line2D([], [], color="cyan", marker="o", linestyle="None",
                   markersize=8, label="M6f dead-end anchor"),
        plt.Line2D([], [], color="magenta", marker="o", linestyle="None",
                   markersize=8, label="M6f junction"),
        plt.Line2D([], [], color="red", marker="*", linestyle="None",
                   markersize=14, label="M6f waypoint"),
        plt.Line2D([], [], color="hotpink", lw=2, label="shore polyline (bank pixels)"),
        plt.Line2D([], [], color="limegreen", lw=3,
                   label="shore-hug PATH (bot follows this)"),
        plt.Line2D([], [], color="orange", marker="*", linestyle="None",
                   markersize=14, label="shore-hug waypoint"),
    ]
    fig.legend(handles=legend_handles, loc="lower center",
               ncol=3, bbox_to_anchor=(0.5, -0.02), fontsize=10,
               frameon=True)
    fig.suptitle("M6f centerline (red tree + red star) vs shore-hug "
                 "(pink shore + lime path + orange star)  —  hug=left",
                 fontsize=13, fontweight="bold", y=0.99)
    fig.tight_layout(rect=[0, 0.03, 1, 0.97])
    fig.savefig(out_dir / "all_frames_compare.png",
                dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"\nMega-comparison: {out_dir}/all_frames_compare.png")


if __name__ == "__main__":
    _main()
