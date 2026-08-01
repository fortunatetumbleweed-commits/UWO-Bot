"""Visual probe for mini-map image-processing navigation.

Takes one or more sea frames, crops the mini-map, runs candidate
image-processing primitives, and writes a side-by-side debug PNG so
you can eyeball whether the thresholds make sense before we promote
the code to `vision/minimap_navigation_view.py`.

Pipeline visualised in the debug panel:

    [original mini-map crop]
    [HSV channels: H | S | V]
    [color masks: ship green | yellow anchors | saturated mask]
    [land mask candidates: bright-threshold | dark-threshold | edge-density]
    [sector overlay with land fractions printed]

Usage:
    python tools/explore_minimap_geometry.py \\
        --frame data/sessions/observe_2026-05-27_16-44-29/frames/0010_164950703.png \\
        [--out /tmp/mm_debug.png]

    python tools/explore_minimap_geometry.py \\
        --session observe_2026-05-27_16-44-29 \\
        --limit 5

The first form produces one debug PNG; the second walks N frames in a
session and writes a numbered PNG per frame to /tmp/mm_debug_<n>.png.
"""
from __future__ import annotations

import argparse
import sys
from math import atan2, degrees, sqrt
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Crop region for the mini-map ──────────────────────────────────────────
# (left, top, right, bottom) on a 2400×1080 frame.  Calibrated 2026-05-27
# via user-drawn red bbox on frame 0010 (see
# /tmp/0010_164950703_minimap_bbox.png).  Bbox extracted by detecting
# the red rectangle's edges in that annotation.
MINIMAP_CROP = (2003, 191, 2384, 375)        # 381×184

# Important: the mini-map shows MORE THAN the small translucent radar
# disc around the ship — the surrounding rectangular area is also part
# of the compass-fixed top-down view.  Land and ports can sit outside
# the radar disc but inside this navigable rectangle.
#
# Calibrated 2026-05-27 from frame 0010 with user feedback.
# Now that MINIMAP_CROP excludes the icon bar above, the nav-area can
# start near the top edge of the crop.  Bottom stops above the lat/lon
# text strip (which sits in the last ~20 px of the 230-tall crop).
# User decision 2026-05-27: NAV_AREA covers the full crop — every
# pixel in the user-drawn mini-map bbox is in scope.  The lat/lon
# text in the bottom-right may trigger small false-positive land
# pixels but is acceptable since it's a tiny, fixed region.
NAV_AREA = (0, 0, 381, 184)         # (l, t, r, b) — same as full crop
# The translucent radar disc is anchored on the ship icon and is much
# smaller than the nav area.  Kept around for visualisation only.
DISC_RADIUS = 75
DISC_FALLBACK_CENTER = (170, 125)


# ── Image-processing primitives ───────────────────────────────────────────


def _crop_minimap(frame: Image.Image) -> Image.Image:
    """Crop the mini-map area, scaling if frame size != 2400×1080."""
    w, h = frame.size
    cx0, cy0, cx1, cy1 = MINIMAP_CROP
    if (w, h) != (2400, 1080):
        sx, sy = w / 2400.0, h / 1080.0
        cx0, cy0 = int(cx0 * sx), int(cy0 * sy)
        cx1, cy1 = int(cx1 * sx), int(cy1 * sy)
    return frame.crop((cx0, cy0, cx1, cy1))


def _nav_area_mask(shape: tuple[int, int]) -> np.ndarray:
    """A boolean mask covering the navigable rectangle inside the
    mini-map crop — excludes chrome strip, lat/lon strip, and the right
    panel content."""
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    l, t, r, b = NAV_AREA
    mask[t:b, l:r] = True
    return mask


def _disc_mask(shape: tuple[int, int],
               center: tuple[float, float],
               radius: float = DISC_RADIUS) -> np.ndarray:
    """A circular boolean mask covering the radar disc region, centred
    on the supplied (x, y).  Used only for visual reference now — the
    nav-area rectangle is the real bounds."""
    h, w = shape
    cx, cy = center
    yy, xx = np.ogrid[:h, :w]
    return ((xx - cx) ** 2 + (yy - cy) ** 2) <= radius * radius


