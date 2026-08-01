"""Diagnostic for ship-heading detection across tick frames.

Loads a saved mini-map crop (one of the tick_NNNN.png files written
during a hug-shore voyage), runs the existing ShipHeadingDetector,
and produces a multi-panel image showing what each stage of the
pipeline sees.  Also runs candidate alternative methods on the same
frame so they can be compared side-by-side.

Usage:
  python -m tools.diag_ship_heading \
      data/sessions/hug_debug_20260531_234502/tick_0134.png \
      data/sessions/hug_debug_20260531_234502/tick_0135.png \
      --out /tmp/heading_diag.png

  python -m tools.diag_ship_heading \
      data/sessions/hug_debug_20260531_234502/tick_{0001,0050,0134,0135}.png \
      --methods baseline,compactness,text_mask \
      --out /tmp/heading_compare.png

Methods:
  baseline      — today's MinimapNavigationView pipeline.
  compactness   — adds a compactness filter (4πA/P² ≥ thr) on yellow
                  candidates before strategies run.
  text_mask     — EasyOCR-detected text regions are zeroed from the
                  yellow mask before candidate collection.
  sprite_mask   — detect compact very-white clusters near the ship
                  (= village house icon / port marker) and mask those
                  pixels from ship_green AND yellow_any.  Treats
                  overlapping sprites as occluders.
  white_aware   — reclaim occluded ship pixels by dilating ship_green
                  into adjacent very-white pixels (reconstructs the
                  ship-icon blob where the village house overlapped).
  sticky        — if baseline returns pca_fallback with conf < 0.5,
                  fall back to a passed-in previous heading instead.
                  Run baseline first to capture the prior; ticks
                  processed left-to-right so prior is the prev frame's
                  best accepted reading.

Output is a single PNG with one row per input frame and one column
per requested method.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from vision.minimap_navigation_view import (
    _clean_ship_green,
    _color_masks,
    _ship_heading_detector as DET,
)


# ── Method variants ──────────────────────────────────────────────────


def _compactness(comp: np.ndarray) -> float:
    """4·π·A / P² for a binary blob; 1.0 = perfect disc, ~0 = line.

    Perimeter approximated as the count of pixels with a non-blob
    neighbour (4-connected).  Sufficient for the relative filter we want.
    """
    area = int(comp.sum())
    if area <= 0:
        return 0.0
    # Edge pixels = pixels in comp that have at least one non-comp neighbour.
    h, w = comp.shape
    pad = np.zeros((h + 2, w + 2), dtype=bool)
    pad[1:-1, 1:-1] = comp
    n4 = (pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:])
    interior = n4
    perim = int(comp.sum()) - int(interior.sum())
    perim = max(perim, 1)
    return 4.0 * math.pi * area / (perim * perim)


def _sprite_occluder_mask(color: dict, ship_cx: Optional[float],
                           ship_cy: Optional[float],
                           radius_px: float = 40.0,
                           min_area: int = 40,
                           max_area: int = 800,
                           min_compactness: float = 0.35,
                           ) -> tuple[np.ndarray, list[dict]]:
    """Pixel mask of overlapping sprite icons (village house, port
    diamond) near the ship — to be AND-NOT'd off ship_green / yellow_any.

    Heuristic: compact bright-white connected components within
    `radius_px` of the ship centroid, with area in [min_area, max_area]
    and compactness ≥ min_compactness.  The village-house icon at
    ~150-400 px and a port-marker diamond at ~50-150 px both fit; text
    glyphs fail compactness and small NPC sails fail area.
    """
    from scipy.ndimage import label as ndi_label
    very_white = color["very_white"] | color["light_gray"]
    labels, n = ndi_label(very_white)
    if n == 0 or ship_cx is None:
        return np.zeros_like(very_white), []
    out_mask = np.zeros_like(very_white)
    debug = []
    for lbl in range(1, n + 1):
        comp = (labels == lbl)
        area = int(comp.sum())
        if not (min_area <= area <= max_area):
            continue
        cys, cxs = np.where(comp)
        wx, wy = float(cxs.mean()), float(cys.mean())
        dist = math.hypot(wx - ship_cx, wy - ship_cy)
        if dist > radius_px:
            continue
        c = _compactness(comp)
        if c < min_compactness:
            continue
        out_mask |= comp
        debug.append({"area": area, "dist": dist, "compactness": c,
                      "wx": wx, "wy": wy})
    return out_mask, debug


def _yellow_mask_text_excluded(yellow: np.ndarray, rgb: np.ndarray
                                ) -> tuple[np.ndarray, list]:
    """Run EasyOCR on the crop, build a pixel mask of text regions, and
    AND-NOT it from the yellow mask.

    Returns (filtered_yellow, list_of_text_bboxes_for_drawing).
    """
    try:
        import easyocr
    except ImportError:
        return yellow, []
    # Lazy singleton — EasyOCR init is slow.
    global _OCR
    try:
        _OCR  # type: ignore[used-before-def]
    except NameError:
        _OCR = easyocr.Reader(["en"], gpu=False, verbose=False)
    results = _OCR.readtext(rgb)
    h, w = yellow.shape
    text_mask = np.zeros_like(yellow)
    boxes = []
    for box, txt, conf in results:
        if conf < 0.3:
            continue
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        x0, x1 = max(0, min(xs)), min(w, max(xs))
        y0, y1 = max(0, min(ys)), min(h, max(ys))
        text_mask[y0:y1, x0:x1] = True
        boxes.append((x0, y0, x1, y1, txt, conf))
    return yellow & ~text_mask, boxes


# ── Candidate collection with annotations ────────────────────────────


def _collect_cands_annotated(green, yellow, cx, cy, principal):
    """Mirror of ShipHeadingDetector._collect_yellow_candidates but
    returns per-candidate compactness + the connected-component mask
    so we can draw it later."""
    from scipy.ndimage import binary_dilation, label as ndi_label
    yel_labels, n_yel = ndi_label(yellow)
    if n_yel == 0:
        return []
    green_dil = binary_dilation(green, iterations=2)
    out = []
    for lbl in range(1, n_yel + 1):
        comp = (yel_labels == lbl)
        area = int(comp.sum())
        cys, cxs = np.where(comp)
        if cxs.size == 0:
            continue
        wx, wy = float(cxs.mean()), float(cys.mean())
        dx, dy = wx - cx, wy - cy
        dist = math.hypot(dx, dy)
        if dist < 1e-3:
            continue
        touches = bool((comp & green_dil).any())
        on_axis = True
        if principal is not None:
            proj = abs(dx * principal[0] + dy * principal[1])
            on_axis = (proj / dist) >= DET.AXIS_PROJECTION_MIN
        ang = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
        out.append({
            "area": area, "dist": dist, "angle": ang,
            "wx": wx, "wy": wy,
            "touches_green": touches, "on_axis": on_axis,
            "compactness": _compactness(comp),
            "comp_mask": comp,
        })
    return out


# ── Method runners — each returns (reading_or_None, debug_dict) ──────


def _run_baseline(rgb: np.ndarray):
    color = _color_masks(rgb)
    green = _clean_ship_green(color["ship_green"])
    yellow = color["yellow_any"]
    gys, gxs = np.where(green)
    if gxs.size < DET.MIN_GREEN_PIXELS:
        return None, {"ship_pixels": gxs.size, "cands": [], "yellow_used": yellow}
    cx, cy = float(gxs.mean()), float(gys.mean())
    principal = DET._principal_axis(gxs, gys, cx, cy)
    cands = _collect_cands_annotated(green, yellow, cx, cy, principal)
    reading = (DET._sail_pair(cands)
               or DET._bow_wedge(cands)
               or DET._pca_fallback(gxs, gys, cx, cy))
    return reading, {
        "cx": cx, "cy": cy, "principal": principal,
        "cands": cands, "green": green, "yellow_used": yellow,
    }


def _run_compactness(rgb: np.ndarray, thr: float = 0.35):
    """Apply compactness filter on `cands` before the strategies."""
    color = _color_masks(rgb)
    green = _clean_ship_green(color["ship_green"])
    yellow = color["yellow_any"]
    gys, gxs = np.where(green)
    if gxs.size < DET.MIN_GREEN_PIXELS:
        return None, {"cands": [], "yellow_used": yellow}
    cx, cy = float(gxs.mean()), float(gys.mean())
    principal = DET._principal_axis(gxs, gys, cx, cy)
    cands_all = _collect_cands_annotated(green, yellow, cx, cy, principal)
    cands = [c for c in cands_all if c["compactness"] >= thr]
    reading = (DET._sail_pair(cands)
               or DET._bow_wedge(cands)
               or DET._pca_fallback(gxs, gys, cx, cy))
    return reading, {
        "cx": cx, "cy": cy, "principal": principal,
        "cands": cands_all, "kept": cands, "thr": thr,
        "green": green, "yellow_used": yellow,
    }


def _run_text_mask(rgb: np.ndarray):
    color = _color_masks(rgb)
    yellow0 = color["yellow_any"]
    yellow, boxes = _yellow_mask_text_excluded(yellow0, rgb)
    green = _clean_ship_green(color["ship_green"])
    gys, gxs = np.where(green)
    if gxs.size < DET.MIN_GREEN_PIXELS:
        return None, {"cands": [], "yellow_used": yellow, "text_boxes": boxes}
    cx, cy = float(gxs.mean()), float(gys.mean())
    principal = DET._principal_axis(gxs, gys, cx, cy)
    cands = _collect_cands_annotated(green, yellow, cx, cy, principal)
    reading = (DET._sail_pair(cands)
               or DET._bow_wedge(cands)
               or DET._pca_fallback(gxs, gys, cx, cy))
    return reading, {
        "cx": cx, "cy": cy, "principal": principal,
        "cands": cands, "text_boxes": boxes,
        "green": green, "yellow_used": yellow,
    }


def _run_sprite_mask(rgb: np.ndarray):
    """Mask out compact white sprites near the ship before detection."""
    color = _color_masks(rgb)
    # Need a coarse ship centroid first (use raw green for the locator).
    ship_xy = None
    raw_green = color["ship_green"]
    if raw_green.any():
        gys, gxs = np.where(raw_green)
        ship_xy = (float(gxs.mean()), float(gys.mean()))
    occluder, occ_debug = _sprite_occluder_mask(color,
                                                  ship_xy[0] if ship_xy else None,
                                                  ship_xy[1] if ship_xy else None)
    # Mask BOTH inputs.  Note: occluder is the WHITE pixels of the
    # sprite — they aren't in ship_green or yellow_any anyway, but
    # masking them ensures any color-bleed pixels don't contaminate.
    green_in = color["ship_green"] & ~occluder
    yellow_in = color["yellow_any"] & ~occluder
    green = _clean_ship_green(green_in)
    gys, gxs = np.where(green)
    if gxs.size < DET.MIN_GREEN_PIXELS:
        return None, {"cands": [], "yellow_used": yellow_in,
                       "occluder": occluder, "occ_debug": occ_debug}
    cx, cy = float(gxs.mean()), float(gys.mean())
    principal = DET._principal_axis(gxs, gys, cx, cy)
    cands = _collect_cands_annotated(green, yellow_in, cx, cy, principal)
    reading = (DET._sail_pair(cands)
               or DET._bow_wedge(cands)
               or DET._pca_fallback(gxs, gys, cx, cy))
    return reading, {
        "cx": cx, "cy": cy, "principal": principal,
        "cands": cands, "green": green, "yellow_used": yellow_in,
        "occluder": occluder, "occ_debug": occ_debug,
    }


def _pca_with_tip_selector(gys, gxs, cx, cy, tip_fn):
    """Run PCA on the green pixel set, then ask tip_fn(principal, pts,
    cx, cy) which end is the bow.  Returns a HeadingReading."""
    from vision.minimap_navigation_view import HeadingReading
    pts = np.column_stack([gxs - cx, gys - cy]).astype(float)
    try:
        cov = np.cov(pts, rowvar=False)
        eigvals, eigvecs = np.linalg.eigh(cov)
        principal = eigvecs[:, np.argmax(eigvals)]
    except Exception:
        return None
    bow_vec = tip_fn(principal, pts)
    if bow_vec is None:
        return None
    ang = float((math.degrees(math.atan2(bow_vec[0], -bow_vec[1])) + 360) % 360)
    return HeadingReading(ang, "pointy_tip", 0.45)


def _tip_pointy_end(principal, pts):
    """Pick the principal-axis direction whose tip slice is NARROWER
    (smaller perpendicular cross-section)."""
    proj = pts @ principal           # axial coord
    perp_vec = np.array([-principal[1], principal[0]])
    perp = pts @ perp_vec            # perpendicular coord
    # Slice the 25% of points furthest along each direction; measure
    # the perpendicular std on that slice.  Narrower slice = pointier
    # end = bow.
    n = len(proj)
    if n < 8:
        return None
    cut = max(2, n // 4)
    pos_idx = np.argsort(proj)[-cut:]     # furthest in +principal
    neg_idx = np.argsort(proj)[:cut]      # furthest in -principal
    pos_width = float(perp[pos_idx].std())
    neg_width = float(perp[neg_idx].std())
    # Bow = narrower end → bow_vec points toward the narrower side.
    return principal if pos_width < neg_width else -principal


def _run_pointy_tip(rgb: np.ndarray):
    """Use text_close-reconstructed hull, then PCA + narrower-end-is-bow
    tip selection."""
    # Reuse text_close pipeline to rebuild the hull.
    _, dbg = _run_text_close(rgb)
    green = dbg.get("green")
    if green is None or not green.any():
        return None, dbg
    gys, gxs = np.where(green)
    if gxs.size < 8:
        return None, dbg
    cx, cy = float(gxs.mean()), float(gys.mean())
    reading = _pca_with_tip_selector(gys, gxs, cx, cy, _tip_pointy_end)
    out = dict(dbg)
    out.update({"cx": cx, "cy": cy})
    return reading, out


def _run_pca_yellow_anchor(rgb: np.ndarray, dilate_iters: int = 2):
    """Bow direction = principal axis of green hull, oriented so the
    yellow centroid lies on the bow end.

    Rationale (from user, t134/t135 diagnostic): when text occludes
    half the yellow circular sector, the visible yellow is one sail
    (not the bow apex), so green→yellow vector is biased toward that
    sail.  But the BOW DIRECTION is the principal axis of the hull,
    which is independent of which sail survived.  The yellow centroid
    still tells us which END of the principal axis is the bow, since
    yellow only exists at the bow."""
    from scipy.ndimage import binary_dilation, label as ndi_label
    from vision.minimap_navigation_view import HeadingReading
    _, dbg = _run_text_close(rgb)
    green = dbg.get("green")
    color = _color_masks(rgb)
    yellow = color["yellow_any"]
    if green is None or not green.any() or not yellow.any():
        return None, dbg
    # Touches-green filter (same as _run_yellow_centroid_touches).
    green_dil = binary_dilation(green, iterations=dilate_iters)
    labels, n = ndi_label(yellow)
    touching = np.zeros_like(yellow)
    for lbl in range(1, n + 1):
        comp = (labels == lbl)
        if (comp & green_dil).any():
            touching |= comp
    if int(touching.sum()) < 4:
        return None, dbg
    gys, gxs = np.where(green)
    yys, yxs = np.where(touching)
    gcx, gcy = float(gxs.mean()), float(gys.mean())
    ycx, ycy = float(yxs.mean()), float(yys.mean())

    # PCA on green for principal axis direction (undirected).
    pts = np.column_stack([gxs - gcx, gys - gcy]).astype(float)
    try:
        cov = np.cov(pts, rowvar=False)
        eigvals, eigvecs = np.linalg.eigh(cov)
        principal = eigvecs[:, np.argmax(eigvals)]  # 2-vector
    except Exception:
        return None, dbg

    # Project (yellow - green) onto principal.  Positive projection
    # means yellow lies in +principal direction → bow_vec = +principal.
    dx, dy = ycx - gcx, ycy - gcy
    proj = dx * principal[0] + dy * principal[1]
    bow_vec = principal if proj > 0 else -principal

    ang = float((math.degrees(math.atan2(bow_vec[0], -bow_vec[1])) + 360) % 360)
    out = dict(dbg)
    out["yellow_used"] = touching
    out.update({"cx": gcx, "cy": gcy,
                 "yellow_centroid": (ycx, ycy),
                 "principal": principal})
    return HeadingReading(ang, "pca_yellow_anchor", 0.65), out


def _run_yellow_centroid_touches(rgb: np.ndarray,
                                   dilate_iters: int = 2,
                                   min_yellow_px: int = 4):
    """yellow_centroid variant that only counts yellow connected
    components that ACTUALLY TOUCH the cleaned ship_green hull (within
    `dilate_iters` of dilation).  Filters out NPC ship yellows and
    village-shadow yellows that are spatially separate.

    Rationale (t134 diagnostic): yellow_centroid_full pulled in
    village-shadow yellow at the lower-right of the crop and produced
    a wrong-direction reading (207° vs 255° truth).  Requiring the
    yellow CC to touch the green hull restricts the signal to the
    ship's own sails/bow-arc."""
    from scipy.ndimage import binary_dilation, label as ndi_label
    from vision.minimap_navigation_view import HeadingReading
    _, dbg = _run_text_close(rgb)
    green = dbg.get("green")
    color = _color_masks(rgb)
    yellow = color["yellow_any"]
    if green is None or not green.any() or not yellow.any():
        return None, dbg
    # Find yellow CCs that touch the green hull (allowing a small
    # dilation gap, since text glyphs may break contact).
    green_dil = binary_dilation(green, iterations=dilate_iters)
    labels, n = ndi_label(yellow)
    touching = np.zeros_like(yellow)
    n_touching_cc = 0
    for lbl in range(1, n + 1):
        comp = (labels == lbl)
        if (comp & green_dil).any():
            touching |= comp
            n_touching_cc += 1
    if int(touching.sum()) < min_yellow_px:
        out = dict(dbg)
        out["yellow_used"] = touching
        out["touching_cc"] = n_touching_cc
        out["touching_px"] = int(touching.sum())
        return None, out
    gys, gxs = np.where(green)
    yys, yxs = np.where(touching)
    gcx, gcy = float(gxs.mean()), float(gys.mean())
    ycx = float(yxs.mean())
    ycy = float(yys.mean())
    dx, dy = ycx - gcx, ycy - gcy
    if math.hypot(dx, dy) < 1e-3:
        return None, dbg
    ang = float((math.degrees(math.atan2(dx, -dy)) + 360) % 360)
    out = dict(dbg)
    out["yellow_used"] = touching
    out["touching_cc"] = n_touching_cc
    out["touching_px"] = int(touching.sum())
    out.update({"cx": gcx, "cy": gcy, "yellow_centroid": (ycx, ycy)})
    return HeadingReading(ang, "yellow_centroid_touches", 0.6), out


