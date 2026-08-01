"""L4 tactical — single hugging-side bankline follower.

Select with `--tactical loop`.  A SEPARATE layer that does NOT touch
`LookaheadTactical`.

The navigation substrate is ONE hugging-side bankline, not the whole loop
(see `docs/loop_navigation_design.md`, "single hugging-side bankline"):

  1. LOOP = the ship's raw water connected-component contour — full
     coverage of the water body the ship is on (no erosion to distort or
     split it).
  2. DESIRED DIRECTION = the ship's HEADING (perception, not a compass
     seed); falls back to the previous commit when heading is unavailable.
  3. FOOTHOLD = a forward-port ARC SCAN: sweep from abeam to straight ahead
     on the hugging side, raycast each direction, take the NEAREST land hit.
     Reacts to land on the bow, not just directly abeam; excludes "behind".
  4. NAV LINE = trace the bank forward from the foothold to the frame-edge
     exit / dead-end, offsetting each point inward by `min(hug, DT)` (the
     hug distance without crossing the centerline in a narrow channel),
     then skip short dead-end pockets + light-DP smooth.
  5. Reflex = 40 px along the nav line, tactical = 80 px; commit = ship→reflex.

Validated across the tactical scenarios (river bend, fork, dead-end,
winding, open coast, strait, islands); prototype `tools/viz_fullcov.py`.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import cv2
import numpy as np

from brain.ai_nav.state import NavState, CommitDirection
from brain.ai_nav.vision_input import VisionFrame

log = logging.getLogger(__name__)

REFLEX_PX = 40.0       # nav-line reflex point (immediate steering target)
TACTICAL_PX = 80.0     # nav-line lookahead point
HUG_PX = 14.0          # desired hug distance (capped per-point by DT)
PX_PER_DEG = 100.0     # sim canvas + live measured ~100 px/°


def _bearing(sy, sx, py, px) -> float:
    return (math.degrees(math.atan2(px - sx, -(py - sy))) + 360.0) % 360.0


def _strip_overlays(mask, rgb):
    """Restore bright-YELLOW / solid-WHITE UI-overlay pixels (city anchors,
    "???"/name text, diamond/star markers) that got segmented as LAND back
    to WATER — but only where they abut water (the fake notches/protrusions
    at the water edge), so real coastline and cloud land are untouched.
    These overlays otherwise make a city protrude into the hugging path."""
    R = rgb[:, :, 0].astype(int); G = rgb[:, :, 1].astype(int); B = rgb[:, :, 2].astype(int)
    yellow = (R > 150) & (G > 130) & (B < 130) & ((R - B) > 55) & ((G - B) > 40)
    white = (R > 205) & (G > 205) & (B > 205) & (np.abs(R - G) < 22) & (np.abs(G - B) < 22)
    land = ~mask
    water_adj = cv2.dilate(mask.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
    fix = (yellow | white) & land & water_adj
    if not fix.any():
        return mask
    out = mask.copy(); out[fix] = True
    return out


def _raw_cc(m, sr, sc):
    """Contour (y,x) of the ship's raw water connected component."""
    n, lbl = cv2.connectedComponents(m.astype(np.uint8))
    cid = lbl[sr, sc]
    if cid == 0:                                   # ship on a non-water pixel
        yy, xx = np.where(m)
        if len(yy) == 0:
            return None
        i = int(np.argmin((yy - sr) ** 2 + (xx - sc) ** 2))
        cid = lbl[yy[i], xx[i]]
    cc = (lbl == cid).astype(np.uint8)
    cs, _ = cv2.findContours(cc, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea).reshape(-1, 2)
    return np.stack([c[:, 1], c[:, 0]], 1)         # -> (y, x)


def _nearest_idx(ct, yx) -> int:
    return int(np.argmin((ct[:, 0] - yx[0]) ** 2 + (ct[:, 1] - yx[1]) ** 2))


