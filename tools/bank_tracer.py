"""Bank-tracing prototype for tactical navigation.

Semantic: follow the ship's left bank forward from its current
position.  Output a polyline that traces the shore in the approach
direction.  This IS the tactical route — no opening enumeration, no
turning-point vs frame-edge distinction, no arrival test.

Rationale (from voyage 2026-07-24T22-01-22 t395 diagnosis):
  Body-frame angle bucketing ("LEFT/STRAIGHT/RIGHT/REVERSE") fails
  at complex junctions because "left" is a topological property of
  the WATER CHANNEL, not a body-frame angle from the momentary bow.
  Literal bank tracing solves this cleanly and handles both river
  navigation and open-water routing with the same code.

Algorithm:
  1. Extract the outer contour of the ship's water CC via
     cv2.findContours (returns pixels in CCW order for outer
     boundaries — interior is on the LEFT of each tangent).
  2. Project ship onto the contour — nearest boundary pixel.
  3. Determine walking direction:
       - CW traversal (index-reversed) keeps water on RIGHT →
         we're walking with the bank on our LEFT (hugging left).
       - CCW traversal keeps water on LEFT →
         hugging right bank.
     Pick per `hug_side` argument.
  4. Walk the contour for `max_length_px` pixels.  Emit points
     offset INWARD by `offset_px` (perpendicular to local tangent,
     into water), so the ship follows the trace at safe distance
     from the shore rather than beaching on it.

Returns the traced polyline in mask pixel coords.
"""
from __future__ import annotations
from typing import Optional
import math

import numpy as np


def _find_ship_contour(mask: np.ndarray, ship_yx: tuple[int, int]
                        ) -> Optional[np.ndarray]:
    """Return the outer contour enclosing the ship's water CC.

    Contour shape: (N, 2) array of (y, x) pixel points in CCW order.
    """
    try:
        import cv2
    except ImportError:
        return None
    m = mask.astype(np.uint8)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    # cv2 contours are (N, 1, 2) with (x, y) coords.  Convert to (N, 2)
    # in (y, x) order for consistency with numpy/mask indexing.
    sy, sx = ship_yx
    best = None
    best_dist = float("inf")
    for c in contours:
        pts = c.reshape(-1, 2)   # (N, 2) with (x, y)
        ys = pts[:, 1]; xs = pts[:, 0]
        d2 = (ys - sy) ** 2 + (xs - sx) ** 2
        d = float(d2.min())
        if d < best_dist:
            best_dist = d
            best = np.stack([ys, xs], axis=1)   # (N, 2) with (y, x)
    return best


def _project_ship(contour: np.ndarray, mask: np.ndarray,
                  ship_yx: tuple[int, int],
                  approach_deg: float, hug_side: str) -> int:
    """Return the contour index of the true port perpendicular ray-cast
    hit — the first land pixel encountered when stepping from ship in
    the port direction.

    The start point MUST sit exactly on the port perpendicular from
    ship (along_fwd = 0) for the walker's tangent at that point to
    have a clean forward dot product; otherwise both walker step
    directions score near-zero and the pick is "lucky" (see session
    ai_nav_2026-07-27T20-20-44 t805/t807: prior scored-approximation
    picked contour points 34 px away from the true perpendicular hit,
    causing the walker to flip CW/CCW between adjacent ticks and the
    reflex WP to swing 180° between top-LEFT and bot-RIGHT).

    Ray-casting is deterministic: sub-pixel step along the port
    unit vector, find first mask=False pixel, snap to nearest contour
    index.  No scoring, no weight tuning.

    Falls back to scored approximation (weight-10) only when the ray
    exits the frame without hitting land — rare in bounded water bodies.
    """
    sy, sx = ship_yx
    H, W = mask.shape
    rad = math.radians(approach_deg)
    # Compass → image coords:
    #   forward = (-cos θ, sin θ)
    #   port    = 90° CCW on compass = (-sin θ, -cos θ)
    port_uy = -math.sin(rad)
    port_ux = -math.cos(rad)
    if hug_side != "port":
        port_uy = -port_uy; port_ux = -port_ux
    # Ray-cast: step along port direction from ship, find first land pixel.
    max_range = max(H, W)
    hit = None
    for step in range(1, max_range):
        py = int(round(sy + port_uy * step))
        px = int(round(sx + port_ux * step))
        if not (0 <= py < H and 0 <= px < W):
            break
        if not mask[py, px]:
            # Last water pixel before land = the bank hit
            hit = (py - int(round(port_uy)), px - int(round(port_ux)))
            break
    if hit is not None:
        # Snap ray hit to nearest contour index.
        d2 = ((contour[:, 0] - hit[0]) ** 2
              + (contour[:, 1] - hit[1]) ** 2)
        return int(np.argmin(d2))
    # Fallback (rare): ray exits frame without hitting land.
    # Use scored approximation.
    fwd_uy = -math.cos(rad); fwd_ux = math.sin(rad)
    dy = contour[:, 0] - sy
    dx = contour[:, 1] - sx
    along_offset = np.abs(dy * fwd_uy + dx * fwd_ux)
    port_dist    = dy * port_uy + dx * port_ux
    score = along_offset * 10.0 + port_dist
    score = np.where(port_dist > 0, score, score + 10000.0)
    return int(np.argmin(score))