def _run_yellow_centroid_full(rgb: np.ndarray, radius_px: float = 40.0):
    """yellow_centroid variant that uses UNMASKED yellow_any (no OCR
    bbox suppression) — preserves bow-area yellow pixels that happen
    to fall inside a text bbox.  Wider proximity gate.

    Rationale (from t134 diagnostic): when "Nubia V" text bbox
    overlaps the bow, the bbox-masking removes the bow yellow even
    though those pixels aren't text glyphs.  Using raw yellow_any
    keeps them; the proximity gate filters distant NPC sprites."""
    from vision.minimap_navigation_view import HeadingReading
    # Use text_close to get the reconstructed GREEN, but keep RAW yellow.
    _, dbg = _run_text_close(rgb)
    green = dbg.get("green")
    color = _color_masks(rgb)
    yellow = color["yellow_any"]  # raw, not text-masked
    if green is None or not green.any() or not yellow.any():
        return None, dbg
    gys, gxs = np.where(green)
    yys, yxs = np.where(yellow)
    gcx, gcy = float(gxs.mean()), float(gys.mean())
    d2 = (yxs - gcx) ** 2 + (yys - gcy) ** 2
    near = d2 <= radius_px ** 2
    if not near.any():
        return None, dbg
    ycx = float(yxs[near].mean())
    ycy = float(yys[near].mean())
    dx, dy = ycx - gcx, ycy - gcy
    if math.hypot(dx, dy) < 1e-3:
        return None, dbg
    ang = float((math.degrees(math.atan2(dx, -dy)) + 360) % 360)
    out = dict(dbg)
    out["yellow_used"] = yellow
    out.update({"cx": gcx, "cy": gcy,
                 "yellow_centroid": (ycx, ycy)})
    return HeadingReading(ang, "yellow_centroid_full", 0.55), out


