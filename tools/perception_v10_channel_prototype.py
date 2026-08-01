"""V10 channel-extraction perception prototype.

The V10 pipeline frames the problem as **channel extraction** rather than
bank detection: find the connected water region the ship sails in,
discard everything inside it (NPCs, sonar, village markers, small
islands) as a single clean polygon whose boundary is the bank.

Iterated during 2026-06-08/09 in /tmp/ while diagnosing a string of
failure modes in the prior colour-rule pipeline (V8) and the
production `MinimapNavigationView` pipeline.  Promoted here so the
artefact survives /tmp/ being cleared and can be A/B'd against the
production water_mask via `tools/tick_viewer.py` (toggle `v`).

Pipeline
─────────
1. Tight sprite mask (`confident_sprite_mask`):
     yellow + red + green + WHITE>235 (tightened from V8's >215 to avoid
     catching the brightest text-stroke pixels).
2. Text-only inpaint (`cv2.inpaint(text_thin, TELEA)`):
     fills only the thin-stroke pixels of text-like uniform-light-gray
     regions.  Crucially, sprites stay at their original colour during
     the inpaint, so they act as a barrier — water colour from the
     river side of a diamond marker can't bleed across the marker into
     the village text on the land side.
3. K-means K=5 on bilateral-filtered cleaned input:
     fit EXCLUDES the original (un-dilated) sprite ∪ text masks so the
     cluster centres represent natural water/land, not synthetic
     inpaint colours.  Argmin assigns labels to every pixel.
4. Water-cluster selection by `is_water_center`:
     dark (lum < 140) + blue-dominant (b > r + 10, b ≥ g - 5).
5. V5-style sprite-in-water rescue:
     ONLY confident sprites adjacent to existing water flip to water.
     Text is NOT rescued (this fixed the t152 Nubia 666-pixel
     text→water bug from V8).
6. Cleanup: `fill_holes` → `dilate(2)` → padded `binary_opening(disk(5))`.
7. **Channel extraction** — the V10 contribution:
   - Connected components of NOT-water.  Engulf any CC smaller than
     ISLAND_THR_PX (default 400) as water — removes NPCs, sonar
     remnants, village structures, small islands.
   - Connected components of WATER.  Keep only the largest — drops
     spurious tiny water blobs in the land area.
8. Result: one binary channel polygon.

Compared to the production `MinimapNavigationView.water_mask`, V10
does NOT punch holes for the bot's own ship sprite or its yellow
sonar fan, so the skeleton built from V10 has clean topology near the
ship.  See `memory/project_ship_sonar_punch_holes_in_water_mask.md`.

Known limitations
─────────────────
- Colour heuristics: `is_water_center` requires dark + blue-dominant.
  Dawn / dusk / open-sea bright water (lum 140-200, still blue) fails
  the gate, producing an empty channel.  Production's brightness
  thresholding handles this better.  See
  `memory/feedback_prefer_learned_segmentation_over_color_rules.md`
  for the longer-term direction (learned segmentation).
- ISLAND_THR_PX is a single global threshold; a small Mediterranean
  arm next to the Nile can be dropped as a "spurious" water CC.

Usage
─────
  python -m tools.perception_v10_channel_prototype

  Reads frames from /tmp/tick_*.png by default.  Plot saved to
  /tmp/v10_pipeline.png.  Tweak the LABELED list or call the helper
  functions directly to point at other frames.
"""
from __future__ import annotations

import numpy as np
import matplotlib.pyplot as plt
import cv2
from PIL import Image, ImageFilter
from scipy.ndimage import (binary_fill_holes, binary_dilation, binary_erosion,
                           distance_transform_edt, label as cc_label)
from skimage.morphology import binary_opening, disk

TRIM_TOP, TRIM_RIGHT = 6, 6
SPRITE_DILATE   = 2
WHITE_THR       = 235
TEXT_THIN_ERODE = 2
ISLAND_THR_PX   = 400