def _walk_arclen(ct, i0, step, target) -> int:
    N = len(ct); acc = 0.0; i = i0
    while acc < target:
        j = (i + step) % N
        acc += math.hypot(float(ct[j, 0] - ct[i, 0]), float(ct[j, 1] - ct[i, 1]))
        i = j
        if i == i0:
            break
    return i


def _raycast(m, sy, sx, bd, maxd=220):
    """First LAND pixel along bearing `bd`; returns (pt, hit_land)."""
    a = math.radians(bd); uy, ux = -math.cos(a), math.sin(a)
    H, W = m.shape; last = (sy, sx)
    for d in range(1, maxd):
        y = int(round(sy + uy * d)); x = int(round(sx + ux * d))
        if not (0 <= y < H and 0 <= x < W):
            return last, False                      # ran off frame, no land
        if not m[y, x]:
            return (y, x), True                     # hit land
        last = (y, x)
    return last, False


def _scan_foothold(m, sr, sc, ddir, hug_side):
    """Forward-port arc scan: nearest land hit from abeam to straight ahead."""
    beam = (ddir - 90) % 360 if hug_side == "port" else (ddir + 90) % 360
    best = None; bestd = 1e9
    for off in range(-20, 91, 4):                   # abeam-ish → ahead (no behind)
        ang = (beam + off) % 360
        pt, hit = _raycast(m, sr, sc, ang)
        if not hit:
            continue
        dd = math.hypot(pt[0] - sr, pt[1] - sc)
        if dd < bestd:
            bestd = dd; best = pt
    if best is None:
        best, _ = _raycast(m, sr, sc, beam)
    return best


def _fwd_arc(ct, i0, step, H, W, maxlen=300):
    """Indices from the foothold forward until the frame-edge exit."""
    N = len(ct); i = i0; acc = 0.0; pts = [i0]
    while acc < maxlen:
        j = (i + step) % N
        acc += math.hypot(float(ct[i, 0] - ct[j, 0]), float(ct[i, 1] - ct[j, 1]))
        i = j; pts.append(i)
        y, x = ct[i]
        if acc > 20 and (y <= 2 or y >= H - 3 or x <= 2 or x >= W - 3):
            break
        if i == i0:
            break
    return pts


def _inward_normal(ct, i, m):
    N = len(ct); k = 3
    ty = float(ct[(i + k) % N, 0] - ct[(i - k) % N, 0])
    tx = float(ct[(i + k) % N, 1] - ct[(i - k) % N, 1])
    mag = math.hypot(ty, tx) or 1.0; ty /= mag; tx /= mag
    for ny, nx in ((-tx, ty), (tx, -ty)):
        py = int(round(ct[i, 0] + 3 * ny)); px = int(round(ct[i, 1] + 3 * nx))
        if 0 <= py < m.shape[0] and 0 <= px < m.shape[1] and m[py, px]:
            return ny, nx
    return -tx, ty


def _offset_pt(ct, i, m, DT, hug):
    """March the bank point inward by up to `hug`, stopping at the centerline."""
    ny, nx = _inward_normal(ct, i, m); H, W = m.shape
    by, bx = float(ct[i, 0]), float(ct[i, 1]); bestd = -1.0; best = (by, bx)
    for kk in range(1, int(hug) + 1):
        py = by + ny * kk; px = bx + nx * kk
        iy, ix = int(round(py)), int(round(px))
        if not (0 <= iy < H and 0 <= ix < W and m[iy, ix]):
            break
        dv = DT[iy, ix]
        if dv < bestd - 1.0:                         # passed the centerline
            break
        best = (py, px); bestd = dv
    return best


