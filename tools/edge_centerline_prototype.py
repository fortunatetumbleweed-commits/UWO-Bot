"""Edge-anchored centerline extraction — prototype.

Algorithm:
  1. Build water mask (V11 → closing → CC → narrow filter → CC).
  2. Distance transform of the mask.
  3. Find "openings" = contiguous strips of water pixels touching each
     frame edge (top, bottom, left, right).  Anchor point = midpoint
     of each strip.
  4. For each opening, compute the shortest path from the ship's
     position to the opening's anchor, using edge cost = 1 / (DT + 1)
     over the water mask (8-connected).  Shortest path in this cost
     field is the centerline (wide water = low cost = preferred).
  5. Result: one polyline per opening — ignores dead-ends, spurs, and
     internal artifacts entirely.

Contrast with current skeleton pipeline: no branch enumeration, no
pruning, no anchor classification.  Only openings are meaningful; the
algorithm produces exactly as many centerlines as there are openings.
"""
from __future__ import annotations
import sys, os, math, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from typing import Optional
import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import label as cc_label, distance_transform_edt
from skimage.morphology import binary_closing, disk
from skimage.graph import route_through_array
from tools.perception_v10_channel_prototype import v11_brightness_channel_mask


# Trim to match the calibrated MINIMAP_CROP shrink (L +5, T +3, R -5).
# Gallery images were captured before that adjustment.
TRIM_LEFT   = 5
TRIM_TOP    = 3
TRIM_RIGHT  = 5

MASK_CLOSING_RADIUS = 7
MASK_MIN_DT_PX      = 5


