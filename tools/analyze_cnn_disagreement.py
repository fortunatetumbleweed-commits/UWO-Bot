"""Build an HTML report of CNN primary/shadow disagreements + agreement-
but-safety-net-fired cases from a voyage trace.

Usage:
  python -m tools.analyze_cnn_disagreement <session_dir> [--out report.html]

Categories:
  A. DISAGREE >15°: primary CNN and shadow CNN differ by more than 15°.
  B. AGREE-SUSPICIOUS: primary and shadow agree (≤15°) BUT one or more
     safety-net filters fired (pca_tiebreak / physics_reject /
     noise_reject) AND final heading diverges >30° from CNN raw —
     meaning both models were confidently wrong the same way.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def ang_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def _ship_center(img: Image.Image) -> tuple[int, int]:
    """Locate the green ship blob in the mini-map crop.  Falls back to
    image centre when no green pixels found."""
    arr = np.asarray(img.convert("RGB"))
    R, G, B = (arr[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    if int(green.sum()) < 5:
        H, W = arr.shape[:2]
        return W // 2, H // 2
    ys, xs = np.where(green)
    return int(xs.mean()), int(ys.mean())


def _draw_arrow(draw: ImageDraw.ImageDraw, cx: int, cy: int,
                bearing_deg: float, length: int, color: tuple,
                width: int = 2) -> tuple[int, int]:
    """Compass bearing → arrow (north-up mini-map).  Returns tip."""
    th = math.radians(bearing_deg)
    dx = math.sin(th) * length
    dy = -math.cos(th) * length
    x2, y2 = int(cx + dx), int(cy + dy)
    draw.line([(cx, cy), (x2, y2)], fill=color, width=width)
    for sign in (-1, 1):
        ah = th + sign * math.radians(25) + math.pi
        ax = x2 + 6 * math.sin(ah)
        ay = y2 - 6 * math.cos(ah)
        draw.line([(x2, y2), (int(ax), int(ay))], fill=color, width=width)
    return x2, y2


def annotate_frame(src: Path, dst: Path, v10: float, v6: float,
                   final: float | None) -> None:
    """Save an annotated copy of the tick frame with heading arrows.

    Yellow  = v10 (primary CNN raw)
    Orange  = v6 (shadow CNN raw)
    Cyan    = final heading (after safety-net resolution)
    """
    img = Image.open(src).convert("RGB").copy()
    draw = ImageDraw.Draw(img, "RGBA")
    cx, cy = _ship_center(img)
    L = 45
    _draw_arrow(draw, cx + 2, cy + 2, v10, L, (255, 204, 51, 255))     # yellow
    _draw_arrow(draw, cx - 2, cy + 2, v6,  L, (255, 140, 0,  255))     # orange
    if final is not None:
        _draw_arrow(draw, cx, cy - 2, final, L, (51, 221, 255, 255))    # cyan
    img.save(dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None,
                    help="Output HTML path. Default: <session>/cnn_disagreement.html")
    args = ap.parse_args()

    sess = args.session_dir
    trace_path = sess / "trace.jsonl"
    if not trace_path.exists():
        raise SystemExit(f"no trace.jsonl at {trace_path}")

    disagree, agree_susp = [], []
    for line in trace_path.open():
        d = json.loads(line)
        v10 = d.get("cnn_raw_heading_deg")
        v6 = d.get("shadow_cnn_heading_deg")
        if v10 is None or v6 is None:
            continue
        diff = ang_diff(v10, v6)
        final = d.get("heading_deg")
        src = d.get("heading_source", "")
        net_fired = any(k in src for k in
                        ("pca_tiebreak", "physics_reject", "noise_reject"))
        row = {
            "tick": d["tick"],
            "lat": d.get("lat"),
            "lon": d.get("lon"),
            "v10": v10, "v6": v6, "diff": diff,
            "final": final,
            "conf_p": d.get("heading_conf"),
            "conf_s": d.get("shadow_cnn_confidence"),
            "source": src,
        }
        if diff > 15:
            disagree.append(row)
        elif net_fired and final is not None and ang_diff(v10, final) > 30:
            row["v10_vs_final"] = ang_diff(v10, final)
            agree_susp.append(row)

    disagree.sort(key=lambda r: -r["diff"])          # biggest disagreement first
    agree_susp.sort(key=lambda r: -r["v10_vs_final"])

    out = args.out or (sess / "cnn_disagreement.html")

    # Generate annotated frames with heading arrows overlaid.
    ann_dir = sess / "cnn_disagreement_annotated"
    ann_dir.mkdir(exist_ok=True)
    for r in disagree + agree_susp:
        src = sess / f"tick_{r['tick']:04d}.png"
        dst = ann_dir / f"tick_{r['tick']:04d}.png"
        if src.exists():
            annotate_frame(src, dst, r["v10"], r["v6"], r["final"])
    print(f"annotated {len(disagree) + len(agree_susp)} frames "
          f"→ {ann_dir}")

    # Build HTML.  Images referenced by relative path inside the session dir
    # so opening the report from the session dir shows the tick PNGs inline.
    def row_html(r, extra_col=None):
        img = f"cnn_disagreement_annotated/tick_{r['tick']:04d}.png"
        cf = r.get("conf_p") or 0
        cs = r.get("conf_s") or 0
        cells = [
            f"<td>{r['tick']}</td>",
            f"<td><img src='{img}' style='width:250px'></td>",
            f"<td>{r['lat']:.2f}, {r['lon']:.2f}</td>",
            f"<td>{r['v10']:.0f}°<br><small>conf {cf:.2f}</small></td>",
            f"<td>{r['v6']:.0f}°<br><small>conf {cs:.2f}</small></td>",
            f"<td>{r['diff']:.0f}°</td>",
            f"<td>{r['final']:.0f}°</td>",
            f"<td><small>{r['source']}</small></td>",
        ]
        if extra_col is not None:
            cells.insert(6, f"<td>{extra_col}</td>")
        return "<tr>" + "".join(cells) + "</tr>"

    def table_disagree(rows):
        hdr = ("<tr><th>tick</th><th>frame</th><th>lat,lon</th>"
               "<th>v10 (primary)</th><th>v6 (shadow)</th>"
               "<th>|Δ|</th><th>final</th><th>source</th></tr>")
        body = "\n".join(row_html(r) for r in rows)
        return f"<table>{hdr}{body}</table>"

    def table_susp(rows):
        hdr = ("<tr><th>tick</th><th>frame</th><th>lat,lon</th>"
               "<th>v10</th><th>v6</th><th>|Δ v10-v6|</th>"
               "<th>|Δ v10-final|</th><th>final</th><th>source</th></tr>")
        body = "\n".join(row_html(r, extra_col=f"{r['v10_vs_final']:.0f}°")
                         for r in rows)
        return f"<table>{hdr}{body}</table>"

    bins = {"15-30°": 0, "30-60°": 0, "60-90°": 0,
            "90-135°": 0, "135-180°": 0}
    for r in disagree:
        d = r["diff"]
        if d < 30:   bins["15-30°"] += 1
        elif d < 60: bins["30-60°"] += 1
        elif d < 90: bins["60-90°"] += 1
        elif d < 135:bins["90-135°"] += 1
        else:        bins["135-180°"] += 1
    bin_html = "".join(f"<li>{k}: {v}</li>" for k, v in bins.items())

    html = f"""<!doctype html>
