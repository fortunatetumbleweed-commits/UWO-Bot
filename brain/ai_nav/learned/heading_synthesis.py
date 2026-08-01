"""Occlusion synthesis for the heading CNN training set.

Builds (crop, heading, visibility_fraction) examples from clean
sprites by applying realistic occlusion masks.  All labels are exact
because the ship's heading doesn't depend on which pixels are
visible — only the inputs change.

Occlusion strategies, all relative to the ship's current heading:

  none                — no occlusion (keep clean)
  front_half          — bow side hidden (the user's "stern only" case)
  back_half           — stern side hidden (the user's "front only" case)
  left_half/right_half — port/starboard side hidden
  center_band         — middle stripe hidden, both ends visible
                        (HUD-text occlusion case — t130-t132)
  random_blobs        — 1-3 random ellipses (organic shapes)
  random_polygons     — 1-3 random polygons (jigsaw-piece shapes)
  random_rect         — single random rectangle

The "_half" cases are direction-aware: at heading θ, the bow points
in (sin θ, -cos θ) direction in image coords, so front_half hides
that half-plane.

Confidence target = clip(visibility_fraction, 0.2, 1.0).  The 0.2
floor keeps the head from going completely dead; the model can still
contribute some signal even when only 20% of the ship is visible.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


# ── Canonical ship silhouette (bow up, north-pointing) ──────────────
# Built once from `data/reference/ship_template_raw.png` by extracting
# just the ship-green alpha (yellow sonar stripped) and centering in
# 80×80.  Used as the ground-truth mask for reconstruction training:
# `ship_mask = rotate(canonical, target_heading)`.  Every training
# sample gets the same shape rotated to its target — clean, consistent,
# no per-sprite variance.

def _load_canonical_ship_mask() -> np.ndarray:
    """Return the 80×80 bow-up ship silhouette as a bool array."""
    tpl_path = (Path(__file__).resolve().parents[3]
                / "data/reference/ship_template_raw.png")
    if not tpl_path.exists():
        # Fallback: a small central rectangle so tests don't explode.
        m = np.zeros((80, 80), dtype=bool)
        m[30:55, 37:43] = True
        return m
    tpl = np.asarray(Image.open(tpl_path).convert("RGBA"))
    R = tpl[..., 0].astype(np.int16)
    G = tpl[..., 1].astype(np.int16)
    B = tpl[..., 2].astype(np.int16)
    a = tpl[..., 3] > 200
    is_ship = (G > 140) & (G - R > 30) & (G - B > 30)
    is_yellow = (R > 180) & (G > 150) & (B < 130) & (np.abs(R - G) < 40)
    m = a & is_ship & ~is_yellow
    ys, xs = np.where(m)
    if len(ys) == 0:
        return np.zeros((80, 80), dtype=bool)
    cy, cx = int(round(ys.mean())), int(round(xs.mean()))
    H, W = m.shape
    y0 = max(0, cy - 40); x0 = max(0, cx - 40)
    y1 = min(H, y0 + 80); x1 = min(W, x0 + 80)
    src = m[y0:y1, x0:x1]
    canonical = np.zeros((80, 80), dtype=bool)
    ty = (80 - src.shape[0]) // 2
    tx = (80 - src.shape[1]) // 2
    canonical[ty:ty + src.shape[0], tx:tx + src.shape[1]] = src
    return canonical


CANONICAL_SHIP_MASK = _load_canonical_ship_mask()


def _load_canonical_ship_rgba() -> np.ndarray:
    """Return the canonical ship as an 80×80 RGBA sprite ready to feed
    into `synthesize` — RGB pixels come from the saved template's
    actual ship pixels, alpha = the yellow-stripped ship mask.

    Used for training the reconstruction CNN with a canonical-only
    dataset: every training sample uses this same sprite (with random
    heading + background + occluder + color jitter), so the input and
    GT are always the same shape.  Eliminates the ~55% partial-ship
    problem in the extracted sprite pool.
    """
    tpl_path = (Path(__file__).resolve().parents[3]
                / "data/reference/ship_template_raw.png")
    if not tpl_path.exists():
        return np.zeros((80, 80, 4), dtype=np.uint8)
    tpl = np.asarray(Image.open(tpl_path).convert("RGBA"))
    # Locate ship centroid to crop an 80×80 region centered on it.
    R = tpl[..., 0].astype(np.int16)
    G = tpl[..., 1].astype(np.int16)
    B = tpl[..., 2].astype(np.int16)
    a = tpl[..., 3] > 200
    is_ship = (G > 140) & (G - R > 30) & (G - B > 30)
    is_yellow = (R > 180) & (G > 150) & (B < 130) & (np.abs(R - G) < 40)
    ship_mask = a & is_ship & ~is_yellow
    ys, xs = np.where(ship_mask)
    if len(ys) == 0:
        return np.zeros((80, 80, 4), dtype=np.uint8)
    cy, cx = int(round(ys.mean())), int(round(xs.mean()))
    H, W, _ = tpl.shape
    y0 = max(0, cy - 40); x0 = max(0, cx - 40)
    y1 = min(H, y0 + 80); x1 = min(W, x0 + 80)
    src_rgb = tpl[y0:y1, x0:x1, :3]
    src_mask = ship_mask[y0:y1, x0:x1]
    canonical = np.zeros((80, 80, 4), dtype=np.uint8)
    ty = (80 - src_rgb.shape[0]) // 2
    tx = (80 - src_rgb.shape[1]) // 2
    canonical[ty:ty + src_rgb.shape[0], tx:tx + src_rgb.shape[1], :3] = src_rgb
    canonical[ty:ty + src_mask.shape[0], tx:tx + src_mask.shape[1], 3] = (
        src_mask.astype(np.uint8) * 255)
    return canonical


CANONICAL_SHIP_RGBA = _load_canonical_ship_rgba()


def jitter_canonical_rgba(seed: int | None = None) -> np.ndarray:
    """Return a color-jittered copy of the canonical RGBA sprite.

    Jitter is constrained to stay STRICTLY green — brightness scaled
    uniformly across channels (no per-channel drift), tint kept tight
    (±5), and a hard post-condition of G > R + 20 & G > B + 20
    enforced by re-clamping.  This prevents the ship from ever
    drifting toward yellow-ish tones during training, so at inference
    the model doesn't associate yellow-adjacent pixels with the ship.
    Alpha unchanged.
    """
    if seed is not None:
        rng = np.random.default_rng(seed)
    else:
        rng = np.random.default_rng()
    out = CANONICAL_SHIP_RGBA.copy()
    ship_pixels = out[..., 3] > 200
    if not ship_pixels.any():
        return out
    # Global brightness scale (single scalar → all channels move
    # together, no color drift).  Small per-channel tint kept tight.
    brightness = float(rng.uniform(0.85, 1.15))
    tint = rng.integers(-5, 6, size=3).astype(np.int16)
    rgb = out[..., :3].astype(np.int16)
    rgb = np.clip(rgb * brightness + tint, 0, 255).astype(np.int16)
    # Post-condition: enforce green dominance.  If R or B is too
    # close to G, pull them back down.
    R, G, B = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    R = np.minimum(R, G - 20)
    B = np.minimum(B, G - 20)
    rgb = np.clip(np.stack([R, G, B], axis=-1), 0, 255).astype(np.uint8)
    out[..., :3] = np.where(ship_pixels[..., None], rgb, out[..., :3])
    return out


def _rotate_canonical(target_heading_deg: float) -> np.ndarray:
    """Rotate the bow-up canonical ship mask to the target compass
    bearing.  PIL rotate uses CCW-positive; compass is CW-positive,
    so we negate the angle."""
    img = Image.fromarray(
        (CANONICAL_SHIP_MASK.astype(np.uint8) * 255), mode="L")
    rot = img.rotate(-target_heading_deg, resample=Image.BILINEAR)
    return np.asarray(rot) > 128


@dataclass
class SynthExample:
    image: Image.Image          # 80×80 RGB
    heading_deg: float          # target heading in [0, 360)
    visibility_frac: float      # in [0, 1]; confidence target
    # v15 shape-prior targets — the FULL ship silhouette (before
    # occlusion is applied) and which end of the ship's PCA axis
    # contains the bow.  Both fields are optional; consumers that
    # don't need them (v10-v14 training) can ignore them.
    ship_mask: np.ndarray | None = None    # 80×80 bool — clean silhouette
    bow_end: int | None = None             # 1 = bow at +PCA end, 0 = -PCA


def _ship_half_mask(shape: tuple[int, int], cx: int, cy: int,
                    heading_deg: float, side: str,
                    ship_extent_px: int = 20) -> np.ndarray:
    """Compact rectangular occluder covering the front/back/side
    portion of the ship — sized like a real game pirate NPC that
    happens to be adjacent to and partially covering that side.

    `side` ∈ {'front', 'back', 'left', 'right'}.
    `ship_extent_px` = approximate ship half-length in the crop
    (used to position the occluder centre near the ship's front or
    back tip instead of at the far edge of the image).

    Prior implementation used a half-PLANE (half the entire image),
    which drew a 40×80 rectangle when the color-fill was drawn over
    the mask itself.  Real half-side occluders are compact ~15×20
    NPC-shaped blobs, not half the frame.
    """
    H, W = shape
    th = math.radians(heading_deg)
    fx, fy = math.sin(th), -math.cos(th)
    rx, ry = -fy, fx
    if side == "front":
        ox, oy = cx + fx * ship_extent_px * 0.5, cy + fy * ship_extent_px * 0.5
    elif side == "back":
        ox, oy = cx - fx * ship_extent_px * 0.5, cy - fy * ship_extent_px * 0.5
    elif side == "right":
        ox, oy = cx + rx * ship_extent_px * 0.5, cy + ry * ship_extent_px * 0.5
    else:  # left
        ox, oy = cx - rx * ship_extent_px * 0.5, cy - ry * ship_extent_px * 0.5
    # Compact NPC-sized rectangle at the target position.
    yy, xx = np.mgrid[0:H, 0:W]
    half_w = random.randint(9, 13)
    half_h = random.randint(9, 13)
    return (np.abs(xx - ox) < half_w) & (np.abs(yy - oy) < half_h)


def _center_band_mask(shape: tuple[int, int], cx: int, cy: int,
                      heading_deg: float, half_width: int) -> np.ndarray:
    """Village-text overlay: a BOUNDED rectangle centered on the ship,
    horizontal (screen-aligned), roughly the size of village name text.
    Prior implementation used an unbounded strip perpendicular to the
    ship axis, which was hidden by the old ship-only occluder fill but
    dominated the whole image after the 2026-07-02 fill fix.  Real
    village text is a small rectangle, not a strip across the frame.

    `half_width` here controls the rectangle's HEIGHT (short axis).
    The width (long axis) is drawn ~2.5× the half_width to look
    text-shaped.
    """
    H, W = shape
    half_h = half_width               # short axis (~vertical extent)
    half_w = int(half_width * 2.5)    # long axis (~horizontal extent)
    yy, xx = np.mgrid[0:H, 0:W]
    return (np.abs(xx - cx) < half_w) & (np.abs(yy - cy) < half_h)


def _random_blob_mask(shape: tuple[int, int],
                      ship_center: tuple[int, int] | None = None
                      ) -> np.ndarray:
    """1-2 compact ellipses placed near the ship — mimics a pirate NPC
    or small overlay adjacent to the bot's ship.  Constrained radii
    (5-11 px) so a single blob doesn't dominate the frame.  When
    `ship_center` is provided, blobs are positioned within a small
    radius of it (real occluders are near the ship, not on empty water).
    """
    H, W = shape
    cx0, cy0 = ship_center if ship_center is not None else (W//2, H//2)
    img = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(img)
    for _ in range(random.randint(1, 2)):
        rx = random.randint(5, 11)
        ry = random.randint(5, 11)
        # Placement radius: within ~20 px of the ship centroid.
        pr = random.randint(0, 20)
        pth = random.uniform(0, 2*math.pi)
        cx = int(cx0 + pr * math.cos(pth))
        cy = int(cy0 + pr * math.sin(pth))
        cx = max(rx, min(W-rx, cx))
        cy = max(ry, min(H-ry, cy))
        draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    return np.asarray(img) > 128


def _random_polygon_mask(shape: tuple[int, int],
                         ship_center: tuple[int, int] | None = None
                         ) -> np.ndarray:
    """1 small polygon near the ship — text-glyph-like irregular shape."""
    H, W = shape
    cx0, cy0 = ship_center if ship_center is not None else (W//2, H//2)
    img = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(img)
    pr = random.randint(0, 18)
    pth = random.uniform(0, 2*math.pi)
    cx = int(cx0 + pr * math.cos(pth))
    cy = int(cy0 + pr * math.sin(pth))
    n_vertices = random.randint(4, 7)
    radius = random.randint(7, 13)
    pts = []
    for i in range(n_vertices):
        a = 2 * math.pi * i / n_vertices + random.uniform(-0.4, 0.4)
        r = radius * random.uniform(0.5, 1.2)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.polygon(pts, fill=255)
    return np.asarray(img) > 128


def _random_rect_mask(shape: tuple[int, int],
                      ship_center: tuple[int, int] | None = None
                      ) -> np.ndarray:
    """Compact rectangle near ship — village text / HUD digit strip."""
    H, W = shape
    cx0, cy0 = ship_center if ship_center is not None else (W//2, H//2)
    rw = random.randint(14, 28)      # width matches text glyph width
    rh = random.randint(6, 12)       # height matches text glyph height
    pr = random.randint(0, 18)
    pth = random.uniform(0, 2*math.pi)
    cx = int(cx0 + pr * math.cos(pth))
    cy = int(cy0 + pr * math.sin(pth))
    x0 = max(0, min(W - rw, cx - rw // 2))
    y0 = max(0, min(H - rh, cy - rh // 2))
    m = np.zeros((H, W), dtype=bool)
    m[y0:y0 + rh, x0:x0 + rw] = True
    return m


def _ship_keep_fraction_mask(shape: tuple[int, int],
                             ship_mask: np.ndarray,
                             cx: int, cy: int, heading_deg: float,
                             keep_end: str, keep_fraction: float
                             ) -> np.ndarray:
    """Rectangular NPC-shaped occluder covering the ship's back or
    front side, leaving `keep_fraction` of the OPPOSITE end intact.

    The rectangle's own axis is jittered ±20° from the ship's axis
    (so the CNN can't shortcut on "occluder axis = ship axis"), and
    its perpendicular position is jittered a few pixels off-centre.
    The kept endpoint is protected by a hard "keep zone" subtraction
    against the ship's along-axis threshold — no matter how the
    rectangle is rotated/shifted, the target endpoint stays visible.
    """
    H, W = shape
    th = math.radians(heading_deg)
    fx, fy = math.sin(th), -math.cos(th)     # ship-frame forward
    px, py = -fy, fx                         # ship-frame perpendicular
    yy, xx = np.mgrid[0:H, 0:W]
    dx = xx - cx
    dy = yy - cy
    along_ship = dx * fx + dy * fy
    perp_ship  = dx * px + dy * py
    ship_along = along_ship[ship_mask]
    ship_perp  = perp_ship[ship_mask]
    if ship_along.size == 0:
        return np.zeros((H, W), dtype=bool)
    a_min, a_max = float(ship_along.min()), float(ship_along.max())
    p_max = float(np.abs(ship_perp).max())
    length = a_max - a_min
    # ── occluder-frame axis: rotate ±20° from ship axis + shift a few
    #    px perpendicular AND ±3-5 px along the axis, so the rectangle
    #    sits obliquely AND asymmetrically on the ship.
    delta_deg = random.uniform(-20.0, 20.0)
    th_occ = math.radians(heading_deg + delta_deg)
    ox, oy = math.sin(th_occ), -math.cos(th_occ)
    pxo, pyo = -oy, ox
    perp_offset = random.uniform(-3.0, 3.0)
    along_offset = random.uniform(-4.0, 4.0)
    # Position rectangle centre on the OCCLUDE side of the ship,
    # halfway between the stern (or bow) and the keep threshold,
    # plus random along-axis jitter.
    if keep_end == "front":
        c_along = ((a_min + a_max - keep_fraction * length) / 2
                   + along_offset)
    else:
        c_along = ((a_min + keep_fraction * length + a_max) / 2
                   + along_offset)
    rect_cx = cx + c_along * fx + perp_offset * (-fy)
    rect_cy = cy + c_along * fy + perp_offset * fx
    along_occ = (xx - rect_cx) * ox + (yy - rect_cy) * oy
    perp_occ  = (xx - rect_cx) * pxo + (yy - rect_cy) * pyo
    half_along = (length * (1 - keep_fraction) / 2 +
                  random.uniform(1, 3))
    half_perp  = max(p_max * 1.3, 8.0) * random.uniform(0.9, 1.15)
    rect_mask = ((np.abs(along_occ) < half_along)
                 & (np.abs(perp_occ) < half_perp))
    # ── Keep-zone protection: force the target endpoint to remain
    #    visible regardless of how the rectangle rotated/shifted.
    if keep_end == "front":
        keep_threshold = a_max - keep_fraction * length
        keep_zone = along_ship > keep_threshold
    else:
        keep_threshold = a_min + keep_fraction * length
        keep_zone = along_ship < keep_threshold
    return rect_mask & ~keep_zone


# Strategy mix.  Tuned roughly: a chunk of clean (so the model sees
# unambiguous gold), plenty of direction-aware halves (what t141
# needs), and plenty of irregular shapes (so it generalizes beyond
# the half-plane cases).  keep_front_/keep_back_ (third, quarter)
# preserve a precise fraction of the ship's front or back end (with
# the full bow tip / stern end intact) and occlude the rest of the
# body — trains the model to anchor direction on a single endpoint
# under heavy occlusion (production failure modes t83/t84/t556/t557).
def _pirate_boss_mask(shape: tuple[int, int], ship_mask: np.ndarray,
                      cx: int, cy: int, heading_deg: float,
                      keep_end: str) -> np.ndarray:
    """Large yellow ELLIPSE covering ~80% of the ship, positioned so
    the FULL terminating line at the opposite endpoint stays intact.

    Ellipse orientation is jittered ±25° from the ship's axis + a
    few px of perpendicular offset, so the CNN can't shortcut on
    "occluder axis aligns with ship axis".  The kept endpoint is
    protected by a hard keep-zone subtraction — no matter how the
    ellipse tilts, the target end stays visible.
    """
    H, W = shape
    th = math.radians(heading_deg)
    fx, fy = math.sin(th), -math.cos(th)
    px, py = -fy, fx
    yy, xx = np.mgrid[0:H, 0:W]
    dx = xx - cx
    dy = yy - cy
    along_ship = dx * fx + dy * fy
    perp_ship  = dx * px + dy * py
    ship_along = along_ship[ship_mask]
    ship_perp  = perp_ship[ship_mask]
    if ship_along.size == 0:
        return np.zeros((H, W), dtype=bool)
    a_min, a_max = float(ship_along.min()), float(ship_along.max())
    p_max = float(np.abs(ship_perp).max())
    length = a_max - a_min
    # ── Occluder frame: ±25° rotation + perp AND along-axis jitter.
    delta_deg = random.uniform(-25.0, 25.0)
    th_occ = math.radians(heading_deg + delta_deg)
    ox, oy = math.sin(th_occ), -math.cos(th_occ)
    pxo, pyo = -oy, ox
    perp_offset = random.uniform(-4.0, 4.0)
    along_offset = random.uniform(-4.0, 4.0)
    keep_frac = random.uniform(0.15, 0.22)
    # Ellipse centre halfway between stern (or bow) and keep threshold,
    # plus random along-axis jitter so the ellipse isn't perfectly
    # centred on the ship's midline.
    if keep_end == "front":
        c_along = (a_min + a_max - keep_frac * length) / 2 + along_offset
    else:
        c_along = (a_max + a_min + keep_frac * length) / 2 + along_offset
    e_cx = cx + c_along * fx + perp_offset * (-fy)
    e_cy = cy + c_along * fy + perp_offset * fx
    along_occ = (xx - e_cx) * ox + (yy - e_cy) * oy
    perp_occ  = (xx - e_cx) * pxo + (yy - e_cy) * pyo
    r_along = (1 - keep_frac) * length / 2 * random.uniform(1.0, 1.15)
    r_perp  = max(p_max * 1.5, 12.0) * random.uniform(0.95, 1.15)
    ellipse = ((along_occ ** 2) / max(r_along ** 2, 1) +
               (perp_occ ** 2) / max(r_perp ** 2, 1)) < 1.0
    # ── Keep-zone protection along ship axis (not occluder axis).
    if keep_end == "front":
        keep_zone = along_ship > (a_max - keep_frac * length)
    else:
        keep_zone = along_ship < (a_min + keep_frac * length)
    return ellipse & ~keep_zone


def _text_overlay_mask(shape: tuple[int, int], ship_mask: np.ndarray,
                       cx: int, cy: int) -> np.ndarray:
    """SCREEN-HORIZONTAL text label rendered from a real font, sized
    to match in-game village-name overlays.

    Sizing (recalibrated from t556 "Bari Village" 2026-07-02): each
    character height ≈ full ship height (roughly 22-28 px cap
    height in the 80×80 crop, so a bit larger than the ship itself).
    Character width ≈ ship width, so a 3-6 char label spans 3-5
    ship widths and extends well past the ship on both sides.  Font
    is bold sans-serif with dark outline — but for training-mask
    purposes only the letter silhouette matters.

    Real letters (via PIL font) give natural gaps inside letters
    ('o', 'a', 'e' loops) and between them, so ship pixels leak
    through the same way they do in-game.  Screen-horizontal
    orientation regardless of ship heading — text reads left-to-right
    while the ship rotates through it.
    """
    H, W = shape
    from PIL import ImageFont
    import string
    # Try a few font paths — bold sans-serif preferred to match the
    # in-game village-label font.  Fall back to any available font if
    # bold isn't present.
    for fp in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
               "/System/Library/Fonts/Helvetica.ttc",
               "/System/Library/Fonts/Supplemental/Arial.ttf",
               "/System/Library/Fonts/Menlo.ttc"):
        try:
            font = ImageFont.truetype(fp, size=random.randint(22, 28))
            break
        except (OSError, IOError):
            font = None
    if font is None:
        yy, xx = np.mgrid[0:H, 0:W]
        band_half_h = random.randint(10, 14)
        band_half_w = random.randint(25, 35)
        return ((np.abs(yy - cy) < band_half_h)
                & (np.abs(xx - cx) < band_half_w))
    # Mixed-case letters — real village names ("Bari Village",
    # "Nubia") use title case.  3-6 chars fits inside 80 px width
    # when each char is ship-width sized.
    n_chars = random.randint(3, 6)
    text = (random.choice(string.ascii_uppercase)
            + "".join(random.choice(string.ascii_lowercase)
                      for _ in range(n_chars - 1)))
    # Render to an alpha canvas, then convert to boolean mask
    tmp = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(tmp)
    # Measure text extent so we can centre it on the ship centroid
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x0 = cx - tw // 2 - bbox[0]
        y0 = cy - th // 2 - bbox[1]
    except AttributeError:                          # older Pillow
        x0 = cx - font.getsize(text)[0] // 2
        y0 = cy - font.getsize(text)[1] // 2
    draw.text((x0, y0), text, fill=255, font=font)
    return np.asarray(tmp) > 128


_STRATEGIES: list[tuple[str, float]] = [
    ("none",                0.16),
    ("front_half",          0.06),
    ("back_half",           0.06),
    ("left_half",           0.05),
    ("right_half",          0.05),
    ("center_band",         0.06),
    ("random_blobs",        0.11),
    ("random_polygons",     0.11),
    ("random_rect",         0.07),
    ("keep_front_third",    0.05),
    ("keep_back_third",     0.05),
    ("keep_front_quarter",  0.05),
    ("keep_back_quarter",   0.05),
    ("text_overlay",        0.07),   # horizontal glyph band (white)
    # pirate_boss_bow/stern removed — yellow blob strategies were
    # teaching the model to associate yellow with ship-adjacent
    # contexts, which caused mask puffiness on real frames with
    # sonar fan / pirate boss.  Deferred: use sonar as a positive
    # direction signal in a separate training run.
]


def _sample_strategy() -> str:
    r = random.random()
    cum = 0.0
    for name, p in _STRATEGIES:
        cum += p
        if r < cum:
            return name
    return _STRATEGIES[-1][0]


def _occluder_fill(crop: np.ndarray, mask: np.ndarray,
                   ship_mask: np.ndarray,
                   force_color: np.ndarray | None = None) -> np.ndarray:
    """Draw the occluder over its OWN mask shape, not just the ship-
    intersecting pixels.  Real game occluders (pirate sprites,
    village text) have their own distinct shapes — drawing only over
    the ship-boundary would trace the ship's silhouette exactly,
    letting the CNN learn a spurious "color-boundary = ship outline"
    feature that doesn't exist in real game frames.

    The occluder color is sampled from a palette that approximates
    the real game occluders observed in voyages:
      - pirate-sprite skin tone (peach/beige)
      - village-text white
      - HUD digit white
      - generic UI overlay gray
    """
    out = crop.copy()
    # We only need to draw the occluder where it *overlaps* the crop
    # (any of its pixels visible).  Draw over the occluder mask as a
    # whole — its own shape — so the model sees a shape that isn't
    # the ship outline.  We still limit drawing to the occluder's
    # intersection with a small halo around the ship, since occluders
    # far from the ship are irrelevant and would just add background
    # noise.
    from scipy.ndimage import binary_dilation
    ship_halo = binary_dilation(ship_mask, iterations=8)
    draw_area = mask & ship_halo
    if not draw_area.any():
        return out
    if force_color is not None:
        color = force_color
    else:
        palette = [
            np.array([240, 200, 150], dtype=np.uint8),  # pirate skin
            np.array([255, 255, 255], dtype=np.uint8),  # text white
            np.array([200, 200, 200], dtype=np.uint8),  # UI gray
            np.array([255, 230, 100], dtype=np.uint8),  # pirate yellow
            np.array([180, 150, 100], dtype=np.uint8),  # text shadow
        ]
        color = palette[random.randrange(len(palette))]
    # Small Gaussian jitter so the fill isn't a solid block
    noise = np.random.randint(-15, 16, size=(*out.shape[:2], 3),
                              dtype=np.int16)
    fill = np.clip(color[None, None, :].astype(np.int16) + noise,
                   0, 255).astype(np.uint8)
    out[draw_area] = fill[draw_area]
    return out


def _bow_end_from_mask_and_heading(mask: np.ndarray,
                                   heading_deg: float) -> int:
    """Given a ship silhouette mask and its true heading, return which
    end of the mask's PCA principal axis is the bow (1 = positive end,
    0 = negative end).

    Used to derive the ground-truth label for the v15 bow-end head at
    training time.  We know the true heading (label) and the true mask
    (from the clean sprite before occlusion); the model at inference
    only sees an occluded image.
    """
    ys, xs = np.where(mask)
    if len(ys) < 5:
        return 1
    cy = ys.mean()
    cx = xs.mean()
    dx = xs - cx
    dy = ys - cy
    # Weighted covariance (unit weights since mask is binary).
    sxx = float((dx * dx).mean())
    syy = float((dy * dy).mean())
    sxy = float((dx * dy).mean())
    # Principal-axis angle: theta = 0.5 * atan2(2*sxy, sxx - syy).
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    # +PCA direction in compass bearing (0° = up, clockwise).  In image
    # coords x goes right, y goes down.  A vector along +theta axis is
    # (cos θ, sin θ) → convert to compass: bearing = atan2(cos θ, -sin θ)
    # then wrap.  Simpler: heading_rad matches (sin, -cos) direction in
    # image; the +PCA direction as compass = 90° - deg(θ), matches
    # `mask_pca_angle` convention in heading_cnn.
    axis_bearing = (90.0 - math.degrees(theta)) % 360.0
    # Bow is at the +PCA end if heading points same way, else -PCA end.
    delta = (heading_deg - axis_bearing + 540.0) % 360.0 - 180.0
    return 1 if abs(delta) < 90.0 else 0


def _is_water_heavy(bg_rgb: np.ndarray, min_frac: float = 0.6) -> bool:
    """Return True if at least `min_frac` of pixels look like water.

    Mini-map water tends to be blue-ish and darker than land.  Rule:
    blue channel > red channel AND blue channel > green channel by
    at least a small margin.  Rough but works well against the
    green-heavy shore/river-bank patches that leaked into the
    backgrounds/ directory.
    """
    R = bg_rgb[..., 0].astype(np.int16)
    G = bg_rgb[..., 1].astype(np.int16)
    B = bg_rgb[..., 2].astype(np.int16)
    water = (B > R) & (B > G)
    return water.mean() >= min_frac


def synthesize(sprite_rgba: np.ndarray,
               source_heading_deg: float,
               background_rgb: np.ndarray,
               rotation_deg: float | None = None,
               strategy: str | None = None
               ) -> SynthExample:
    """Composite a ship-only RGBA sprite over a real minimap
    background, rotate to a target heading, apply occlusion.

    `sprite_rgba` is the 80×80 RGBA crop where the alpha channel is
    1 only on the ship's hull pixels (everything else — yellow
    sonar, water, shore, NPCs — is transparent).
    `background_rgb` is an 80×80 RGB patch sampled from a non-ship
    region of a real minimap frame.  The composite is what the
    model will see at inference time after we mask the live crop
    to ship-only.
    """
    if rotation_deg is None:
        rotation_deg = random.uniform(0.0, 360.0)
    if strategy is None:
        strategy = _sample_strategy()

    H, W = sprite_rgba.shape[:2]
    target_heading = (source_heading_deg + rotation_deg) % 360.0

    # Rotate the sprite (RGBA) FIRST so half-plane masks align with
    # the target heading.  Use NEAREST-neighbour for the alpha channel
    # so partially-transparent edge pixels don't blend the yellow-tinted
    # sonar / bow highlights into the surrounding water and produce
    # light-green artifacts in the halo (2026-07-02 issue: user saw
    # "light green bands" in rows 5 and 8 — those were soft-yellow
    # edges bleeding through bilinear rotation).  Strict alpha
    # threshold (>200) then rejects any remaining half-transparent
    # bleed at the ship's silhouette boundary.
    pil_sprite = Image.fromarray(sprite_rgba, "RGBA").rotate(
        -rotation_deg, resample=Image.NEAREST, expand=False,
        fillcolor=(0, 0, 0, 0))
    rot_sprite = np.asarray(pil_sprite)
    rot_alpha = rot_sprite[..., 3] > 200                # strict ship mask
    rot_rgb_ship = rot_sprite[..., :3]

    # Background stays in screen frame (no rotation).  Prior code
    # rotated the bg by the same angle so "ship-in-channel" geometries
    # would follow the ship, but the rotation left triangular corner
    # gaps filled with a dark blue that showed up as visible horizontal
    # dark bands at certain heading buckets (2026-07-02 issue).  Keeping
    # the bg static means each cell shows a pure water backdrop with
    # only the ship rotating — matches the model's inference-time input,
    # where the mini-map crop is always screen-aligned.
    bg = background_rgb
    if bg.shape[:2] != (H, W):
        bg = np.asarray(Image.fromarray(bg).resize((W, H)))

    # Composite: ship pixels over background.  Use alpha as a binary
    # mask (the rotation produced soft edges; threshold for clean
    # boundary).
    #
    # Water halo: fill a small radius around the ship with a water
    # color BEFORE compositing the ship.  Prevents "ship on land"
    # artifacts when the background patch happens to contain land
    # pixels adjacent to where the ship sits (real game ships are
    # always on water).
    # Water halo: a CIRCULAR disk centred on the ship's centroid,
    # not a dilation of the ship shape.  A dilation follows the ship's
    # outline, which at elongated headings makes the halo look like a
    # narrow stretched band — visually confusing.  A circle stays a
    # circle regardless of the ship's orientation.
    ys_a, xs_a = np.where(rot_alpha)
    if len(ys_a) > 0:
        ship_cy_h = int(ys_a.mean())
        ship_cx_h = int(xs_a.mean())
    else:
        ship_cy_h, ship_cx_h = H // 2, W // 2
    yy_h, xx_h = np.mgrid[0:H, 0:W]
    disc = ((yy_h - ship_cy_h)**2 + (xx_h - ship_cx_h)**2) < 22**2
    water_halo = disc & ~rot_alpha
    composite = bg.copy()
    if water_halo.any():
        R = bg[..., 0].astype(np.int16)
        G = bg[..., 1].astype(np.int16)
        B = bg[..., 2].astype(np.int16)
        is_water = (B > R + 5) & (B > G + 5)
        water_pool = bg[is_water]
        n_halo = int(water_halo.sum())
        if len(water_pool) > 30:
            # Random-sample from the actual water pixel pool for each
            # halo pixel.  Result mixes the bg's water tones naturally.
            idx = np.random.randint(0, len(water_pool), size=n_halo)
            composite[water_halo] = water_pool[idx]
        else:
            # Fallback: canonical mini-map water colour with jitter.
            water_color = np.array([50, 75, 105], dtype=np.uint8)
            noise = np.random.randint(-10, 11, size=(n_halo, 3),
                                      dtype=np.int16)
            fill = np.clip(
                water_color[None, :].astype(np.int16) + noise, 0, 255
            ).astype(np.uint8)
            composite[water_halo] = fill
    composite[rot_alpha] = rot_rgb_ship[rot_alpha]
    rot_rgb = composite
    rot_mask = rot_alpha

    n_ship_total = int(rot_mask.sum())
    if n_ship_total == 0:
        return SynthExample(image=Image.fromarray(rot_rgb),
                            heading_deg=target_heading,
                            visibility_frac=1.0,
                            ship_mask=_rotate_canonical(target_heading),
                            bow_end=1)

    cy, cx = H // 2, W // 2

    # Compute ship centroid from mask so occluders can be positioned
    # relative to it rather than image center.  For a sprite that
    # sits close to (cx, cy) after rotation, these will match.
    ys, xs = np.where(rot_mask)
    if len(ys) > 0:
        ship_cy = int(ys.mean())
        ship_cx = int(xs.mean())
    else:
        ship_cy, ship_cx = cy, cx

    if strategy == "none":
        occ_mask = np.zeros((H, W), dtype=bool)
    elif strategy in ("front_half", "back_half",
                      "left_half", "right_half"):
        side = strategy.split("_")[0]
        occ_mask = _ship_half_mask((H, W), ship_cx, ship_cy,
                                   target_heading, side)
    elif strategy == "center_band":
        occ_mask = _center_band_mask((H, W), ship_cx, ship_cy,
                                     target_heading,
                                     half_width=random.randint(4, 6))
    elif strategy == "random_blobs":
        occ_mask = _random_blob_mask((H, W), (ship_cx, ship_cy))
    elif strategy == "random_polygons":
        occ_mask = _random_polygon_mask((H, W), (ship_cx, ship_cy))
    elif strategy == "random_rect":
        occ_mask = _random_rect_mask((H, W), (ship_cx, ship_cy))
    elif strategy in ("keep_front_third", "keep_back_third",
                      "keep_front_quarter", "keep_back_quarter"):
        keep_end = "front" if "front" in strategy else "back"
        keep_frac = 1/3 if "third" in strategy else 1/4
        occ_mask = _ship_keep_fraction_mask((H, W), rot_mask,
                                            ship_cx, ship_cy,
                                            target_heading, keep_end,
                                            keep_frac)
    elif strategy in ("pirate_boss_bow", "pirate_boss_stern"):
        keep_end = "front" if strategy == "pirate_boss_bow" else "back"
        occ_mask = _pirate_boss_mask((H, W), rot_mask, ship_cx, ship_cy,
                                     target_heading, keep_end)
    elif strategy == "text_overlay":
        occ_mask = _text_overlay_mask((H, W), rot_mask, ship_cx, ship_cy)
    else:
        occ_mask = np.zeros((H, W), dtype=bool)

    # Coverage floor: if the occlusion strategy would leave the ship
    # essentially invisible, downgrade to a less aggressive strategy.
    # A near-empty ship + heading X° is unlearnable.  Raised to 30%
    # (from 15%) after 2026-07-02 eyeballing.
    # EXCEPTION: keep_* strategies deliberately leave only 15-33% of
    # the ship visible — that's the training signal (endpoint intact,
    # rest occluded).  Skip the downgrade for them entirely.
    n_visible_pre = int((rot_mask & ~occ_mask).sum())
    aggressive_strat = strategy in (
        "keep_front_third", "keep_back_third",
        "keep_front_quarter", "keep_back_quarter",
        "pirate_boss_bow", "pirate_boss_stern",
        "text_overlay")
    if (not aggressive_strat and n_ship_total > 0
            and (n_visible_pre / n_ship_total) < 0.30):
        # Retry with a less aggressive strategy — small ship-centered
        # blob or no occlusion at all.
        strategy = "random_blobs" if random.random() < 0.5 else "none"
        if strategy == "none":
            occ_mask = np.zeros((H, W), dtype=bool)
        else:
            occ_mask = _random_blob_mask((H, W), (ship_cx, ship_cy))

    # Pirate boss uses a fixed saturated-yellow fill matching the
    # actual in-game boss NPC color; text_overlay uses white glyphs.
    if strategy in ("pirate_boss_bow", "pirate_boss_stern"):
        force = np.array([245, 220, 90], dtype=np.uint8)
    elif strategy == "text_overlay":
        force = np.array([250, 250, 245], dtype=np.uint8)
    else:
        force = None
    out_rgb = _occluder_fill(rot_rgb, occ_mask, rot_mask,
                             force_color=force)
    n_visible = int((rot_mask & ~occ_mask).sum())
    visibility_frac = n_visible / n_ship_total

    # v15 targets: the CLEAN ship silhouette (before occlusion) and
    # which end of the mask's PCA axis is the bow.  These are the
    # ground-truth labels for the multi-head shape-prior training.
    # ship_mask is derived from the canonical bow-up template rotated
    # to target_heading — clean, consistent, no per-sprite variance
    # (see docs/heading_reconstruction_ensemble_design.md).
    ship_mask_gt = _rotate_canonical(target_heading)
    bow_end = _bow_end_from_mask_and_heading(ship_mask_gt, target_heading)
    return SynthExample(image=Image.fromarray(out_rgb),
                        heading_deg=target_heading,
                        visibility_frac=visibility_frac,
                        ship_mask=ship_mask_gt,
                        bow_end=bow_end)