def _color_masks(rgb: np.ndarray) -> dict[str, np.ndarray]:
    """Return per-color masks computed from the RGB array."""
    hsv = np.array(Image.fromarray(rgb).convert("HSV"))
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    R, G, B = rgb[..., 0], rgb[..., 1], rgb[..., 2]

    # Ship icon — saturated green
    ship_green = (H > 60) & (H < 130) & (S > 100) & (V > 100)

    # Saturated yellow/orange — port_anchor OR pirate head, disambiguated
    # downstream by shape / size.  Color alone cannot tell them apart.
    yellow_any = ((H > 15) & (H < 50) & (S > 120) & (V > 120))

    # Very white pixels — merchant diamonds, known-port anchors, ???
    # markers, settlement name text.  These are pure-white desaturated
    # sprites.  Threshold V>220 is tight to avoid eating land pixels.
    spread = np.maximum(np.maximum(R, G), B).astype(np.int16) - \
             np.minimum(np.minimum(R, G), B).astype(np.int16)
    very_white = (V > 220) & (spread < 25)

    # Medium-bright desaturated pixels (V 185-220, S<30): these include
    # village-building roof tiles AND broad land patches.  Color alone
    # can't separate them — shape does.  Stored raw here; the sprite-
    # mask builder filters by connected-component size to keep only
    # small clusters (sprites) and reject big swaths (land).
    light_gray = (V > 185) & (V <= 220) & (spread < 30)

    # Saturated anything (debug overlay) — covers coloured sprites that
    # weren't otherwise caught (pink asterisks, etc.).
    saturated = S > 100

    return {
        "ship_green":    ship_green,
        "yellow_any":    yellow_any,
        "very_white":    very_white,
        "light_gray":    light_gray,
        "saturated":     saturated,
    }


def _connected_components(mask: np.ndarray) -> list[dict]:
    """Lightweight 8-connected component finder.  Returns one dict per
    blob with bbox, centroid, area.  Pure numpy — avoids the scipy
    dependency for this small workload."""
    visited = np.zeros_like(mask, dtype=bool)
    h, w = mask.shape
    blobs = []
    # BFS over each unvisited mask pixel.
    from collections import deque
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or visited[sy, sx]:
                continue
            q = deque([(sy, sx)])
            ys = []
            xs = []
            while q:
                y, x = q.popleft()
                if y < 0 or y >= h or x < 0 or x >= w:
                    continue
                if visited[y, x] or not mask[y, x]:
                    continue
                visited[y, x] = True
                ys.append(y); xs.append(x)
                q.extend([(y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1),
                          (y + 1, x + 1), (y - 1, x - 1),
                          (y + 1, x - 1), (y - 1, x + 1)])
            if len(ys) < 3:
                continue
            blobs.append({
                "area":     len(ys),
                "centroid": (float(np.mean(xs)), float(np.mean(ys))),
                "bbox":     (min(xs), min(ys), max(xs), max(ys)),
                "aspect":   (max(xs) - min(xs) + 1) / max(1, (max(ys) - min(ys) + 1)),
            })
    blobs.sort(key=lambda b: -b["area"])
    return blobs


def _land_mask_bright(gray: np.ndarray, disc: np.ndarray) -> np.ndarray:
    """Naive bright-pixel threshold; land is brighter than water."""
    return disc & (gray > 175)


def _land_mask_dark(gray: np.ndarray, disc: np.ndarray) -> np.ndarray:
    """Inverse hypothesis: land is DARKER than water (some regions)."""
    return disc & (gray < 110)


def _ship_centroid(ship_mask: np.ndarray) -> tuple[float, float] | None:
    ys, xs = np.where(ship_mask)
    if xs.size == 0:
        return None
    return float(xs.mean()), float(ys.mean())


