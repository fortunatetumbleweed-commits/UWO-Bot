"""Render an HTML preview of the new keep_bow_tip / keep_stern_end
synthesis strategies alongside the existing occlusion strategies for
comparison.

Rows = strategies.  Cols = target headings (0/45/90/135/180/225/270/315°).
Each cell is a synthesized 80×80 example upscaled ×3 with a compass
arrow overlaid at the true heading and the ship's visibility fraction
in the caption.

Output: data/synth_strategies_preview.html + embedded PNGs.
"""
from __future__ import annotations

import base64
import io
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.ai_nav.learned.heading_synthesis import synthesize

SNAP = REPO / "data/heading_training_snapshot_v5_2026-07-01"
BGS = REPO / "data/heading_backgrounds_clean_2026-07-02"

STRATEGIES = [
    "none",
    "center_band",
    "random_polygons",
    "keep_front_third",
    "keep_back_third",
    "keep_front_quarter",
    "keep_back_quarter",
    "pirate_boss_bow",
    "pirate_boss_stern",
    "text_overlay",
]
NEW = {"pirate_boss_bow", "pirate_boss_stern", "text_overlay"}
ANGLES = [0, 45, 90, 135, 180, 225, 270, 315]


def _draw_arrow(img: Image.Image, heading_deg: float) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out, "RGBA")
    W, H = out.size
    cx, cy = W // 2, H // 2
    r = min(W, H) // 3
    th = math.radians(heading_deg)
    x2, y2 = cx + r * math.sin(th), cy - r * math.cos(th)
    d.line([(cx, cy), (x2, y2)], fill=(255, 200, 40, 240), width=3)
    for sign in (-1, 1):
        ah = th + sign * math.radians(25) + math.pi
        ax = x2 + 10 * math.sin(ah)
        ay = y2 - 10 * math.cos(ah)
        d.line([(x2, y2), (int(ax), int(ay))],
               fill=(255, 200, 40, 240), width=2)
    return out


def _b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def main():
    manifest = [json.loads(l) for l in
                (SNAP / "sprites/manifest.jsonl").open()]
    # Pick a canonical high-quality sprite
    canon = max((r for r in manifest
                 if 150 <= r["heading_deg"] < 180
                 and r.get("largest_component_frac", 0) > 0.85),
                key=lambda r: r.get("n_green", 0))
    print(f"canonical sprite: {canon['sprite_id']} "
          f"src={canon['heading_deg']:.0f}°")

    sprite = np.asarray(
        Image.open(SNAP / "sprites" / f"sprite_{canon['sprite_id']}.png"
                   ).convert("RGBA"))
    bg_paths = sorted(BGS.glob("bg_*.png"))[98:]     # procedural only
    bgs = [np.asarray(Image.open(p).convert("RGB"))
           for p in bg_paths[:16]]

    random.seed(0)
    np.random.seed(0)

    cell_size = 200
    rows_html = []
    for strat in STRATEGIES:
        cells = [f"<td class='label'>{strat}"
                 f"{' <span class=\"new\">NEW</span>' if strat in NEW else ''}"
                 f"</td>"]
        for angle in ANGLES:
            rot_deg = (angle - canon["heading_deg"]) % 360
            bg = random.choice(bgs)
            ex = synthesize(sprite, canon["heading_deg"], bg,
                            rotation_deg=rot_deg, strategy=strat)
            img = ex.image.resize((cell_size, cell_size), Image.NEAREST)
            img = _draw_arrow(img, ex.heading_deg)
            b64 = _b64(img)
            cells.append(
                f"<td><img src='data:image/png;base64,{b64}'>"
                f"<div class='caption'>{angle}° · vis {ex.visibility_frac:.0%}</div>"
                f"</td>"
            )
        rows_html.append("<tr>" + "".join(cells) + "</tr>")

    hdr = ("<tr><th>strategy</th>"
           + "".join(f"<th>{a}°</th>" for a in ANGLES)
           + "</tr>")

    html = f"""<!doctype html>
<meta charset='utf-8'>
<title>Synth strategy preview — v11 candidate</title>
<style>
 body {{ font-family: system-ui, monospace; background:#111; color:#eee;
        margin:20px; }}
 h1 {{ color:#ffcc33; }}
 h2 {{ color:#eee; }}
 table {{ border-collapse: collapse; margin-top: 12px; }}
 th, td {{ border:1px solid #333; padding:4px; vertical-align:top;
          text-align:center; }}
 th {{ background:#222; color:#ffcc33; font-family: monospace; }}
 td.label {{ background:#181818; color:#ffcc33; font-family: monospace;
            text-align:left; padding:8px 12px; min-width: 160px; }}
 img {{ image-rendering: pixelated; display:block; margin:auto; }}
 .caption {{ color:#aaa; font-size:11px; font-family:monospace; margin-top:2px; }}
 .new {{ background:#ffcc33; color:#111; padding:1px 6px;
        border-radius:3px; font-size:10px; margin-left:4px; }}
 .note {{ background:#181818; padding:14px 20px; border:1px solid #333;
         max-width:900px; }}
</style>

<h1>Synthesis strategy preview — v11 candidate mix</h1>

<div class='note'>
 <p>Canonical sprite <code>{canon['sprite_id']}</code>
    (source heading {canon['heading_deg']:.0f}°) rendered at 8 target
    angles under each strategy.  Yellow arrow shows the ground-truth
    heading the CNN should learn.  Backgrounds sampled from the
    procedural water pool (720 clean water bgs).</p>
 <p><b>Four new strategies</b> added on top of v10's mix — the
    named fraction of the ship is KEPT visible; the rest of the body
    is occluded:</p>
 <ul>
   <li><code>keep_front_third</code> — front 1/3 (bow tip + near-bow)
       preserved intact.  Occludes the back 2/3.</li>
   <li><code>keep_back_third</code> — back 1/3 (stern end + near-stern)
       preserved intact.  Occludes the front 2/3.</li>
   <li><code>keep_front_quarter</code> / <code>keep_back_quarter</code>
       — tighter version, only the front/back 1/4 stays visible.</li>
 </ul>
 <p>Motivation: production heavy-occlusion failures (t83, t84, t556, t557
    in the Cairo→9.7,33.08 voyage) show both v10 and v6 collapsing to a
    N/S axis under pirate-NPC occlusion.  Bow tip and stern end are the
    two most direction-discriminating features on the ship; either alone
    is enough for a human to call the heading.  Fractional-end retention
    (as opposed to a small disc which truncates the tip) guarantees the
    surviving endpoint is complete.</p>
 <p>Strategy weights: none 16%, front/back/L/R_half 4-6% each,
    center_band 8%, random blobs/polys/rects 8-12% each,
    <b>keep_front/back_third 6% each,
    keep_front/back_quarter 6% each</b>.</p>
</div>

<h2>Grid</h2>
<table>{hdr}{''.join(rows_html)}</table>
"""
    out = REPO / "data/synth_strategies_preview.html"
    out.write_text(html)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