def _run_yellow_centroid(rgb: np.ndarray):
    """Bearing = vector from green centroid to YELLOW centroid.
    Yellow lives at the bow (sails/arc); stern has no yellow."""
    from vision.minimap_navigation_view import HeadingReading
    # Reuse text_close hull reconstruction for the green centroid.
    _, dbg = _run_text_close(rgb)
    green = dbg.get("green")
    yellow = dbg.get("yellow_used")
    if green is None or yellow is None or not green.any() or not yellow.any():
        return None, dbg
    gys, gxs = np.where(green)
    yys, yxs = np.where(yellow)
    gcx, gcy = float(gxs.mean()), float(gys.mean())
    # Restrict yellow to pixels within ~ship-size of the ship centroid
    # so we don't pull in distant NPC sprites.
    d2 = (yxs - gcx) ** 2 + (yys - gcy) ** 2
    near = d2 <= 25.0 ** 2
    if not near.any():
        return None, dbg
    ycx = float(yxs[near].mean())
    ycy = float(yys[near].mean())
    dx, dy = ycx - gcx, ycy - gcy
    if math.hypot(dx, dy) < 1e-3:
        return None, dbg
    ang = float((math.degrees(math.atan2(dx, -dy)) + 360) % 360)
    out = dict(dbg)
    out.update({"cx": gcx, "cy": gcy,
                 "yellow_centroid": (ycx, ycy)})
    return HeadingReading(ang, "yellow_centroid", 0.55), out