def _bearing_axis_pca(mask: np.ndarray) -> float | None:
    """Estimate the principal axis (in image degrees, 0=right, 90=down)
    of a binary mask.  Returns None if too few pixels."""
    ys, xs = np.where(mask)
    if xs.size < 5:
        return None
    pts = np.column_stack([xs - xs.mean(), ys - ys.mean()]).astype(float)
    cov = np.cov(pts, rowvar=False)
    w, v = np.linalg.eigh(cov)
    principal = v[:, np.argmax(w)]
    # Image y grows downward; convert to math angle, then to compass.
    # Compass: 0 = up = north, increases clockwise.
    angle_image = degrees(atan2(principal[1], principal[0]))
    # principal axis is a line (180° ambiguity); normalize to [0, 180)
    return angle_image % 180


def _ship_heading(green_mask: np.ndarray) -> float | None:
    """Estimate ship heading from the green icon shape.

    Returns compass bearing in degrees (0=N, 90=E, 180=S, 270=W), or
    None if the icon is too small to analyse.

    Method:
        1. PCA on the green pixels gives the icon's principal axis
           (with 180° ambiguity).
        2. Project pixels onto the axis; the BOW is the END with
           FEWER pixels — the bow tip is a pointed wedge while the
           stern is wider with a flag/banner.
    """
    ys, xs = np.where(green_mask)
    if xs.size < 8:
        return None
    cx, cy = xs.mean(), ys.mean()
    pts = np.column_stack([xs - cx, ys - cy]).astype(float)
    cov = np.cov(pts, rowvar=False)
    eigvals, eigvecs = np.linalg.eigh(cov)
    principal = eigvecs[:, np.argmax(eigvals)]
    proj = pts @ principal
    # The side with FEWER pixels is the bow (sharper tip).
    n_pos = int((proj > 0).sum())
    n_neg = int((proj < 0).sum())
    bow_vec = principal if n_neg > n_pos else -principal
    # Image: x right, y down.  Compass: 0=up=N=-y, 90=E=+x.
    heading_rad = np.arctan2(bow_vec[0], -bow_vec[1])
    return float((np.degrees(heading_rad) + 360) % 360)


def _detect_sprites(
    masks: dict[str, np.ndarray],
    nav_mask: np.ndarray,
    ship_xy: tuple[float, float] | None,
) -> dict[str, list[dict]]:
    """Detect ports / villages / unvisited markers on the mini-map.

    Yellow anchor (unvisited port)  → from yellow_any mask, filtered by
                                      anchor-like size + aspect.
    White anchor (known port)       → from very_white mask, filtered.
    Building (village)              → also from very_white / grayscale
                                      shape — placeholder for now.

    Returns a dict of sprite-kind → list of blob records (centroid,
    area, bbox).  The ship icon's own centroid is filtered out by
    proximity to ship_xy.
    """
    def _filter_near_ship(blobs):
        if ship_xy is None:
            return blobs
        sx, sy = ship_xy
        out = []
        for b in blobs:
            bx, by = b["centroid"]
            if (bx - sx) ** 2 + (by - sy) ** 2 > 64:    # >8 px from ship centre
                out.append(b)
        return out

    yellow_blobs = _connected_components(masks["yellow_any"] & nav_mask)
    # Anchor sprites are roughly square, ~30-200 px area.  Pirate heads
    # may be similar; we don't fully disambiguate by size alone, but the
    # filter rejects single-pixel noise + huge blobs.
    yellow_filtered = [
        b for b in yellow_blobs
        if 25 < b["area"] < 300 and 0.5 < b["aspect"] < 2.0
    ]
    yellow_filtered = _filter_near_ship(yellow_filtered)

    white_blobs = _connected_components(masks["very_white"] & nav_mask)
    # White sprites: known-port anchors AND merchant diamonds AND
    # name/??? text.  Without shape matching we can't fully separate
    # them; report all candidates and let the caller decide.
    white_filtered = [b for b in white_blobs if 15 < b["area"] < 400]

    return {
        "yellow_unvisited": yellow_filtered,
        "white_sprites":    white_filtered,
    }


