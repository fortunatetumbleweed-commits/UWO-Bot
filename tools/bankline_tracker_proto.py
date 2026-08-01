"""Prototype: directed-vector bankline tracker (design of record in
docs/bankline_continuity_design.md).

Re-trace the hug-side bank fresh each frame, but keep the line as a
directed vector (head->tail).  Carry the traversal direction by
registering the TAIL (frame-shift-bounded) against the previous line
shifted by frame_shift; the HEAD is free to grow.  No anchor policy.

Run:  python -m tools.bankline_tracker_proto        # tests all scenarios
"""
from __future__ import annotations
import math
from typing import Optional

import numpy as np

import cv2

from tools.bank_tracer import (
    trace_bank_to_edge, _find_ship_contour, _project_ship,
    _walk_contour_with_offset,
)


def _bearing(sy, sx, py, px) -> float:
    return (math.degrees(math.atan2(px - sx, -(py - sy))) + 360.0) % 360.0


def _full_loop(mask, ship_yx, approach, hug):
    """Walk the WHOLE ship-water-CC contour as a CLOSED loop (the frame
    edges act as walls — cv2's contour already follows them), offset into
    water, starting at the ship's projection.  A closed directed loop has
    a stable topology: a dead-end reveal just adds a lobe, the loop never
    flips."""
    contour = _find_ship_contour(mask, ship_yx)
    if contour is None or len(contour) < 4:
        return None
    idx0 = _project_ship(contour, mask, ship_yx, approach, hug)
    N = len(contour)
    rad = math.radians(approach)
    fuy, fux = -math.cos(rad), math.sin(rad)
    LOOK = min(5, N // 4)
    dyp = float(contour[(idx0 + LOOK) % N, 0] - contour[idx0, 0])
    dxp = float(contour[(idx0 + LOOK) % N, 1] - contour[idx0, 1])
    step = 1 if (dyp * fuy + dxp * fux) > 0 else -1
    return _walk_contour_with_offset(
        contour, idx0, None, step, mask, max_length_px=N * 2,
        offset_px=10, stop_at_frame_edge=False)


def _simplify(poly, eps):
    """Douglas-Peucker: straighten sub-`eps` zigzags into a topography
    polyline of a few straight sections.  Returns list of (y, x)."""
    pts = np.array([[p[1], p[0]] for p in poly], np.int32).reshape(-1, 1, 2)
    ap = cv2.approxPolyDP(pts, eps, False).reshape(-1, 2)
    return [(int(y), int(x)) for x, y in ap]


def _arclen_point(poly, target):
    """Point at `target` arc-length along the polyline (skips sub-target
    wiggles — notches / fake-water nicks — by using a longer baseline)."""
    acc = 0.0
    for i in range(1, len(poly)):
        acc += math.hypot(poly[i][0] - poly[i - 1][0],
                          poly[i][1] - poly[i - 1][1])
        if acc >= target:
            return poly[i]
    return poly[-1]


def _topo_change(new_poly, prev_poly, shift):
    """Mean nearest-point distance from the new polyline BODY to the
    shifted previous polyline — how much the shore's shape changed
    between frames beyond the pure motion shift.  Small = same shore;
    large = a feature appeared/vanished (dead-end reveal OR noise)."""
    if not prev_poly or shift is None:
        return None
    dy, dx, _ = shift
    ps = np.array([(p[0] + dy, p[1] + dx) for p in prev_poly], float)
    # exclude the leading third of each (the growing head) — compare bodies
    body = np.array(new_poly[:max(2, 2 * len(new_poly) // 3)], float)
    if len(body) < 2 or len(ps) < 2:
        return None
    d = np.sqrt(((body[:, None, :] - ps[None, :, :]) ** 2).sum(-1)).min(1)
    return float(d.mean())


class BanklineTracker:
    """Self-carrying bankline follower.

    Each tick: project the ship onto the hug-side bank ⟂ to the carried
    direction, trace forward -> navigable polyline (ship -> head).  The
    HEAD is free to jump; steering uses the REFLEX POINT — the point
    ~REFLEX_PX along the polyline (head of the nearest section) — which
    stays on the line near the ship, so it moves only by ~frame_shift and
    never jumps.  commit = ship->reflex; that direction is carried to the
    next tick.  Seeded once (CLI).  No anchor / sticky / migrate / hold.
    """

    def __init__(self, hug_side: str = "port", seed_bearing: float = 0.0,
                 section_len: float = 30.0, dp_eps: float = 15.0,
                 loop: bool = False):
        self.hug = hug_side
        self.commit = seed_bearing           # carried desired direction
        self.section_len = section_len       # slope baseline on simplified line
        self.dp_eps = dp_eps                 # zigzag-straightening tolerance
        self.loop = loop                     # closed-loop vs open trace
        self.prev_reflex: Optional[tuple] = None
        self.prev_poly: Optional[list] = None

    def update(self, mask, frame_shift, ship_yx=None):
        H, W = mask.shape
        if ship_yx is None:
            ship_yx = (H // 2, W // 2)
        sy, sx = ship_yx
        # 1. Trace: closed loop (frame edges as walls) or open-to-edge.
        if self.loop:
            poly = _full_loop(mask, ship_yx, self.commit, self.hug)
        else:
            poly = trace_bank_to_edge(mask, ship_yx, self.commit,
                                      hug_side=self.hug, offset_px=10)
        if poly is None or len(poly) < 2:
            return self.commit, None
        # 2. Simplify the traceline into a TOPOGRAPHY polyline (straighten
        #    minute zigzags / notches into a few straight sections).
        simp = _simplify(poly, self.dp_eps)
        nsec = len(simp) - 1
        # 3. Reflex = point ~section_len along the SIMPLIFIED line (so the
        #    slope baseline sits on straight topography, not inside a notch).
        reflex = _arclen_point(simp, self.section_len)
        # 4. Diagnostics: reflex jump + topography change vs shifted-prev.
        jump = None
        if self.prev_reflex is not None and frame_shift is not None:
            dy, dx, _ = frame_shift
            ps = (self.prev_reflex[0] + dy, self.prev_reflex[1] + dx)
            jump = math.hypot(reflex[0] - ps[0], reflex[1] - ps[1])
        topo = _topo_change(poly, self.prev_poly, frame_shift)
        # 5. Commit = direction of the FIRST topography section
        #    (simplified-start -> reflex) — no zigzag/notch to drift on.
        commit = _bearing(simp[0][0], simp[0][1], reflex[0], reflex[1])
        self.commit = commit
        self.prev_reflex = reflex
        self.prev_poly = poly
        return commit, {'reflex': reflex, 'head': poly[-1], 'poly': poly,
                        'simp': simp, 'nsec': nsec, 'jump': jump, 'topo': topo}


def _build_loop_polyline(contour, ideal=40.0, min_len=10.0, hard_floor=5.0):
    """Turn the raw closed contour into a navigable POLYLINE LOOP whose
    sections are ideally `ideal` px and never shorter than `min_len`
    (hard floor `hard_floor`).  Irons out the sub-section notches / fake-
    water nicks around ports & villages that pollute the raw traceline.

    Returns (resampled_loop (M,2) yx, section_lengths).
    """
    # 1. DP-simplify (closed) — removes zigzag < ~ideal/5 deviation.
    pts = np.array([[p[1], p[0]] for p in contour], np.int32).reshape(-1, 1, 2)
    ap = cv2.approxPolyDP(pts, ideal / 5.0, True).reshape(-1, 2)
    verts = [(int(y), int(x)) for x, y in ap]

    def seglen(i):
        a, b = verts[i], verts[(i + 1) % len(verts)]
        return math.hypot(a[0] - b[0], a[1] - b[1])

    # 2. Iron out sections shorter than min_len (merge shortest first),
    #    but never collapse below the hard floor of vertices.
    while len(verts) > 3:
        segs = [seglen(i) for i in range(len(verts))]
        m = min(range(len(segs)), key=lambda i: segs[i])
        if segs[m] >= min_len:
            break
        verts.pop((m + 1) % len(verts))     # drop the short section's end
    seclens = [seglen(i) for i in range(len(verts))]
    # 3. Resample to a dense loop (2 px) so the foothold/arc-length walk
    #    runs on the smoothed shape, not the raw contour.
    out = []
    N = len(verts)
    for i in range(N):
        a, b = verts[i], verts[(i + 1) % N]
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(d / 2.0))
        for t in range(n):
            f = t / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return np.array(out, float), seclens


def _nearest_idx(contour, yx):
    d2 = (contour[:, 0] - yx[0]) ** 2 + (contour[:, 1] - yx[1]) ** 2
    return int(np.argmin(d2))


def _walk_arclen(contour, i0, step, target):
    """Index `target` px of arc-length from i0 along the loop in `step`."""
    N = len(contour)
    acc = 0.0
    i = i0
    while acc < target:
        j = (i + step) % N
        acc += math.hypot(float(contour[j, 0] - contour[i, 0]),
                          float(contour[j, 1] - contour[i, 1]))
        i = j
        if i == i0:
            break
    return i


class LoopTracker:
    """Directed-loop follower (the final design).

    Close the ship's water region into a directed loop (real bank +
    frame-edge walls = land).  Carry ONE quantity — the foothold, the
    ship's point on the loop — by shift→snap each frame.  The hug side
    fixes the traversal direction (land on the left) at cold-start; reflex
    = +40 px ahead of the foothold along the loop, tactical = +80 px.
    Commit = ship→reflex.  No CLI seed; direction = (foothold, hug side).
    """

    def __init__(self, hug_side: str = "port", simplify: bool = False,
                 ideal: float = 40.0, min_len: float = 10.0, **_ignored):
        self.hug = hug_side
        self.simplify = simplify
        self.ideal = ideal
        self.min_len = min_len
        self.prev_foothold: Optional[tuple] = None
        self.step: Optional[int] = None
        self.commit = 0.0
        self.last_seclens: list = []

    def _land_on_left(self, contour, i, step, mask):
        N = len(contour)
        j = (i + step) % N
        k = (i - step) % N
        ty = float(contour[j, 0] - contour[k, 0])
        tx = float(contour[j, 1] - contour[k, 1])
        mag = math.hypot(ty, tx) or 1.0
        ty /= mag; tx /= mag
        ly, lx = -tx, ty                       # left of travel (image coords)
        H, W = mask.shape
        py = int(round(contour[i, 0] + 4 * ly))
        px = int(round(contour[i, 1] + 4 * lx))
        if not (0 <= py < H and 0 <= px < W):
            return True                        # off-frame ⇒ wall = land
        return not mask[py, px]

    def update(self, mask, frame_shift, ship_yx=None):
        H, W = mask.shape
        if ship_yx is None:
            ship_yx = (H // 2, W // 2)
        sy, sx = ship_yx
        contour = _find_ship_contour(mask, ship_yx)
        if contour is None or len(contour) < 4:
            return self.commit, None
        if self.simplify:
            loop, self.last_seclens = _build_loop_polyline(
                contour, ideal=self.ideal, min_len=self.min_len)
            if loop is not None and len(loop) >= 4:
                contour = loop
        # Foothold = the loop point ABEAM the ship (nearest to the ship,
        # which is always the minimap centre).  It must track the SHIP, not
        # a world-fixed point: on a ship-centred minimap the ship never
        # moves, so a world-fixed foothold (prev+frame_shift) scrolls away
        # from centre, drags the reflex backward and rotates the commit.
        # But pure nearest-to-ship JUMPS across a pinch (strait): the two
        # banks are ~equidistant and the nearest flips bank tick-to-tick.
        # So constrain the abeam pick to a continuity WINDOW around where the
        # foothold was last tick (prev + frame_shift) — it can only slide,
        # never leap to the far bank.
        foot = _nearest_idx(contour, ship_yx)
        if self.prev_foothold is not None:
            dy, dx, _ = frame_shift if frame_shift else (0, 0, 0)
            cy, cx = self.prev_foothold[0] + dy, self.prev_foothold[1] + dx
            near = np.where((contour[:, 0] - cy) ** 2
                            + (contour[:, 1] - cx) ** 2 < 30.0 ** 2)[0]
            if len(near) >= 2:
                d2s = (contour[near, 0] - sy) ** 2 + (contour[near, 1] - sx) ** 2
                foot = int(near[int(np.argmin(d2s))])

        if self.step is None:
            # Cold start: pick the traversal direction with land on the hug
            # side (port = land on the left).  In a narrow body the near
            # shore resolves direction; a dead-centre WIDE body (delta / lake)
            # is genuinely ambiguous — live, launch placement (departing while
            # hugging the correct shore) resolves it.
            s = 1 if self._land_on_left(contour, foot, 1, mask) else -1
            self.step = s if self.hug == "port" else -s
            reg = "cold"
        else:
            # Carry the forward direction by CONTINUITY (robust to the
            # contour re-indexing each tick): pick the step whose reflex
            # bearing stays closest to the previous commit.
            rp = contour[_walk_arclen(contour, foot, 1, 40.0)]
            rm = contour[_walk_arclen(contour, foot, -1, 40.0)]
            bp = _bearing(sy, sx, rp[0], rp[1])
            bm = _bearing(sy, sx, rm[0], rm[1])
            dp = abs(((bp - self.commit + 540) % 360) - 180)
            dm = abs(((bm - self.commit + 540) % 360) - 180)
            self.step = 1 if dp <= dm else -1
            reg = "abeam"
        reflex_i = _walk_arclen(contour, foot, self.step, 40.0)
        tac_i = _walk_arclen(contour, foot, self.step, 80.0)
        rf = (int(contour[reflex_i, 0]), int(contour[reflex_i, 1]))
        commit = _bearing(sy, sx, *rf)
        self.commit = commit
        self.prev_foothold = (int(contour[foot, 0]), int(contour[foot, 1]))
        return commit, {'foothold': self.prev_foothold, 'reflex': rf,
                        'tactical': (int(contour[tac_i, 0]), int(contour[tac_i, 1])),
                        'contour': contour, 'step': self.step, 'reg': reg}


# ---------------------------------------------------------------- harness

def _run_scenarios():
    import sys
    from pathlib import Path
    import json
    from brain.ai_nav.pipeline import AiNavPipeline, PipelineConfig
    from brain.ai_nav.vision_input import FileVisionSource
    from brain.ai_nav.layers.hug_path_planner import HugPathPlanner
    from brain.ai_nav.layers.tactical import NoOpTactical
    from brain.ai_nav.layers.heading import ShipPartsHeading
    from brain.ai_nav.state import NavState

    from tests.tactical_scenarios import (
        cairo_start, nubia_bend, y_fork, y_tip_return, lake,
        nile_exit_left, med_hug_west, nile_meander, strait_t1525,
        channel_stuck_t1444,
    )
    SCEN = [cairo_start.SCENARIO, nubia_bend.SCENARIO, y_fork.SCENARIO,
            y_tip_return.SCENARIO, lake.SCENARIO, nile_exit_left.SCENARIO,
            med_hug_west.SCENARIO, nile_meander.SCENARIO,
            strait_t1525.SCENARIO, channel_stuck_t1444.SCENARIO]
    root = Path("data/sessions")

    def spread(cs):
        a = sorted(c % 360 for c in cs)
        if len(a) < 2:
            return 0.0
        gaps = [a[i + 1] - a[i] for i in range(len(a) - 1)] + [a[0] + 360 - a[-1]]
        return 360 - max(gaps)

    def word(d):
        return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][int(((d + 22.5) % 360) // 45)]

    def run(sc, simplify):
        d = root / sc.session
        cfg = PipelineConfig(heading=ShipPartsHeading(),
                             planner=HugPathPlanner(), tactical=NoOpTactical())
        pipe = AiNavPipeline(source=FileVisionSource(d), config=cfg)
        L = {json.loads(l)['tick']: json.loads(l) for l in open(d / "trace.jsonl")}
        r0 = L[sc.start_tick]
        st = NavState(tick=sc.start_tick - 1, lat=r0['lat'], lon=r0['lon'])
        trk = LoopTracker(hug_side=sc.hug_side, simplify=simplify,
                          ideal=40.0, min_len=10.0)
        commits, minsec, nsec = [], [], []
        for _ in range(sc.start_tick, sc.end_tick + 1):
            st = pipe.tick(st)
            if st.water_mask is None:
                continue
            c, info = trk.update(st.water_mask, st.frame_shift_px)
            commits.append(c)
            if simplify and trk.last_seclens:
                minsec.append(min(trk.last_seclens))
                nsec.append(len(trk.last_seclens))
        rot = sum(1 for i in range(1, len(commits))
                  if abs(((commits[i] - commits[i - 1] + 540) % 360) - 180) > 60)
        mins = min(minsec) if minsec else 0
        avgn = (sum(nsec) / len(nsec)) if nsec else 0
        return spread(commits), rot, mins, avgn

    print(f"{'scenario':22} {'RAW spr/flip':>13}   {'SIMPLIFIED spr/flip':>19}"
          f"  {'min_sec':>7} {'~nsec':>5}")
    for sc in SCEN:
        if not (root / sc.session).exists():
            print(f"{sc.name:22} (missing)")
            continue
        rsp, rrot, _, _ = run(sc, False)
        ssp, srot, mins, avgn = run(sc, True)
        tag = " <-- xfail" if sc.xfail else ""
        print(f"{sc.name:22} {rsp:6.0f}/{rrot:<5d}   {ssp:6.0f}/{srot:<11d}"
              f"  {mins:7.0f} {avgn:5.1f}{tag}")


if __name__ == "__main__":
    _run_scenarios()