def _run_text_close(rgb: np.ndarray):
    """Detect OCR text bboxes, then morphologically close the green
    mask THROUGH the text region to bridge fragments.

    Rationale (from t134/t135 wedge close-up): "Nubia Village" text
    glyphs sit on top of the ship icon, replacing ~60% of the ship's
    green hull pixels with text-colored pixels.  What remains is a
    fragmented blob — PCA on it is noise.  Binary closing through the
    text bbox merges the surviving green fragments into a single
    plausible silhouette.
    """
    from scipy.ndimage import binary_closing
    color = _color_masks(rgb)
    yellow0 = color["yellow_any"]
    yellow, boxes = _yellow_mask_text_excluded(yellow0, rgb)
    green = color["ship_green"]
    if boxes:
        # Closing only within ROIs that include the text region.
        radius = 3
        kernel = np.ones((radius * 2 + 1, radius * 2 + 1), dtype=bool)
        green_closed = binary_closing(green, structure=kernel, iterations=1)
        # Use the closed version inside text-affected regions; original
        # elsewhere (avoid over-merging unrelated greenery on land).
        text_mask = np.zeros_like(green)
        h, w = green.shape
        pad = 4
        for (x0, y0, x1, y1, *_rest) in boxes:
            text_mask[max(0, y0-pad):min(h, y1+pad),
                      max(0, x0-pad):min(w, x1+pad)] = True
        green_hybrid = np.where(text_mask, green_closed, green)
    else:
        green_hybrid = green
    green = _clean_ship_green(green_hybrid)
    gys, gxs = np.where(green)
    if gxs.size < DET.MIN_GREEN_PIXELS:
        return None, {"cands": [], "yellow_used": yellow,
                       "text_boxes": boxes, "green": green}
    cx, cy = float(gxs.mean()), float(gys.mean())
    principal = DET._principal_axis(gxs, gys, cx, cy)
    cands = _collect_cands_annotated(green, yellow, cx, cy, principal)
    reading = (DET._sail_pair(cands)
               or DET._bow_wedge(cands)
               or DET._pca_fallback(gxs, gys, cx, cy))
    return reading, {
        "cx": cx, "cy": cy, "principal": principal,
        "cands": cands, "green": green, "yellow_used": yellow,
        "text_boxes": boxes,
    }