def _bearing_from_ship(ship_xy, target_xy, ship_heading_deg) -> tuple[float, float]:
    """Compute (compass_bearing, ship_relative_bearing) from ship to target.

    Both in degrees.  ship_relative_bearing is in [0, 360); 0 = dead
    ahead, 90 = starboard, 180 = astern, 270 = port.
    """
    dx = target_xy[0] - ship_xy[0]
    dy = -(target_xy[1] - ship_xy[1])     # flip so up=+y
    compass = (np.degrees(np.arctan2(dx, dy)) + 360) % 360
    if ship_heading_deg is None:
        return float(compass), float("nan")
    relative = (compass - ship_heading_deg + 360) % 360
    return float(compass), float(relative)


def _reachable(
    water_mask: np.ndarray,
    start_xy: tuple[float, float],
    target_xy: tuple[float, float],
    start_slack: int = 20,
    target_slack: int = 4,
) -> bool:
    """BFS over water pixels; True if any cell within `target_slack` of
    target_xy is reachable from start_xy.

    `start_slack` is generous because the ship's pixel is masked out as
    a sprite — we need to escape the ship icon (~10 px wide) AND any
    nearby sprite cluster (pirate heads sit close to the ship) to find
    a water-adjacent starting cell.

    `target_slack` is smaller — anchor sprites overlap their port by a
    few pixels, so 4 px is enough margin without making the check too
    permissive.
    """
    from collections import deque
    h, w = water_mask.shape
    sx, sy = int(round(start_xy[0])), int(round(start_xy[1]))
    tx, ty = int(round(target_xy[0])), int(round(target_xy[1]))
    if not (0 <= sx < w and 0 <= sy < h):
        return False
    if not (0 <= tx < w and 0 <= ty < h):
        return False
    if not water_mask[sy, sx]:
        # Start not in water — search outward in concentric rings.
        slack_found = False
        for r in range(1, start_slack + 1):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if abs(dy) != r and abs(dx) != r:
                        continue  # only the ring at radius r
                    ny, nx = sy + dy, sx + dx
                    if 0 <= ny < h and 0 <= nx < w and water_mask[ny, nx]:
                        sy, sx = ny, nx
                        slack_found = True
                        break
                if slack_found:
                    break
            if slack_found:
                break
        if not slack_found:
            return False
    visited = np.zeros_like(water_mask, dtype=bool)
    visited[sy, sx] = True
    q = deque([(sy, sx)])
    target_set = {(ty + dy, tx + dx)
                  for dy in range(-target_slack, target_slack + 1)
                  for dx in range(-target_slack, target_slack + 1)}
    while q:
        y, x = q.popleft()
        if (y, x) in target_set:
            return True
        for dy, dx in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
            ny, nx = y + dy, x + dx
            if (0 <= ny < h and 0 <= nx < w
                    and not visited[ny, nx]
                    and water_mask[ny, nx]):
                visited[ny, nx] = True
                q.append((ny, nx))
    return False