LABELED = [
    (5,   "delta_apex",    "/tmp/tick_0005.png",
     "/tmp/bank_ground_truth/tick_0005_delta_apex_bank_mask.png"),
    (100, "clean_channel", "/tmp/tick_0100.png",
     "/tmp/bank_ground_truth/tick_0100_clean_channel_bank_mask.png"),
    (152, "nubia_village", "/tmp/tick_0152.png",
     "/tmp/bank_ground_truth/tick_0152_nubia_village_bank_mask.png"),
    (300, "viewport_bend", "/tmp/tick_0300.png",
     "/tmp/bank_ground_truth/tick_0300_viewport_bend_bank_mask.png"),
    (475, "Y_junction",    "/tmp/tick_0475.png",
     "/tmp/bank_ground_truth/tick_0475_Y_junction_bank_mask.png"),
    (500, "dead_end",      "/tmp/tick_0500.png",
     "/tmp/bank_ground_truth/tick_0500_dead_end_bank_mask.png"),
    (571, "bari_village",  "/tmp/tick_0571_bari_village.png",
     "/tmp/bank_ground_truth/tick_0571_bari_village_bank_mask.png"),
    (750, "U_bend",        "/tmp/tick_0750_y_branch.png",
     "/tmp/bank_ground_truth/tick_0750_U_bend_bank_mask.png"),
    (786, "lake_mouth",    "/tmp/tick_0786_lake_at_end.png",
     "/tmp/bank_ground_truth/tick_0786_lake_mouth_bank_mask.png"),
]


def confident_sprite_mask(img):
    R, G, B = (img[:, :, c].astype(np.int16) for c in range(3))
    yellow = (R > 180) & (G > 150) & (B < 130)
    green  = (G > 140) & (G - R > 30) & (G - B > 30)
    white  = (R > WHITE_THR) & (G > WHITE_THR) & (B > WHITE_THR)
    red    = (R > 180) & (R - G > 40) & (R - B > 40)
    return yellow | green | white | red