def _cut_pockets(pts, thresh=26.0, max_arc=70.0):
    """Skip a short dead-end notch: the line leaves and RETURNS near itself
    within a short arc.  Keeps through-passages and long dead-ends."""
    N = len(pts)
    if N < 3:
        return pts
    out = []; i = 0
    while i < N:
        out.append(pts[i]); best = i; acc = 0.0
        for j in range(i + 1, N):
            acc += math.hypot(pts[j][0] - pts[j - 1][0], pts[j][1] - pts[j - 1][1])
            if acc > max_arc:
                break
            if j > i + 2 and math.hypot(pts[i][0] - pts[j][0],
                                        pts[i][1] - pts[j][1]) < thresh:
                best = j
        i = best + 1 if best > i else i + 1
    if out[-1] != pts[-1]:          # always keep the exit point — a fully
        out.append(pts[-1])         # pocketed short trace must not collapse
    return out                      # to 1 pt (→ nav-line<2 → frozen hold)


def _dp_nav(pts, eps):
    if len(pts) < 3:
        return pts
    a = np.array([[x, y] for y, x in pts], np.float32).reshape(-1, 1, 2)
    r = cv2.approxPolyDP(a, eps, False).reshape(-1, 2)
    return [(float(y), float(x)) for x, y in r]


def _arclen_pt(nav, target):
    acc = 0.0
    for k in range(1, len(nav)):
        acc += math.hypot(nav[k][0] - nav[k - 1][0], nav[k][1] - nav[k - 1][1])
        if acc >= target:
            return nav[k]
    return nav[-1]


def _side_land(m, sr, sc, center_deg, maxd=70):
    """Nearest land distance in a ~80° fan around `center_deg`, or None."""
    best = None
    for off in (-40, -25, -10, 0, 10, 25, 40):
        pt, hit = _raycast(m, sr, sc, (center_deg + off) % 360, maxd)
        if hit:
            d = math.hypot(pt[0] - sr, pt[1] - sc)
            best = d if best is None else min(best, d)
    return best