def _sector_land_fractions(
    land_mask: np.ndarray,
    ship_xy: tuple[float, float],
    disc_mask: np.ndarray,
    n_sectors: int = 8,
) -> list[tuple[float, float]]:
    """For each of n_sectors angular bins around ship_xy, compute
    (land_fraction, nearest_normalized_distance).

    Bearings are in image-frame compass coords (0 = up, +clockwise).
    Steering-relative bearings come later when we know ship heading.
    """
    h, w = land_mask.shape
    sx, sy = ship_xy
    yy, xx = np.indices(land_mask.shape)
    dx = xx - sx
    dy = -(yy - sy)        # flip so up = +y
    bearing = (np.degrees(np.arctan2(dx, dy)) + 360) % 360
    radius = np.sqrt(dx * dx + dy * dy)
    radius_norm = radius / radius[disc_mask].max() if disc_mask.any() else radius / radius.max()

    sector_width = 360 / n_sectors
    half = sector_width / 2
    out = []
    for i in range(n_sectors):
        centre = i * sector_width
        lower = (centre - half) % 360
        upper = (centre + half) % 360
        if lower < upper:
            in_sector = (bearing >= lower) & (bearing < upper)
        else:
            in_sector = (bearing >= lower) | (bearing < upper)
        in_sector &= disc_mask
        total = int(in_sector.sum())
        land_in_sector = int((in_sector & land_mask).sum())
        frac = land_in_sector / total if total > 0 else 0.0
        # Nearest land in this sector
        land_in_sec = in_sector & land_mask
        if land_in_sec.any():
            nearest = float(radius_norm[land_in_sec].min())
        else:
            nearest = None
        out.append((frac, nearest))
    return out


# ── Visualisation ─────────────────────────────────────────────────────────