def _run_white_aware(rgb: np.ndarray, dilate_iters: int = 3):
    """Reclaim ship pixels by dilating ship_green into adjacent
    very-white pixels.  Reconstructs the ship-icon blob where the
    village house roof occluded it."""
    from scipy.ndimage import binary_dilation
    color = _color_masks(rgb)
    green_raw = color["ship_green"]
    very_white = color["very_white"]
    # Find very-white pixels within `dilate_iters` of green; add them in.
    near_green = binary_dilation(green_raw, iterations=dilate_iters)
    reclaim = near_green & very_white
    green_aug = green_raw | reclaim
    green = _clean_ship_green(green_aug)
    yellow = color["yellow_any"]
    gys, gxs = np.where(green)
    if gxs.size < DET.MIN_GREEN_PIXELS:
        return None, {"cands": [], "yellow_used": yellow,
                       "reclaim": reclaim, "green": green}
    cx, cy = float(gxs.mean()), float(gys.mean())
    principal = DET._principal_axis(gxs, gys, cx, cy)
    cands = _collect_cands_annotated(green, yellow, cx, cy, principal)
    reading = (DET._sail_pair(cands)
               or DET._bow_wedge(cands)
               or DET._pca_fallback(gxs, gys, cx, cy))
    return reading, {
        "cx": cx, "cy": cy, "principal": principal,
        "cands": cands, "green": green, "yellow_used": yellow,
        "reclaim": reclaim,
    }