def _walk_contour_with_offset(contour: np.ndarray, idx_start: int,
                              idx_end: Optional[int], step: int,
                              mask: np.ndarray,
                              max_length_px: int, offset_px: int,
                              stop_at_frame_edge: bool = False,
                              min_walked_before_edge_stop: float = 15.0,
                              skip_edges: Optional[set] = None
                              ) -> list[tuple[int, int]]:
    """Walk `contour` from idx_start in +step or -step direction, emit
    inward-offset polyline points.  Termination conditions:
      - walked ≥ max_length_px
      - reached idx_end (if non-None) — includes idx_end
      - stop_at_frame_edge and pixel is near frame boundary
      - loop back to idx_start

    Normal-into-water is data-driven (sample mask 3 px along the
    perpendicular; flip if not water).  This handles both CCW and CW
    walk directions uniformly.
    """
    H, W = mask.shape
    FRAME_MARGIN_PX = 2
    N = len(contour)
    polyline: list[tuple[int, int]] = []
    walked = 0.0
    i = idx_start
    prev_pt = None
    while walked < max_length_px:
        j_next = (i + step) % N
        j_prev = (i - step) % N
        ty = float(contour[j_next, 0] - contour[j_prev, 0])
        tx = float(contour[j_next, 1] - contour[j_prev, 1])
        tmag = math.hypot(ty, tx)
        if tmag < 1e-6:
            i = j_next
            if i == idx_start or i == idx_end:
                break
            continue
        ty /= tmag; tx /= tmag
        ny, nx = tx, -ty   # rotate 90° CW; data-driven check below
        cy = int(round(contour[i, 0] + 3 * ny))
        cx = int(round(contour[i, 1] + 3 * nx))
        if not (0 <= cy < H and 0 <= cx < W and mask[cy, cx]):
            ny, nx = -ny, -nx
        oy = int(round(contour[i, 0] + offset_px * ny))
        ox = int(round(contour[i, 1] + offset_px * nx))
        oy = max(0, min(H - 1, oy))
        ox = max(0, min(W - 1, ox))
        if not mask[oy, ox]:
            oy, ox = int(contour[i, 0]), int(contour[i, 1])
        pt = (oy, ox)
        if pt != prev_pt:
            polyline.append(pt)
            if prev_pt is not None:
                walked += math.hypot(pt[0] - prev_pt[0], pt[1] - prev_pt[1])
            prev_pt = pt
        i = j_next
        if i == idx_start:
            break
        if idx_end is not None and i == idx_end:
            break
        if stop_at_frame_edge and walked > min_walked_before_edge_stop:
            y = int(contour[i, 0]); x = int(contour[i, 1])
            # Check which edge (if any) this point is on.
            hit_edge = None
            if y <= FRAME_MARGIN_PX: hit_edge = "top"
            elif y >= H - 1 - FRAME_MARGIN_PX: hit_edge = "bot"
            elif x <= FRAME_MARGIN_PX: hit_edge = "left"
            elif x >= W - 1 - FRAME_MARGIN_PX: hit_edge = "right"
            if hit_edge is not None and (skip_edges is None
                                          or hit_edge not in skip_edges):
                break
    return polyline


def _nearest_contour_idx(contour: np.ndarray, yx: tuple[int, int]) -> int:
    """Return index of contour point nearest to yx (pixel coords)."""
    y, x = yx
    d2 = (contour[:, 0] - y) ** 2 + (contour[:, 1] - x) ** 2
    return int(np.argmin(d2))