def _composite_debug(
    mm: Image.Image,
    masks: dict[str, np.ndarray],
    land_bright: np.ndarray,
    land_dark: np.ndarray,
    ship_xy: tuple[float, float] | None,
    heading: float | None,          # bow-resolved heading (compass deg)
    heading_axis: float | None,     # PCA axis (image deg, 0=east)
    sectors: list[tuple[float, float]],
    yellow_blobs: list[dict],
    targets: list[dict],
    nav_area: np.ndarray,
    disc_mask: np.ndarray,
) -> Image.Image:
    """Build a single tall image stacking all the diagnostic panels."""
    w, h = mm.size
    panels = []

    def with_caption(img: Image.Image, caption: str, grid: bool = True) -> Image.Image:
        canvas = Image.new("RGB", (img.width, img.height + 28), (20, 20, 20))
        canvas.paste(img, (0, 28))
        d = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 16)
            font_tick = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 10)
        except Exception:
            font = ImageFont.load_default()
            font_tick = font
        d.text((6, 6), caption, fill="white", font=font)
        if grid:
            # X ticks every 50 px along the bottom; Y ticks every 50 px
            # down the left edge.  Tick marks drawn 4 px long, semi-
            # transparent yellow.  Labels in tiny font.
            yoff = 28
            for x in range(0, img.width, 50):
                d.line([(x, yoff), (x, yoff + 4)], fill=(255, 255, 0), width=1)
                d.text((x + 2, yoff + 5), str(x), fill=(220, 220, 0), font=font_tick)
            for y in range(0, img.height, 50):
                d.line([(0, yoff + y), (4, yoff + y)], fill=(255, 255, 0), width=1)
                d.text((6, yoff + y), str(y), fill=(220, 220, 0), font=font_tick)
        return canvas

    # 1. Raw mini-map crop with nav-area + small disc shown
    raw = mm.copy()
    d_raw = ImageDraw.Draw(raw)
    # Yellow rectangle = navigable area for land detection.
    l, t, r, b = NAV_AREA
    d_raw.rectangle([l, t, r - 1, b - 1], outline="yellow", width=2)
    # Cyan circle = radar disc (the small translucent overlay around the
    # ship); does NOT bound land detection.
    if ship_xy is not None:
        cx, cy = ship_xy
    else:
        cx, cy = DISC_FALLBACK_CENTER
    rad = DISC_RADIUS
    d_raw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad],
                  outline="cyan", width=1)
    panels.append(with_caption(raw, "raw — yellow=nav-area, cyan=radar disc"))

    # 2. Colour masks
    rgb = np.array(mm.convert("RGB"))
    for name, mask in masks.items():
        vis = rgb.copy()
        vis[~mask] = vis[~mask] // 3
        panels.append(with_caption(Image.fromarray(vis), f"mask: {name}"))

    # 3. Land mask hypotheses — bright vs dark, both shown
    for label, mask in [("land = BRIGHT", land_bright), ("land = DARK", land_dark)]:
        overlay = np.array(mm.convert("RGB"))
        overlay[mask] = [255, 80, 80]
        panels.append(with_caption(Image.fromarray(overlay),
                                   f"{label}  ({int(mask.sum())} px)"))

    # 4. Yellow blobs with size labels — helps see pirate-head vs anchor
    blob_img = mm.copy().convert("RGB")
    d_blob = ImageDraw.Draw(blob_img)
    try:
        font_b = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 11)
    except Exception:
        font_b = ImageFont.load_default()
    for i, b in enumerate(yellow_blobs[:8]):
        x0, y0, x1, y1 = b["bbox"]
        d_blob.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], outline="orange", width=1)
        d_blob.text((x0, max(0, y0 - 12)),
                    f"#{i} a={b['area']}",
                    fill="orange", font=font_b)
    panels.append(with_caption(blob_img,
                               f"yellow blobs ({len(yellow_blobs)} total)"))

    # 4. Sector overlay with sprite + heading annotations
    sec_img = mm.copy().convert("RGB")
    draw = ImageDraw.Draw(sec_img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 13)
    except Exception:
        font = ImageFont.load_default()
    if ship_xy is not None:
        sx, sy = ship_xy
        n = len(sectors)
        radius = 70
        for i, (frac, dist) in enumerate(sectors):
            angle = i * (360 / n) - 90          # 0=top
            ax = sx + radius * np.cos(np.radians(angle))
            ay = sy + radius * np.sin(np.radians(angle))
            colour = (255, int(255 * (1 - frac)), int(255 * (1 - frac)))
            draw.line([(sx, sy), (ax, ay)], fill=colour, width=2)
            draw.text((ax - 14, ay - 10), f"{frac:.2f}", fill="yellow", font=font)
        draw.ellipse([sx - 4, sy - 4, sx + 4, sy + 4], fill="lime")
        # Bow-resolved heading arrow (compass deg → image vector)
        if heading is not None:
            hrad = np.radians(heading)
            hx = sx + 60 * np.sin(hrad)
            hy = sy - 60 * np.cos(hrad)
            draw.line([(sx, sy), (hx, hy)], fill="lime", width=4)
            # Arrowhead
            draw.ellipse([hx - 4, hy - 4, hx + 4, hy + 4], fill="lime")
        # Old PCA axis (180° ambiguous) — dashed-ish cyan for reference
        if heading_axis is not None:
            hxr = sx + 40 * np.cos(np.radians(heading_axis))
            hyr = sy + 40 * np.sin(np.radians(heading_axis))
            draw.line([(sx, sy), (hxr, hyr)], fill="cyan", width=1)

        # Target sprites: draw circles + reachability + bearings
        for tgt in targets:
            tx, ty = tgt["centroid"]
            colour = "yellow" if tgt["kind"] == "yellow_unvisited" else "white"
            outline = "lime" if tgt["reachable"] else "red"
            r = 8
            draw.ellipse([tx - r, ty - r, tx + r, ty + r], outline=outline, width=2)
            draw.ellipse([tx - 2, ty - 2, tx + 2, ty + 2], fill=colour)
            label = f"{tgt['relative_bearing']:.0f}°"
            draw.text((tx + r + 2, ty - 8), label, fill=outline, font=font)
    panels.append(with_caption(sec_img,
                               "sectors + heading (lime arrow) + targets"))

    # Stack panels vertically.
    total_h = sum(p.height for p in panels) + 4 * (len(panels) - 1)
    out = Image.new("RGB", (w, total_h), (0, 0, 0))
    y = 0
    for p in panels:
        out.paste(p, (0, y))
        y += p.height + 4
    return out


# ── Main ──────────────────────────────────────────────────────────────────