<meta charset='utf-8'>
<title>CNN v10 vs v6 disagreement — {sess.name}</title>
<style>
 body {{ font-family: monospace; background:#111; color:#eee;
        margin:20px; }}
 h1,h2 {{ color:#ffcc33; }}
 table {{ border-collapse: collapse; margin-top: 12px; }}
 th, td {{ border:1px solid #444; padding:6px 10px; vertical-align:top;
          text-align:left; }}
 th {{ background:#222; color:#ffcc33; }}
 tr:nth-child(even) td {{ background:#181818; }}
 img {{ image-rendering: pixelated; display:block; }}
 small {{ color:#888; }}
 .summary {{ background:#181818; padding:10px 20px; border:1px solid #333;
             display:inline-block; }}
</style>

<h1>CNN v10 (primary) vs v6 (shadow) — voyage disagreement report</h1>
<p>Session: <code>{sess.name}</code></p>
<p><b>Arrow legend</b>:
   <span style='color:#ffcc33'>yellow = v10 (primary)</span> ·
   <span style='color:#ff8c00'>orange = v6 (shadow)</span> ·
   <span style='color:#33ddff'>cyan = final heading (after safety net)</span>
</p>

<div class='summary'>
 <p><b>Category A — disagree >15°</b>: {len(disagree)} ticks
    (~{100*len(disagree)/max(1,sum(1 for _ in trace_path.open())):.1f}% of voyage)</p>
 <p><b>Category B — agree but safety-net fired w/ final flipped >30°
    from CNN raw</b>: {len(agree_susp)} ticks</p>
 <p><b>Δ distribution (Category A):</b><ul style='margin:0'>{bin_html}</ul></p>
</div>

<h2>A · Disagreement &gt;15° ({len(disagree)} ticks, sorted by |Δ| desc)</h2>
<p>Rows where v10 and v6 give meaningfully different bearings.
   Yellow arrow in tick_viewer = v10. Compare with shadow-CNN's numeric read.</p>
{table_disagree(disagree)}

<h2>B · Agree but safety-net fired ({len(agree_susp)} ticks)</h2>
<p>Both CNNs agreed on a bearing but a downstream safety net
   (pca_tiebreak / physics_reject / noise_reject) flipped or shifted the
   final heading by &gt;30°. These are cases where <em>both</em> v10 and v6
   are confidently wrong the same way — a shared training-data gap.
   Sorted by how far the final heading diverged from the raw CNN read.</p>
{table_susp(agree_susp)}
"""
    out.write_text(html)
    print(f"wrote {out}")
    print(f"disagree: {len(disagree)}, agree_suspicious: {len(agree_susp)}")


if __name__ == "__main__":
    main()