def text_like_mask(img, thin=True):
    R, G, B = (img[:, :, c].astype(np.int16) for c in range(3))
    lum = (R + G + B) // 3
    uniform = ((np.abs(R - G) < 18) & (np.abs(G - B) < 18)
               & (np.abs(R - B) < 18))
    text = uniform & (lum > 195) & (lum < 240)
    H = img.shape[0]
    band = np.zeros_like(text); band[H // 3:, :] = True
    text = text & band
    if thin:
        text = text & ~binary_erosion(text, iterations=TEXT_THIN_ERODE)
    return text


def is_water_center(c):
    r, g, b = int(c[0]), int(c[1]), int(c[2])
    return (r + g + b) // 3 < 140 and b > r + 10 and b >= g - 5


def _v9_water_mask(img):
    """V9 water mask: text-only inpaint + K-means + V5 rescue + cleanup."""
    txt_thin = text_like_mask(img, thin=True)
    if txt_thin.any():
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        bgr = cv2.inpaint(bgr, txt_thin.astype(np.uint8), 3,
                          cv2.INPAINT_TELEA)
        cleaned = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    else:
        cleaned = img.copy()
    conf = confident_sprite_mask(img)
    conf_dil = binary_dilation(conf, iterations=SPRITE_DILATE)
    txt_full = text_like_mask(img, thin=False)
    exclude = conf_dil | txt_full
    bgr = cv2.cvtColor(cleaned, cv2.COLOR_RGB2BGR)
    blur = cv2.cvtColor(cv2.bilateralFilter(bgr, 7, 40, 10), cv2.COLOR_BGR2RGB)
    Z = blur[~exclude].reshape(-1, 3).astype(np.float32)
    if Z.shape[0] < 5:
        return np.zeros(img.shape[:2], dtype=bool), conf
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 25, 0.5)
    _, _, centers = cv2.kmeans(Z, 5, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    all_p = blur.reshape(-1, 3).astype(np.float32)
    d = np.linalg.norm(all_p[:, None, :] - centers[None, :, :], axis=-1)
    labels = np.argmin(d, axis=1).reshape(img.shape[0], img.shape[1])
    cu8 = centers.astype(np.uint8)
    wc = [j for j, c in enumerate(cu8) if is_water_center(c)]
    water = np.isin(labels, wc)
    confident_in_water = conf & binary_dilation(water, iterations=3)
    water = water | confident_in_water
    m = binary_fill_holes(water)
    m = binary_dilation(m, iterations=2)
    P = 6
    mp = np.pad(m, P, mode="edge")
    mp = binary_opening(mp, disk(5))
    return mp[P:-P, P:-P], conf


MIN_KEEP_WATER_CC_PX = 2000


def extract_channel(mask, island_thr=ISLAND_THR_PX,
                    min_keep_water=MIN_KEEP_WATER_CC_PX):
    """Channel extraction:
      a. drop small not-water CCs (engulf as water = remove islands).
      b. KEEP the largest water CC AND any other water CC ≥ `min_keep_water`.

    The multi-CC keep matters when a horizontal/vertical overlay (blue
    transient ribbon, fog band) splits the channel into two pieces.
    Without this, "keep largest only" silently drops the top half of
    the channel — see t79 of hug_debug_20260601_231040 where the blue
    notice ribbon across the middle split a 8400-px channel into a
    5176-px bottom + 3242-px top, with V11 keeping just the bottom.

    Returns (channel_mask, n_islands_removed, total_water_kept).
    """
    nw_labels, n_nw = cc_label(~mask)
    nw_sizes = np.bincount(nw_labels.ravel())
    nw_sizes[0] = 0
    largest_nw = int(np.argmax(nw_sizes)) if n_nw > 0 else 0
    out = mask.copy()
    n_islands = 0
    for lab in range(1, n_nw + 1):
        if lab == largest_nw:
            continue
        if nw_sizes[lab] < island_thr:
            out[nw_labels == lab] = True
            n_islands += 1
    w_labels, n_w = cc_label(out)
    if n_w == 0:
        return out, n_islands, 0
    w_sizes = np.bincount(w_labels.ravel())
    w_sizes[0] = 0
    largest_w = int(np.argmax(w_sizes))
    keep_mask = (w_labels == largest_w)
    # Multi-CC keep: also include any other water CC at least
    # `min_keep_water` pixels.  This recovers split-channel halves
    # without re-introducing tiny spurious water blobs.
    for lab in range(1, n_w + 1):
        if lab == largest_w:
            continue
        if w_sizes[lab] >= min_keep_water:
            keep_mask |= (w_labels == lab)
    return keep_mask, n_islands, int(keep_mask.sum())


def v10_channel_mask(img):
    """End-to-end V10 channel mask.  Returns (channel, n_islands, conf).
    `img` is a `np.ndarray` RGB minimap crop (any size that survived the
    cv2 pipeline)."""
    m_v9, conf = _v9_water_mask(img)
    chan, n_islands, _ = extract_channel(m_v9)
    return chan, n_islands, conf


# ── V11 — brightness-based water detection + V10 channel extraction ──
#
# The V10 → V11 pivot, motivated by the 800-tick reference replay
# finding that V10's colour-rule water detection fails 9% of the time
# (every dawn/dusk transition).  Production already uses a hue-
# invariant brightness threshold (`gray > 175` for land) and handles
# every reference frame.  V11 keeps that simple water detection but
# adds V10's CC-based channel extraction step on top — drop islands,
# keep the largest connected water region — to get a clean channel
# polygon for skeleton / graph extraction without inheriting
# production's sprite-punch-hole bug.
#
# Pipeline:
#   1. `gray <= BRIGHTNESS_THR` (production's V > 175 inverted).
#   2. Light morphological close to bridge ship-icon AA edges.
#   3. V10's channel extraction:
#        - drop NOT-water CCs smaller than ISLAND_THR_PX,
#        - keep only the largest water CC.

BRIGHTNESS_THR_V11 = 160
# Saturation escape: admit SATURATED pixels as water even when they exceed the
# brightness threshold.  Water reflects/refracts the ambient light and stays
# saturated whatever the time-of-day hue — blue glare (B−R +100) at t721,
# orange sunset (B−R −70) at t695 both sit at sat 0.4-0.6 — whereas the desert
# land it must be separated from is washed-out (sat ~0.09) and the radar-disc
# overlay the 160 threshold was tightened to exclude is only weakly coloured
# (sat ~0.14).  So a saturation gate is lighting-INVARIANT where absolute
# brightness (fails on bright glare) and blue-dominance (fails on orange) both
# break.  0.20 sits in the water(0.28+)/desert(0.09)/disc(0.14) gap with
# margin on all sides (t185-187 disc corpus: 0 anomaly).
V11_SAT_ESCAPE_THR = 0.20
V11_MEDIAN_RADIUS = 3
V11_CENTER_SEARCH_RADIUS = 25
V11_OVER_WATER_DILATE_PX = 8
V11_PAINT_COLOR = np.array([50, 80, 80], dtype=np.uint8)   # dark blue-gray


def v11_brightness_channel_mask(img):
    """Brightness-threshold water + sprite-over-water paint + flood from
    nearest-water-to-center.

    Primary rule: pixel is water if grayscale ≤ 160 (was 175; tightened
    2026-07-22 to exclude the 165-185 borderline zone occupied by the
    mini-map's semi-transparent radar-disc overlay, which was flipping
    large borderline runs into water at random ticks — see
    `data/reference/color_edge_cases_2026-07-22/README.md`, t186 the
    canonical failing case).

    Sprite handling (2026-07-22): NPCs/pirates/players/diamond markers
    on the water body get classified as land by pure brightness
    threshold (bright yellow/red/green/white ≫ 160).  When a sprite
    sits at a frame-edge water exit it splits the water run into two
    sub-runs, which destabilises the tactical-tracker anchor midpoint
    tick-to-tick (t37→t38 in session ai_nav_2026-07-22T15-23-02: the
    exit midpoint jumped 256→302 as an NPC face moved out of the
    bottom row).  We detect sprites via
    `confident_sprite_mask` (yellow + green + white + red) and paint
    them water-blue BEFORE thresholding — but only sprites in the
    "over-water zone" (dilated raw water mask).  Sprites over land
    (HUD lat/lon text, land-side settlement icons, etc.) are left
    alone so we don't create false water islands on land.

    Pipeline:
      1. Raw threshold (no median) → raw_water; dilate to over-water zone.
      2. Detect sprites (yellow + green + white + red) via
         `confident_sprite_mask`, dilate slightly.
      3. sprites_over_water = sprites & over-water zone;
         paint those pixels with a dark water-blue color.
      4. On the painted image: median-3 blur, gray ≤ 160 threshold.
      5. Flood fill from the water pixel nearest to image center
         (the ship's position) — returns only that CC.

    No `binary_fill_holes` — internal radar rings inside the river
    show up as tiny (1-2 px) not-water holes but do not affect the
    channel boundary or the skeleton medial-axis.

    Returns (channel, n_islands_removed).  n_islands is 0 with this
    pipeline (kept for interface compatibility).
    """
    # Step 1: raw water zone (no median) — used to guard sprite painting.
    raw_gray = np.array(Image.fromarray(img).convert("L"))
    raw_water = raw_gray <= BRIGHTNESS_THR_V11
    over_water_zone = binary_dilation(raw_water,
                                       iterations=V11_OVER_WATER_DILATE_PX)

    # Step 2-3: detect sprites over water, paint them.
    sprites = binary_dilation(confident_sprite_mask(img),
                               iterations=SPRITE_DILATE)
    sprites_over_water = sprites & over_water_zone
    painted = img.copy()
    painted[sprites_over_water] = V11_PAINT_COLOR

    # Step 4-5: normal V16 pipeline on painted image.
    pil = Image.fromarray(painted).convert("L").filter(
        ImageFilter.MedianFilter(V11_MEDIAN_RADIUS))
    gray = np.array(pil)
    water = gray <= BRIGHTNESS_THR_V11
    # Saturation escape: recover bright-but-saturated water that the brightness
    # threshold rejects.  Water reflects/refracts the ambient light and stays
    # SATURATED whatever the time-of-day hue (blue glare B−R+100 at t721,
    # orange sunset B−R−70 at t695 — both sat 0.4-0.6); the desert land it must
    # be separated from is washed-out (sat ~0.09) and the radar-disc overlay is
    # only weakly coloured (sat ~0.14).  So a saturation gate is lighting-
    # INVARIANT where absolute brightness (fails on glare) and blue-dominance
    # (fails on orange) both break.  There is no green vegetation in the Nile/
    # lake terrain (all desert), so the only saturated non-water — the ship
    # marker and NPC sprites — is already handled (centre hole-fill + sprite
    # painting above), and the flood-from-centre drops any isolated
    # false-water island a stray sprite might create.
    _mx = painted.max(2).astype(np.int16)
    _mn = painted.min(2).astype(np.int16)
    _sat = (_mx - _mn) / np.maximum(_mx, 1)
    sat_escape = _sat > V11_SAT_ESCAPE_THR
    water = water | sat_escape
    chan = _flood_from_near_center(water, radius=V11_CENTER_SEARCH_RADIUS)
    return chan, 0


def _flood_from_near_center(mask, radius=25):
    """Return the CC containing the water pixel nearest to image
    center (within `radius` px).  Empty mask if no water within
    range.  Ship is always at image center on the mini-map, so this
    anchors channel extraction to the ship's actual water body."""
    if not mask.any():
        return np.zeros_like(mask)
    H, W = mask.shape
    cy, cx = H // 2, W // 2
    yy, xx = np.mgrid[:H, :W]
    d2 = (yy - cy) ** 2 + (xx - cx) ** 2
    d2_water = np.where(mask, d2, np.inf)
    idx = int(np.argmin(d2_water))
    if d2_water.flat[idx] > radius * radius:
        return np.zeros_like(mask)
    ny, nx = np.unravel_index(idx, mask.shape)
    labels, _ = cc_label(mask)
    return labels == labels[ny, nx]


def bank_pixels(channel_mask):
    """Bank = water/land 4-neighbour boundary, in (xs, ys) form."""
    bank = np.zeros_like(channel_mask)
    bank[1:-1, 1:-1] = channel_mask[1:-1, 1:-1] & (
        ~channel_mask[:-2, 1:-1] | ~channel_mask[2:, 1:-1]
        | ~channel_mask[1:-1, :-2] | ~channel_mask[1:-1, 2:]
    )
    ys, xs = np.where(bank)
    return xs, ys


def _run_demo():
    fig, axes = plt.subplots(len(LABELED), 4, figsize=(22, 4.0 * len(LABELED)))
    print(f"{'tk':>4} {'scenario':<16}  {'channel_px':>10} {'islands':>7}")
    print('-' * 50)
    for i, (tk, scn, raw, gt_p) in enumerate(LABELED):
        img = np.array(Image.open(raw).convert("RGB"))[TRIM_TOP:, :-TRIM_RIGHT]
        chan, n_islands, conf = v10_channel_mask(img)
        print(f"t{tk:>3} {scn:<16}  {int(chan.sum()):>10} {n_islands:>7}")
        binary = chan.astype(np.uint8) * 255
        axes[i][0].imshow(img); axes[i][0].axis("off")
        axes[i][0].set_title(f"t{tk} {scn} ORIGINAL", fontsize=10)
        axes[i][1].imshow(binary, cmap="gray", vmin=0, vmax=255)
        axes[i][1].axis("off")
        axes[i][1].set_title(f"V10 channel  {int(chan.sum())} px  "
                             f"({n_islands} islands removed)", fontsize=10)
        bxs, bys = bank_pixels(chan)
        axes[i][2].imshow(img, alpha=0.75)
        axes[i][2].scatter(bxs, bys, color="cyan", s=2, alpha=0.85)
        axes[i][2].axis("off")
        axes[i][2].set_title("V10 bank on original", fontsize=10)
        if gt_p:
            try:
                gt = np.array(Image.open(gt_p).convert("L"))
                if gt.shape != img.shape[:2]:
                    gt = np.array(Image.open(gt_p).convert("L").resize(
                        (img.shape[1] + TRIM_RIGHT, img.shape[0] + TRIM_TOP),
                        Image.NEAREST))[TRIM_TOP:, :-TRIM_RIGHT]
                gt = gt > 127
                gy, gx = np.where(gt)
                axes[i][3].imshow(img, alpha=0.75)
                axes[i][3].scatter(gx, gy, color="orange", s=2, alpha=0.7,
                                   label="GT bank")
                axes[i][3].scatter(bxs, bys, color="cyan", s=2, alpha=0.6,
                                   label="V10 bank")
                axes[i][3].axis("off")
                axes[i][3].legend(loc="lower right", fontsize=7)
                axes[i][3].set_title("V10 vs hand-labelled GT bank", fontsize=10)
            except FileNotFoundError:
                axes[i][3].axis("off")
        else:
            axes[i][3].axis("off")
    fig.suptitle(
        "V10 channel extraction prototype — drop small not-water CCs, "
        "keep largest water CC",
        fontsize=14, fontweight="bold")
    plt.tight_layout()
    out = "/tmp/v10_pipeline.png"
    plt.savefig(out, dpi=120, bbox_inches="tight")
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    _run_demo()