def process_frame(frame_path: Path, out_path: Path) -> dict:
    """Process one frame; write debug PNG.  Returns numeric summary."""
    frame = Image.open(frame_path).convert("RGB")
    mm = _crop_minimap(frame)
    rgb = np.array(mm)
    gray = np.array(mm.convert("L"))

    masks = _color_masks(rgb)

    # Detect ship icon first; its centroid anchors the polar bearings.
    ship_xy = _ship_centroid(masks["ship_green"])
    heading_axis = _bearing_axis_pca(masks["ship_green"])
    heading = _ship_heading(masks["ship_green"])    # bow-resolved 0..360

    # Land detection runs over the full navigable rectangle, NOT just
    # the small radar disc — the surrounding compass-fixed map area
    # also shows land/water and we want it all.
    nav_area = _nav_area_mask(gray.shape)
    disc = _disc_mask(gray.shape,
                      ship_xy if ship_xy is not None else DISC_FALLBACK_CENTER)

    # Village-roof recovery: building roofs are light-gray (V ~195)
    # and slip past the strict very_white mask.  The same V range
    # covers huge land swaths, so we can't broaden the mask globally.
    # Strategy: label connected components in light_gray; keep only
    # components that (a) touch a very_white pixel AND (b) are small
    # enough to be a sprite rather than a land patch.  This catches the
    # whole village icon (windows are very_white, roof is light_gray
    # connected to them via the icon's edge) without sweeping in land.
    from scipy.ndimage import label, binary_dilation  # type: ignore
    seed = masks["very_white"]
    lg_labels, n_components = label(masks["light_gray"])
    # A component touches very_white iff any of its pixels is adjacent
    # to one.  Compute adjacency by dilating seed by 1 px.
    seed_neighbor = binary_dilation(seed, iterations=1)
    touched_labels = set(np.unique(lg_labels[seed_neighbor & (lg_labels > 0)]))
    village_recovery = np.zeros_like(masks["light_gray"])
    for lbl in touched_labels:
        comp_mask = (lg_labels == lbl)
        if comp_mask.sum() < 600:    # small enough = sprite, not land
            village_recovery |= comp_mask

    # When detecting land, exclude the ship icon, saturated sprites,
    # the bright/desaturated sprites (very_white) AND the light-gray
    # pixels we just recovered around them (village roofs etc.).
    sprite_mask = (masks["ship_green"]
                   | masks["yellow_any"]
                   | masks["very_white"]
                   | village_recovery
                   | masks["saturated"])

    # Confirmed 2026-05-27 (frame 0010): in the full nav-area the
    # canonical polarity is land=BRIGHT (white-ish coast pixels) vs
    # water=darker.  The "dark" variant is kept around as a regression
    # check for frames where the convention reverses (e.g. arctic or
    # heavily-fogged views).
    land_bright_full = _land_mask_bright(gray, nav_area & ~sprite_mask)
    land_dark_full   = _land_mask_dark(gray, nav_area & ~sprite_mask)
    land = land_bright_full

    # Analyse each yellow blob individually so we can later filter by
    # size/shape: a pirate head vs a port anchor have different
    # silhouettes even when they share the colour band.
    yellow_blobs = _connected_components(masks["yellow_any"] & disc)

    sectors = _sector_land_fractions(land, ship_xy or (gray.shape[1] / 2, gray.shape[0] / 2), nav_area)

    # Sprite detection (uses color masks + connected components).
    sprites = _detect_sprites(masks, nav_area, ship_xy)

    # Reachability check.  The translucent radar disc overlay washes
    # everything inside the disc to "bright", so the naive land mask
    # falsely marks the entire disc as land — the BFS would then start
    # surrounded by land and reach nothing.  Treat the disc interior as
    # always-navigable water; this is correct semantically (the bot is
    # already there) and keeps the BFS workable.
    water = (nav_area & ~sprite_mask & (~land | disc))
    targets_with_reach = []
    if ship_xy is not None:
        for kind, blobs in sprites.items():
            for b in blobs:
                bx, by = b["centroid"]
                compass, relative = _bearing_from_ship(ship_xy, (bx, by), heading)
                # Normalize distance by half-diagonal of the nav-area.
                hd = (nav_area.shape[0] ** 2 + nav_area.shape[1] ** 2) ** 0.5 / 2
                dist = ((bx - ship_xy[0]) ** 2 + (by - ship_xy[1]) ** 2) ** 0.5 / hd
                reach = _reachable(water, ship_xy, (bx, by))
                targets_with_reach.append({
                    "kind":             kind,
                    "centroid":         (bx, by),
                    "area":             b["area"],
                    "compass_bearing":  compass,
                    "relative_bearing": relative,
                    "distance_norm":    dist,
                    "reachable":        reach,
                })

    debug = _composite_debug(mm, masks, land_bright_full, land_dark_full,
                             ship_xy, heading, heading_axis, sectors,
                             yellow_blobs, targets_with_reach,
                             nav_area, disc)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    debug.save(out_path)

    return {
        "frame":         frame_path.name,
        "ship_xy":       ship_xy,
        "heading":       heading,
        "heading_axis":  heading_axis,
        "n_yellow":      int(masks["yellow_any"].sum()),
        "n_green":       int(masks["ship_green"].sum()),
        "n_white":       int(masks["very_white"].sum()),
        "land_bright_px": int(land_bright_full.sum()),
        "land_dark_px":   int(land_dark_full.sum()),
        "yellow_blobs":  yellow_blobs,
        "sectors":       sectors,
        "targets":       targets_with_reach,
        "out":           str(out_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", type=Path, default=None,
                    help="single frame to process")
    ap.add_argument("--session", default=None,
                    help="process N frames from this session")
    ap.add_argument("--limit", type=int, default=5,
                    help="when --session: how many frames to process")
    ap.add_argument("--out", type=Path, default=Path("/tmp/mm_debug.png"),
                    help="output PNG path (single frame mode)")
    args = ap.parse_args()

    if args.frame:
        summary = process_frame(args.frame, args.out)
        print(f"wrote {summary['out']}")
        print(f"  ship_xy:        {summary['ship_xy']}")
        print(f"  heading (bow):  {summary['heading']}")
        print(f"  heading axis:   {summary['heading_axis']}")
        print(f"  green px:       {summary['n_green']}")
        print(f"  yellow px:      {summary['n_yellow']}  ({len(summary['yellow_blobs'])} distinct blobs)")
        print(f"  white px:       {summary['n_white']}")
        print(f"  land BRIGHT px: {summary['land_bright_px']}")
        print(f"  land DARK   px: {summary['land_dark_px']}")
        if summary["targets"]:
            print(f"  targets ({len(summary['targets'])}):")
            for t in summary["targets"][:10]:
                rb = f"{t['relative_bearing']:.0f}°" if t['relative_bearing'] == t['relative_bearing'] else "n/a"
                print(f"    {t['kind']:18s}  bearing(rel)={rb:>5s}  dist={t['distance_norm']:.2f}  "
                      f"area={t['area']:>3d}  reachable={t['reachable']}")
        print(f"  sectors:")
        for i, (frac, dist) in enumerate(summary['sectors']):
            d = f"{dist:.2f}" if dist is not None else "—"
            print(f"    sector {i} ({i*45:3d}°): land_frac={frac:.2f} nearest={d}")
        return 0

    if args.session:
        session_dir = _PROJECT_ROOT / "data" / "sessions" / args.session / "frames"
        if not session_dir.exists():
            raise SystemExit(f"no such session: {session_dir}")
        frames = sorted(session_dir.glob("*.png"))[: args.limit]
        for i, f in enumerate(frames):
            out = Path(f"/tmp/mm_debug_{i:02d}.png")
            summary = process_frame(f, out)
            print(f"[{i}] {f.name}  ship={summary['ship_xy']}  "
                  f"heading={summary['heading']}  green={summary['n_green']}  "
                  f"yellow={summary['n_yellow']}  land={summary['land_px']}  -> {out}")
        return 0

    raise SystemExit("specify --frame or --session")


if __name__ == "__main__":
    sys.exit(main())