class LoopTactical:
    """Single hugging-side bankline follower (see module docstring)."""

    name = "loop"

    def __init__(self, hug_side: str = "port", **_ignored):
        assert hug_side in ("port", "starboard")
        self.hug = hug_side
        self._commit = 0.0
        self._started = False
        self._uturning = False
        self._uturn_target = 0.0
        self._uturn_ticks = 0
        self._wrong_streak = 0

    def reset(self) -> None:
        self._commit = 0.0
        self._started = False
        self._uturning = False
        self._uturn_ticks = 0
        self._wrong_streak = 0

    def maybe_consult(self, frame: VisionFrame, state: NavState) -> NavState:
        m = state.water_mask
        if m is None:
            return state
        H, W = m.shape
        sr, sc = H // 2, W // 2
        # #2: strip UI-overlay fake-land (city anchors / "???" / markers) that
        # would make a city protrude into the hugging path.
        try:
            mm = frame.minimap()
            rgb = np.asarray(mm.convert("RGB").resize((W, H)))
            m = _strip_overlays(m, rgb)
        except Exception:
            pass
        ct = _raw_cc(m, sr, sc)
        if ct is None or len(ct) < 8:
            return self._hold(state)

        # Desired direction: ship heading (perception); prev commit as fallback.
        hdg = state.heading.bearing_deg if state.heading else None
        if hdg is not None:
            ddir = hdg
            reg = "cold_hdg" if not self._started else "hug"
        elif self._started:
            ddir = self._commit
            reg = "hug"
        else:
            return self._hold(state)                # first tick, no heading yet

        # Wrong-hugging-side U-TURN recovery.  The shore is on the WRONG side
        # when the hug side has NO land at all (open sea, checked out to 150px)
        # while the other side has land close (≤70px) — i.e. the ship is
        # hugging the wrong bank on an OPEN COAST (e.g. after a bounce off a
        # headland/port).  A CHANNEL always has some land on the hug side
        # (just farther), so it never triggers.  Persistence guards transients.
        pb = (ddir - 90) % 360 if self.hug == "port" else (ddir + 90) % 360
        ob = (ddir + 90) % 360 if self.hug == "port" else (ddir - 90) % 360
        hug_land = _side_land(m, sr, sc, pb, maxd=150)
        other_land = _side_land(m, sr, sc, ob, maxd=70)
        wrong = hug_land is None and other_land is not None
        if self._uturning:
            if hug_land is not None or self._uturn_ticks > 30:
                self._uturning = False
                self._uturn_ticks = 0
                self._wrong_streak = 0
            else:
                self._uturn_ticks += 1
                return self._emit_uturn(state, sr, sc)
        else:
            self._wrong_streak = self._wrong_streak + 1 if wrong else 0
            if self._wrong_streak >= 5:
                self._uturning = True
                self._uturn_ticks = 0
                self._uturn_target = (ddir + 180.0) % 360.0
                return self._emit_uturn(state, sr, sc)

        DT = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 5)

        # Foothold = nearest land on the forward-port arc.
        foot = _nearest_idx(ct, _scan_foothold(m, sr, sc, ddir, self.hug))

        # Forward sense = the walk whose 40 px reflex aligns with the desired dir.
        rp = ct[_walk_arclen(ct, foot, 1, REFLEX_PX)]
        rm = ct[_walk_arclen(ct, foot, -1, REFLEX_PX)]
        dp = abs(((_bearing(sr, sc, rp[0], rp[1]) - ddir + 540) % 360) - 180)
        dm = abs(((_bearing(sr, sc, rm[0], rm[1]) - ddir + 540) % 360) - 180)
        step = 1 if dp <= dm else -1

        # Nav line = offset bank forward-trace, pockets cut, lightly smoothed.
        arc = _fwd_arc(ct, foot, step, H, W)
        navline = [_offset_pt(ct, i, m, DT, HUG_PX) for i in arc]
        navline = _cut_pockets(navline)
        navline = _dp_nav(navline, 5.0)
        if len(navline) < 2:
            return self._hold(state)

        rf = _arclen_pt(navline, REFLEX_PX)
        tp = _arclen_pt(navline, TACTICAL_PX)
        commit = _bearing(sr, sc, rf[0], rf[1])
        self._commit = commit
        self._started = True

        off = (int(tp[0] - sr), int(tp[1] - sc))
        state.commit_direction = CommitDirection(
            bearing_deg=commit, reason=f"loop({self.hug},{reg})",
            set_at_tick=state.tick)
        state.tactical_dest_px_offset = off
        if state.lat is not None and state.lon is not None:
            state.tactical_dest_latlon = (
                state.lat + (-off[0] / PX_PER_DEG),
                state.lon + (off[1] / PX_PER_DEG))
        log.log(logging.INFO if reg == "cold_hdg" else logging.DEBUG,
                "[loop] t=%d commit=%.0f (%s) foothold=(%d,%d) reflex=(%d,%d)",
                state.tick, commit, reg, int(ct[foot, 0]), int(ct[foot, 1]),
                int(rf[0]), int(rf[1]))
        return state

    def _emit_uturn(self, state: NavState, sr, sc) -> NavState:
        commit = self._uturn_target
        self._commit = commit
        self._started = True
        a = math.radians(commit)
        off = (int(-math.cos(a) * TACTICAL_PX), int(math.sin(a) * TACTICAL_PX))
        state.commit_direction = CommitDirection(
            bearing_deg=commit, reason="loop_uturn", set_at_tick=state.tick)
        state.tactical_dest_px_offset = off
        if state.lat is not None and state.lon is not None:
            state.tactical_dest_latlon = (
                state.lat + (-off[0] / PX_PER_DEG),
                state.lon + (off[1] / PX_PER_DEG))
        log.info("[loop] t=%d U-TURN commit=%.0f (wrong hug-side)",
                 state.tick, commit)
        return state

    def _hold(self, state: NavState) -> NavState:
        if self._commit:
            state.commit_direction = CommitDirection(
                bearing_deg=self._commit, reason="loop_hold",
                set_at_tick=state.tick)
        return state