# Module-level: previous-tick reading (sticky method).
_PREV_READING: Optional[float] = None


def _run_sticky(rgb: np.ndarray):
    """If baseline returns pca_fallback with conf < 0.5, return the
    previous accepted heading; otherwise pass through and update prior."""
    global _PREV_READING
    reading, debug = _run_baseline(rgb)
    if reading is not None and (
            reading.strategy == "pca_fallback" and reading.confidence < 0.5):
        if _PREV_READING is not None:
            # Substitute synthetic reading; keep diag pointing at baseline.
            from vision.minimap_navigation_view import HeadingReading
            substituted = HeadingReading(
                bearing_deg=_PREV_READING,
                strategy="sticky(prev)",
                confidence=0.4,
            )
            debug["substituted"] = True
            debug["prev"] = _PREV_READING
            return substituted, debug
        # else: no prior, fall through and adopt this reading as prior
    if reading is not None:
        _PREV_READING = reading.bearing_deg
    return reading, debug


METHODS = {
    "baseline":             _run_baseline,
    "compactness":          _run_compactness,
    "text_mask":            _run_text_mask,
    "sprite_mask":          _run_sprite_mask,
    "white_aware":          _run_white_aware,
    "text_close":           _run_text_close,
    "pointy_tip":           _run_pointy_tip,
    "yellow_centroid":      _run_yellow_centroid,
    "yellow_centroid_full": _run_yellow_centroid_full,
    "yellow_centroid_touches": _run_yellow_centroid_touches,
    "pca_yellow_anchor":    _run_pca_yellow_anchor,
    "sticky":               _run_sticky,
}