def build_mask(img_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (mask, dt) using the production pipeline."""
    raw, _ = v11_brightness_channel_mask(img_rgb)
    closed = binary_closing(raw, disk(MASK_CLOSING_RADIUS))
    lbl, _ = cc_label(closed)
    H, W = closed.shape
    sr, sc = H // 2, W // 2
    scc = lbl[sr, sc]
    if scc == 0:
        sizes = np.bincount(lbl.ravel()); sizes[0] = 0
        scc = int(np.argmax(sizes)) if sizes.max() > 0 else 0
    cc = (lbl == scc) if scc else closed
    dt = distance_transform_edt(cc)
    wide = cc & (dt >= MASK_MIN_DT_PX)
    lbl2, _ = cc_label(wide)
    scc2 = lbl2[sr, sc] if wide[sr, sc] else 0
    if scc2 == 0:
        sizes = np.bincount(lbl2.ravel()); sizes[0] = 0
        scc2 = int(np.argmax(sizes)) if sizes.max() > 0 else 0
    mask = (lbl2 == scc2) if scc2 else wide
    dt2 = distance_transform_edt(mask)
    return mask, dt2


def find_openings(mask: np.ndarray) -> list[dict]:
    """Return list of {side, y, x, span} anchor dicts, one per contiguous
    water strip on each frame edge."""
    H, W = mask.shape
    out = []
    def strips_from(arr):
        if len(arr) == 0: return []
        strips = []; s = arr[0]; last = arr[0]
        for v in arr[1:]:
            if v > last + 1:
                strips.append((int(s), int(last))); s = v
            last = v
        strips.append((int(s), int(last)))
        return strips
    for side, ys, xs in [
        ("top",    np.where(mask[0, :])[0],   None),
        ("bottom", np.where(mask[H-1, :])[0], None),
        ("left",   np.where(mask[:, 0])[0],   None),
        ("right",  np.where(mask[:, W-1])[0], None),
    ]:
        pass
    # top
    for s, e in strips_from(np.where(mask[0, :])[0]):
        out.append({"side": "top", "y": 0, "x": (s + e) // 2, "span": e - s + 1})
    for s, e in strips_from(np.where(mask[H-1, :])[0]):
        out.append({"side": "bottom", "y": H-1, "x": (s + e) // 2, "span": e - s + 1})
    for s, e in strips_from(np.where(mask[:, 0])[0]):
        out.append({"side": "left", "y": (s + e) // 2, "x": 0, "span": e - s + 1})
    for s, e in strips_from(np.where(mask[:, W-1])[0]):
        out.append({"side": "right", "y": (s + e) // 2, "x": W-1, "span": e - s + 1})
    return out


def snap_to_water(mask: np.ndarray, y: int, x: int) -> tuple[int, int]:
    """If (y,x) is on land, snap to the nearest water pixel."""
    if mask[y, x]:
        return y, x
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return y, x
    d2 = (ys - y)**2 + (xs - x)**2
    i = int(np.argmin(d2))
    return int(ys[i]), int(xs[i])


def compute_centerline(mask: np.ndarray, dt: np.ndarray,
                       start: tuple[int, int], end: tuple[int, int]
                       ) -> Optional[list[tuple[int, int]]]:
    """Shortest path in cost=1/(DT+1) field from start to end.  Returns
    the pixel polyline, or None if not connected."""
    H, W = mask.shape
    # Cost: infinite on land, 1/(DT+1) on water
    cost = np.full(mask.shape, 1e6, dtype=np.float32)
    cost[mask] = 1.0 / (dt[mask] + 1.0)
    try:
        path, total = route_through_array(cost, start, end,
                                          fully_connected=True,
                                          geometric=True)
        return path
    except Exception:
        return None


def render(img: np.ndarray, mask: np.ndarray, openings: list[dict],
           centerlines: list[list[tuple[int, int]]],
           ship_xy: tuple[int, int],
           label: str = "") -> Image.Image:
    """Overlay mask + centerlines + openings on the image."""
    H, W = mask.shape
    ov = img.astype(np.int16).copy()
    ov[mask] = (ov[mask] * 0.4 + np.array([0, 80, 200]) * 0.6).astype(np.int16)
    im = Image.fromarray(np.clip(ov, 0, 255).astype(np.uint8))
    S = 3
    im = im.resize((W*S, H*S), Image.NEAREST)
    d = ImageDraw.Draw(im)
    # Centerlines
    for pl in centerlines:
        for i in range(len(pl) - 1):
            y0, x0 = pl[i]; y1, x1 = pl[i+1]
            d.line([x0*S, y0*S, x1*S, y1*S], fill=(255, 255, 255), width=2)
    # Openings — yellow squares
    for o in openings:
        py, px = o["y"] * S, o["x"] * S
        d.rectangle([px-8, py-8, px+8, py+8], outline=(255, 220, 0), width=3)
        d.text((px + 12, py - 12),
               f"{o['side']}({o['span']}px)", fill=(255, 220, 0))
    # Ship — lime
    sy, sx = ship_xy[0] * S, ship_xy[1] * S
    d.ellipse([sx-10, sy-10, sx+10, sy+10], outline=(0, 255, 0), width=3)
    if label:
        d.text((10, 10), label, fill=(255, 255, 255))
    return im


def process_tick(session: str, tick: int, gallery_dir: Path,
                 out_dir: Path) -> dict:
    """Load a raw tick, run edge-anchored centerline, save viz.

    Returns a summary dict for the HTML gallery."""
    raw_path = (Path(__file__).resolve().parents[1]
                / "data" / "sessions" / session / f"tick_{tick:04d}.png")
    if not raw_path.exists():
        return {"ok": False, "reason": f"raw tick missing: {raw_path}"}

    img_full = np.asarray(Image.open(raw_path).convert("RGB"))
    img = img_full[TRIM_TOP:, TRIM_LEFT:img_full.shape[1] - TRIM_RIGHT]
    H, W = img.shape[:2]
    ship_yx = (H // 2, W // 2)

    mask, dt = build_mask(img)
    openings = find_openings(mask)
    ship_snapped = snap_to_water(mask, *ship_yx)
    centerlines = []
    for o in openings:
        opening_snapped = snap_to_water(mask, o["y"], o["x"])
        cl = compute_centerline(mask, dt, ship_snapped, opening_snapped)
        if cl is not None:
            centerlines.append(cl)

    label = (f"{session}\nt={tick}  "
             f"openings={len(openings)}  centerlines={len(centerlines)}")
    im = render(img, mask, openings, centerlines, ship_yx, label=label)
    out_name = f"{session}_t{tick:04d}.png"
    im.save(out_dir / out_name)
    return {"ok": True,
            "session": session,
            "tick": tick,
            "openings": openings,
            "centerlines": len(centerlines),
            "out": out_name}


def main():
    gallery = (Path(__file__).resolve().parents[1]
               / "docs" / "tactical_regression_gallery" / "images")
    out_dir = Path("/tmp/centerline_viz"); out_dir.mkdir(exist_ok=True)
    entries = sorted(gallery.glob("*.png"))
    print(f"Processing {len(entries)} gallery ticks…")
    results = []
    for p in entries:
        m = re.match(r"(ai_nav_[^_]+)_t(\d+)\.png$", p.name)
        if not m: continue
        session, tick = m.group(1), int(m.group(2))
        try:
            r = process_tick(session, tick, gallery, out_dir)
        except Exception as e:
            r = {"ok": False, "reason": f"{type(e).__name__}: {e}",
                 "session": session, "tick": tick,
                 "out": None}
        results.append(r)
        if r["ok"]:
            print(f"  {p.name}  openings={len(r['openings'])}  "
                  f"centerlines={r['centerlines']}")
        else:
            print(f"  {p.name}  FAIL: {r['reason']}")

    # Emit an HTML side-by-side gallery
    html_lines = ['<!doctype html><html><head><meta charset="utf-8">',
                  '<title>Edge-anchored centerline prototype</title>',
                  '<style>body{font-family:sans-serif;margin:20px;background:#f6f6f8}',
                  'figure{display:inline-block;margin:8px;background:white;',
                  'padding:8px;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.15)}',
                  'figcaption{font-size:12px;color:#555;padding-top:4px}',
                  '.pair{display:flex;gap:8px;align-items:flex-start}',
                  'img{display:block;border:1px solid #999;max-width:400px}',
                  '</style></head><body>',
                  '<h1>Edge-anchored centerline vs current gallery</h1>',
                  '<p>Left: current pipeline (skeleton + all branches). '
                  'Right: prototype (openings + centerlines only). '
                  'Trimmed L+5 T+3 R-5 to match calibrated MINIMAP_CROP.</p>']
    for r in results:
        if not r.get("ok"):
            html_lines.append(f'<div><b>FAIL</b> {r.get("session","?")} '
                              f't{r.get("tick","?")}: {r.get("reason","")}'
                              f'</div>')
            continue
        gname = f"{r['session']}_t{r['tick']:04d}.png"
        html_lines.append(
            f'<div class="pair">'
            f'  <figure><img src="{gallery / gname}">'
            f'  <figcaption>OLD skeleton: {r["session"]} t{r["tick"]}</figcaption></figure>'
            f'  <figure><img src="{r["out"]}">'
            f'  <figcaption>NEW centerline: openings={len(r["openings"])} '
            f'  centerlines={r["centerlines"]}</figcaption></figure>'
            f'</div>')
    html_lines.append('</body></html>')
    html_path = out_dir / "index.html"
    html_path.write_text("\n".join(html_lines))
    print(f"\nHTML: {html_path}")
    return results


if __name__ == "__main__":
    main()