def trace_bank_to_edge(
    mask: np.ndarray,
    ship_yx: tuple[int, int],
    approach_deg: float,
    hug_side: str = "port",
    offset_px: int = 10,
    start_yx: Optional[tuple[int, int]] = None,
) -> Optional[list[tuple[int, int]]]:
    """Walk the hug_side bank from ship's port/starboard projection
    until the first frame-edge exit reached (or a full loop-close).

    Walker direction is FIXED by hug_side (no trial-walk, no cliff
    sensitivity):
      hug_side="port"      → step=-1 (CW along CCW contour)
      hug_side="starboard" → step=+1 (CCW)

    approach_deg is used ONLY for the ship's contour projection
    (to disambiguate which side of the ship the port bank is on)
    when start_yx is not provided.

    start_yx (optional): pixel to use as the trace start.  Nearest
    contour point to this pixel becomes the walk start.  When
    provided, approach_deg is unused.  This lets a persistent
    world-fixed start point carry over across ticks (immune to
    cliff-jumps in the approach-direction-based projection).

    Returns polyline of (y, x) inward-offset points ending at a
    frame edge, or None on failure.
    """
    H, W = mask.shape
    contour = _find_ship_contour(mask, ship_yx)
    if contour is None or len(contour) < 4:
        return None
    # Determine which frame edge(s) the start pixel is on or off-frame
    # against.  The walker will NOT stop at these edges — the trace
    # started there, so terminating there produces a bogus zero-length
    # trace along the same edge.  Only stops on DIFFERENT edges (which
    # represent actual navigable exits toward the desired direction).
    #
    # Off-frame starts (row < 0 etc.) are clamped to the nearest
    # in-frame position — the world-fixed pixel is still world-consistent,
    # just outside the visible frame, and the underlying shore continues
    # off-frame.  We treat off-frame as an implicit "start edge" too.
    FRAME_MARGIN = 3  # a bit more generous than walker's FRAME_MARGIN_PX=2
    skip_edges: set = set()
    if start_yx is not None:
        sy, sx = start_yx
        if sy <= FRAME_MARGIN: skip_edges.add("top")
        if sy >= H - 1 - FRAME_MARGIN: skip_edges.add("bot")
        if sx <= FRAME_MARGIN: skip_edges.add("left")
        if sx >= W - 1 - FRAME_MARGIN: skip_edges.add("right")
        # Clamp off-frame positions to in-frame for _nearest_contour_idx.
        sy_c = max(0, min(H - 1, sy))
        sx_c = max(0, min(W - 1, sx))
        idx0 = _nearest_contour_idx(contour, (sy_c, sx_c))
    else:
        idx0 = _project_ship(contour, mask, ship_yx, approach_deg, hug_side)
        # Also check where projection landed and skip that edge.
        py, px = int(contour[idx0, 0]), int(contour[idx0, 1])
        if py <= FRAME_MARGIN: skip_edges.add("top")
        if py >= H - 1 - FRAME_MARGIN: skip_edges.add("bot")
        if px <= FRAME_MARGIN: skip_edges.add("left")
        if px >= W - 1 - FRAME_MARGIN: skip_edges.add("right")

    N = len(contour)
    # Walker step direction is data-driven: pick whichever of ±1
    # advances the contour pixel in the ship's FORWARD direction.
    # This guarantees the trace heads downstream (with the ship),
    # not backward.  Look 5 pixels ahead to smooth over local noise.
    rad = math.radians(approach_deg)
    fwd_uy = -math.cos(rad)
    fwd_ux =  math.sin(rad)
    LOOK = min(5, N // 4)
    dy_plus = float(contour[(idx0 + LOOK) % N, 0] - contour[idx0, 0])
    dx_plus = float(contour[(idx0 + LOOK) % N, 1] - contour[idx0, 1])
    fwd_dot_plus = dy_plus * fwd_uy + dx_plus * fwd_ux
    step = 1 if fwd_dot_plus > 0 else -1
    polyline = _walk_contour_with_offset(
        contour, idx0, None, step, mask,
        max_length_px=N * 2, offset_px=offset_px,
        stop_at_frame_edge=True,
        min_walked_before_edge_stop=15.0,
        skip_edges=skip_edges if skip_edges else None,
    )
    if len(polyline) < 2:
        return None
    return polyline