# ── Rendering ────────────────────────────────────────────────────────


def _font(size=11):
    for p in ("/System/Library/Fonts/Menlo.ttc",
              "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _render_panel(rgb: np.ndarray, reading, debug: dict,
                  title: str, scale: int = 3) -> Image.Image:
    """One panel per (frame, method): overlay candidates & heading arrow,
    draw a caption with the reading + diagnostics."""
    SCALE = scale
    base = Image.fromarray(rgb).resize(
        (rgb.shape[1] * SCALE, rgb.shape[0] * SCALE), Image.NEAREST)
    # Overlay: dim the base so annotations pop.
    dim = Image.new("RGBA", base.size, (0, 0, 0, 60))
    base = Image.alpha_composite(base.convert("RGBA"), dim).convert("RGB")
    draw = ImageDraw.Draw(base)
    f = _font(11)

    # Text boxes (text_mask method)
    for box in debug.get("text_boxes", []) or []:
        x0, y0, x1, y1, txt, conf = box
        draw.rectangle(
            (x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE),
            outline=(255, 0, 255), width=2)

    # Occluder mask (sprite_mask method) — magenta tint over those pixels.
    occluder = debug.get("occluder")
    if occluder is not None and occluder.any():
        overlay = np.zeros((*occluder.shape, 4), dtype=np.uint8)
        overlay[occluder] = (255, 0, 200, 130)
        ov_img = Image.fromarray(overlay).resize(
            (occluder.shape[1] * SCALE, occluder.shape[0] * SCALE),
            Image.NEAREST)
        base = Image.alpha_composite(base.convert("RGBA"), ov_img).convert("RGB")
        draw = ImageDraw.Draw(base)

    # Reclaimed pixels (white_aware method) — cyan tint.
    reclaim = debug.get("reclaim")
    if reclaim is not None and reclaim.any():
        overlay = np.zeros((*reclaim.shape, 4), dtype=np.uint8)
        overlay[reclaim] = (0, 255, 255, 130)
        ov_img = Image.fromarray(overlay).resize(
            (reclaim.shape[1] * SCALE, reclaim.shape[0] * SCALE),
            Image.NEAREST)
        base = Image.alpha_composite(base.convert("RGBA"), ov_img).convert("RGB")
        draw = ImageDraw.Draw(base)

    # Show cleaned ship_green outline (yellow tint for visibility).
    green = debug.get("green")
    if green is not None and green.any():
        overlay = np.zeros((*green.shape, 4), dtype=np.uint8)
        overlay[green] = (255, 255, 0, 100)
        ov_img = Image.fromarray(overlay).resize(
            (green.shape[1] * SCALE, green.shape[0] * SCALE),
            Image.NEAREST)
        base = Image.alpha_composite(base.convert("RGBA"), ov_img).convert("RGB")
        draw = ImageDraw.Draw(base)

    cx, cy = debug.get("cx"), debug.get("cy")
    cands = debug.get("cands", [])
    kept = debug.get("kept")  # compactness method only
    kept_ids = {id(c) for c in kept} if kept is not None else None

    # Candidates: circle + label
    for c in cands:
        wx, wy = c["wx"], c["wy"]
        col = (0, 200, 0) if c["touches_green"] and c["on_axis"] else (200, 200, 0)
        if kept_ids is not None and id(c) not in kept_ids:
            col = (200, 80, 80)  # filtered out by compactness
        draw.ellipse(
            (wx * SCALE - 8, wy * SCALE - 8,
             wx * SCALE + 8, wy * SCALE + 8),
            outline=col, width=2)
        label = (f"a={c['area']} c={c['compactness']:.2f} "
                 f"d={c['dist']:.1f} ax={int(c['on_axis'])}t={int(c['touches_green'])}")
        draw.text((wx * SCALE + 9, wy * SCALE - 6), label,
                  font=f, fill=col)

    # Ship centroid + heading arrow
    if cx is not None:
        draw.ellipse(
            (cx * SCALE - 3, cy * SCALE - 3,
             cx * SCALE + 3, cy * SCALE + 3),
            fill=(0, 200, 255), outline=(0, 0, 0))
    if reading is not None and cx is not None:
        ang = math.radians(reading.bearing_deg)
        L = 40
        ex = cx * SCALE + L * math.sin(ang)
        ey = cy * SCALE - L * math.cos(ang)
        draw.line((cx * SCALE, cy * SCALE, ex, ey),
                  fill=(0, 255, 80), width=3)
        head = (
            f"{title}\n"
            f"bearing={reading.bearing_deg:.0f}°  "
            f"strategy={reading.strategy}  conf={reading.confidence:.2f}\n"
            f"cands={len(cands)}"
        )
    else:
        head = f"{title}\nNO READING\ncands={len(cands)}"
    # Caption strip at top
    strip = Image.new("RGB", (base.size[0], 48), (40, 40, 40))
    ImageDraw.Draw(strip).text((6, 4), head, font=f, fill=(255, 255, 255))
    combined = Image.new("RGB", (base.size[0], base.size[1] + 48), (0, 0, 0))
    combined.paste(strip, (0, 0))
    combined.paste(base, (0, 48))
    return combined


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", nargs="+", type=Path,
                    help="tick_NNNN.png mini-map crops from a hug session")
    ap.add_argument("--methods", default="baseline,compactness,text_mask",
                    help="comma-separated subset of: " + ",".join(METHODS))
    ap.add_argument("--out", default="/tmp/heading_diag.png", type=Path)
    ap.add_argument("--scale", default=3, type=int,
                    help="per-panel scale factor (3 = small, 6 = readable)")
    args = ap.parse_args()
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    for m in methods:
        if m not in METHODS:
            raise SystemExit(f"unknown method: {m}")

    panels = []
    for fpath in args.frames:
        rgb = np.asarray(Image.open(fpath).convert("RGB"))
        row = []
        for m in methods:
            reading, dbg = METHODS[m](rgb)
            title = f"{fpath.name}  [{m}]"
            row.append(_render_panel(rgb, reading, dbg, title, scale=args.scale))
        panels.append(row)
        # Console summary
        print(f"=== {fpath.name} ===")
        for m in methods:
            reading, dbg = METHODS[m](rgb)
            extras = []
            if "kept" in dbg:
                extras.append(f"kept={len(dbg['kept'])}/{len(dbg['cands'])}")
            if "text_boxes" in dbg:
                extras.append(f"text_boxes={len(dbg['text_boxes'])}")
            if "occ_debug" in dbg:
                extras.append(f"occluders={len(dbg['occ_debug'])}")
                if dbg["occ_debug"]:
                    sizes = ",".join(f"a={o['area']}d={o['dist']:.0f}c={o['compactness']:.2f}"
                                      for o in dbg["occ_debug"])
                    extras.append(f"[{sizes}]")
            if "reclaim" in dbg:
                extras.append(f"reclaim_px={int(dbg['reclaim'].sum())}")
            green = dbg.get("green")
            if green is not None:
                extras.append(f"green_px={int(green.sum())}")
            extras.append(f"cands={len(dbg.get('cands', []))}")
            extra_str = "  " + " ".join(extras) if extras else ""
            if reading is None:
                print(f"  {m:>12}: NO READING{extra_str}")
            else:
                print(f"  {m:>12}: brg={reading.bearing_deg:6.1f}°  "
                      f"strat={reading.strategy:<12}  conf={reading.confidence:.2f}"
                      f"{extra_str}")

    # Compose grid: rows = frames, cols = methods.
    cell_w = max(p.size[0] for row in panels for p in row)
    cell_h = max(p.size[1] for row in panels for p in row)
    grid = Image.new("RGB",
                     (cell_w * len(methods), cell_h * len(panels)),
                     (20, 20, 20))
    for r, row in enumerate(panels):
        for c, panel in enumerate(row):
            grid.paste(panel, (c * cell_w, r * cell_h))
    grid.save(args.out)
    print(f"\nWrote {args.out}  ({grid.size})")


if __name__ == "__main__":
    main()
