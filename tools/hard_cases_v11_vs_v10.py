"""HTML report comparing v11 and v10 CNN readings against motion bearing
on the saved hard-case frames.

For each tick in data/heading_hard_cases/, run both CNNs, compute motion
bearing from ±3 ticks of lat/lon in the source voyage trace, annotate
the frame with three arrows (yellow=v10, green=v11, cyan=motion), and
render an HTML table sorted by |v11-v10| descending.
"""
from __future__ import annotations

import base64, io, json, math, sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw
from scipy.ndimage import label as cc_label, binary_dilation

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain.ai_nav.learned.heading_cnn import HeadingCNN, decode_heading

REPO = Path(__file__).resolve().parents[1]
HARD = REPO / "data/heading_hard_cases"
V11 = REPO / "data/heading_cnn_v11_keep_endpoints_2026-07-02/best.pt"
V10 = REPO / "data/heading_cnn/best_v10_clean_bgs_2026-07-02.pt"
REF_BG_PATH = REPO / "data/heading_backgrounds/bg_000000.png"

# Yellow-mask constants (matches CNNHeading)
Y_R, Y_G, Y_B, Y_RG = 180, 150, 130, 40


def _ship_center_and_crop(rgb):
    H, W, _ = rgb.shape
    R, G, B = (rgb[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    if int(green.sum()) < 10:
        return None
    lab, _ = cc_label(green)
    sizes = np.bincount(lab.ravel())[1:]
    biggest = int(np.argmax(sizes)) + 1
    ys, xs = np.where(lab == biggest)
    cy = float(ys.mean()); cx = float(xs.mean())
    half = 40
    cy_i = int(round(max(half, min(H - half, cy))))
    cx_i = int(round(max(half, min(W - half, cx))))
    return (cx_i, cy_i), rgb[cy_i - half:cy_i + half, cx_i - half:cx_i + half]


def run_cnn(model, device, ref_bg, crop):
    cR, cG, cB = (crop[..., c].astype(np.int16) for c in range(3))
    c_green = (cG > 140) & (cG - cR > 30) & (cG - cB > 30)
    c_yellow = ((cR > Y_R) & (cG > Y_G) & (cB < Y_B)
                & (np.abs(cR - cG) < Y_RG))
    ship_region = binary_dilation(c_green, iterations=2)
    ship_only = ship_region & ~c_yellow
    composite = ref_bg.copy()
    composite[ship_only] = crop[ship_only]
    arr = composite.astype(np.float32) / 255.0
    tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.no_grad():
        sin_cos, conf = model(tensor)
    return float(decode_heading(sin_cos).item()), float(conf.item())


def load_trace(session_name: str):
    sess_dir = REPO / "data/sessions" / session_name
    trace = sess_dir / "trace.jsonl"
    by_tick = {}
    for line in trace.open():
        d = json.loads(line)
        by_tick[d["tick"]] = d
    return by_tick


def motion_bearing_deg(by_tick, tick: int, window: int = 3):
    """Compass bearing from ±window ticks of lat/lon (north-up frame)."""
    ticks = [t for t in range(tick - window, tick + window + 1)
             if t in by_tick]
    if len(ticks) < 3:
        return None
    lats = [by_tick[t]["lat"] for t in ticks]
    lons = [by_tick[t]["lon"] for t in ticks]
    # Simple end-to-end delta over the window
    dlat = lats[-1] - lats[0]
    dlon = lons[-1] - lons[0]
    if abs(dlat) < 1e-4 and abs(dlon) < 1e-4:
        return None
    return (math.degrees(math.atan2(dlon, dlat)) + 360.0) % 360.0


def draw_arrow(draw, cx, cy, deg, length, color, width=2):
    th = math.radians(deg)
    x2 = int(cx + length * math.sin(th))
    y2 = int(cy - length * math.cos(th))
    draw.line([(cx, cy), (x2, y2)], fill=color, width=width)
    for sign in (-1, 1):
        ah = th + sign * math.radians(25) + math.pi
        ax = x2 + 6 * math.sin(ah)
        ay = y2 - 6 * math.cos(ah)
        draw.line([(x2, y2), (int(ax), int(ay))], fill=color, width=width)


def annotate(frame_path: Path, ship_xy, v10, v11, motion):
    img = Image.open(frame_path).convert("RGB").copy()
    d = ImageDraw.Draw(img, "RGBA")
    cx, cy = ship_xy
    L = 45
    draw_arrow(d, cx + 3, cy + 3, v10, L, (255, 204, 51, 255))     # yellow v10
    draw_arrow(d, cx - 3, cy + 3, v11, L, (80, 220, 100, 255))     # green v11
    if motion is not None:
        draw_arrow(d, cx, cy - 3, motion, L, (51, 221, 255, 255))  # cyan motion
    return img


def b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def ang_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def main():
    device = torch.device("mps" if torch.backends.mps.is_available()
                          else "cpu")
    ref_bg = np.asarray(Image.open(REF_BG_PATH).convert("RGB"))
    m10 = HeadingCNN().to(device).eval()
    m10.load_state_dict(torch.load(V10, map_location=device,
                                   weights_only=False)["model"])
    m11 = HeadingCNN().to(device).eval()
    m11.load_state_dict(torch.load(V11, map_location=device,
                                   weights_only=False)["model"])

    records = [json.loads(l) for l in (HARD/"metadata.jsonl").open()]
    # Group by session for trace lookup
    sessions = {}
    for r in records:
        sessions.setdefault(r["session"], []).append(r)

    rows = []
    for sess_name, recs in sessions.items():
        by_tick = load_trace(sess_name)
        for r in recs:
            frame = HARD / r["frame"]
            rgb = np.asarray(Image.open(frame).convert("RGB"))
            hit = _ship_center_and_crop(rgb)
            if hit is None:
                continue
            ship_xy, crop = hit
            h10, c10 = run_cnn(m10, device, ref_bg, crop)
            h11, c11 = run_cnn(m11, device, ref_bg, crop)
            m_bear = motion_bearing_deg(by_tick, r["tick"])
            img = annotate(frame, ship_xy, h10, h11, m_bear)
            rows.append({
                "tick": r["tick"],
                "sess": sess_name,
                "v10": h10, "c10": c10,
                "v11": h11, "c11": c11,
                "motion": m_bear,
                "diff": ang_diff(h10, h11),
                "final_wrong": r.get("final_also_wrong", False),
                "img_b64": b64(img),
                "user_note": r.get("user_note", ""),
            })

    rows.sort(key=lambda r: -r["diff"])

    def cell_img(r):
        return (f"<img src='data:image/png;base64,{r['img_b64']}' "
                f"style='width:260px'>")

    def row_html(r):
        m_str = (f"{r['motion']:.0f}°" if r['motion'] is not None
                 else "—")
        note = ("<span class='fw'>FINAL_WRONG</span>"
                if r['final_wrong'] else "")
        return (
            f"<tr>"
            f"<td>{r['tick']}</td>"
            f"<td>{cell_img(r)}</td>"
            f"<td><span class='v10'>{r['v10']:.0f}°</span><br>"
            f"<small>conf {r['c10']:.2f}</small></td>"
            f"<td><span class='v11'>{r['v11']:.0f}°</span><br>"
            f"<small>conf {r['c11']:.2f}</small></td>"
            f"<td><span class='motion'>{m_str}</span></td>"
            f"<td>{r['diff']:.0f}°</td>"
            f"<td>{note}</td>"
            f"</tr>"
        )

    html = f"""<!doctype html>
<meta charset='utf-8'>
<title>Hard cases — v11 vs v10 vs motion</title>
<style>
 body {{ font-family: monospace; background:#111; color:#eee; margin:20px; }}
 h1 {{ color:#ffcc33; }}
 table {{ border-collapse: collapse; margin-top: 12px; }}
 th, td {{ border:1px solid #444; padding:6px 10px; vertical-align:top;
          text-align:left; }}
 th {{ background:#222; color:#ffcc33; }}
 tr:nth-child(even) td {{ background:#181818; }}
 img {{ image-rendering: pixelated; display:block; }}
 small {{ color:#888; }}
 .v10 {{ color:#ffcc33; font-weight:bold; }}
 .v11 {{ color:#50dc64; font-weight:bold; }}
 .motion {{ color:#33ddff; font-weight:bold; }}
 .fw {{ background:#a02020; color:#fff; padding:2px 6px; border-radius:3px;
       font-size:11px; }}
 .legend {{ background:#181818; padding:14px 20px; border:1px solid #333;
           display:inline-block; }}
</style>

<h1>Hard cases — v11 vs v10 vs motion bearing</h1>

<div class='legend'>
 <p>All rows use the annotated tick frame (mini-map crop) from voyage
    <code>ai_nav_2026-07-02T17-16-48</code>.  Arrows overlaid at the
    ship centroid:</p>
 <p>
   <span class='v10'>■ yellow</span> = v10 (production during voyage) &nbsp;·&nbsp;
   <span class='v11'>■ green</span> = v11 (new CNN, inferred now) &nbsp;·&nbsp;
   <span class='motion'>■ cyan</span> = motion bearing (±3-tick lat/lon delta)
 </p>
 <p>Motion bearing is the geographic direction the ship actually
    traveled around this tick — a good proxy for true heading when
    the ship is under way and not just rotating.  "FINAL_WRONG" tag
    marks the 4 ticks where the user identified the safety-net-corrected
    final heading was still wrong (heavy occlusion, off-axis truth).</p>
 <p>Sorted by |v11 − v10| descending — biggest disagreements first.</p>
</div>

<table>
 <tr><th>tick</th><th>frame</th><th>v10</th><th>v11</th>
     <th>motion</th><th>|Δ v10-v11|</th><th>flag</th></tr>
 {''.join(row_html(r) for r in rows)}
</table>
"""
    out = REPO / "data/heading_hard_cases/v11_vs_v10.html"
    out.write_text(html)
    print(f"wrote {out}")
    print(f"cases: {len(rows)}, sorted by v11-v10 disagreement desc")


if __name__ == "__main__":
    main()
