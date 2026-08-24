# tools/visual_report.py
# Reusable "annotated frames → standalone HTML" diagnostic report.
#
# The pattern behind the one-off debug reports that proved decisive in the 2026-08-20
# village-navigation hunt (/tmp/village_find_report.html: each frame with the parser's
# accepted area drawn as a box and every candidate label colour-coded KEPT vs DROPPED,
# which made the fixed-crop bug visible at a glance).  Codified so future perception
# debugging doesn't reinvent it.
#
# Usage:
#     from tools.visual_report import annotate, Section, build_report
#     img = annotate(frame, boxes=[(x1,y1,x2,y2,"#51cf66","Market — KEPT")],
#                    rects=[(50,80,1860,950,"#8ab4f8","parser MAP AREA")])
#     build_report("Why the label was dropped",
#                  [Section("frame_0042", img,
#                           lines=[("'Apache Village' @ (2094,152) → DROPPED", "bad"),
#                                  ("parse_visible_ports returned []", "info")])],
#                  "/tmp/report.html")
#
# Everything is embedded (base64), so the HTML is a single self-contained file that
# survives being moved/shared.  Images are downscaled for size; tune `width`.

from __future__ import annotations

import base64
import html as _html
import io
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


def _font(size: int = 26, bold: bool = True):
    names = (["Arial Bold.ttf", "Arial.ttf"] if bold else ["Arial.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(f"/System/Library/Fonts/Supplemental/{n}", size)
        except Exception:
            continue
    return ImageFont.load_default()


def annotate(frame: Image.Image,
             boxes: Optional[list] = None,
             rects: Optional[list] = None,
             points: Optional[list] = None,
             font_size: int = 26) -> Image.Image:
    """Draw diagnostics on a COPY of `frame`.

    boxes  : [(x1,y1,x2,y2, css_color, label)]      — element boxes, label above.
    rects  : [(x1,y1,x2,y2, css_color, label)]      — region outlines (thicker), label inside.
    points : [(x, y, css_color, label)]             — tap markers (circle), label beside.
    """
    img = frame.convert("RGB").copy()
    d = ImageDraw.Draw(img)
    f = _font(font_size)
    for x1, y1, x2, y2, color, label in rects or []:
        d.rectangle([x1, y1, x2, y2], outline=color, width=5)
        if label:
            d.text((x1 + 8, y1 + 6), label, font=f, fill=color)
    for x1, y1, x2, y2, color, label in boxes or []:
        d.rectangle([x1, y1, x2, y2], outline=color, width=4)
        if label:
            d.text((x1, max(0, y1 - font_size - 6)), label, font=f, fill=color)
    for x, y, color, label in points or []:
        d.ellipse([x - 10, y - 10, x + 10, y + 10], outline=color, width=4)
        if label:
            d.text((x + 14, y - font_size // 2), label, font=f, fill=color)
    return img


@dataclass
class Section:
    heading: str
    image: Optional[Image.Image] = None
    lines: list = field(default_factory=list)   # [(text, kind)] kind ∈ good|bad|warn|info
    width: int = 1200                           # embedded display width (downscale)


_CSS = """
body{background:#181a1f;color:#ddd;font-family:monospace;margin:24px}
.f{margin:24px 0;padding:12px;background:#22252b;border-radius:8px}
img{border-radius:6px;max-width:100%}
.good{color:#51cf66}.bad{color:#ff6b6b}.warn{color:#ffd43b}.info{color:#aaa}
h1{color:#e8eaed}h3{color:#8ab4f8}p{color:#bbb}
"""


def build_report(title: str, sections: list, out_path: str,
                 intro: str = "") -> str:
    """Write a self-contained HTML report; returns `out_path`."""
    parts = [f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>",
             f"<h1>{_html.escape(title)}</h1>"]
    if intro:
        parts.append(f"<p>{intro}</p>")                       # intro may carry markup
    for s in sections:
        parts.append(f"<div class=f><h3>{_html.escape(s.heading)}</h3>")
        for text, kind in s.lines:
            parts.append(f"<div class={kind}>{_html.escape(text)}</div>")
        if s.image is not None:
            im = s.image
            if im.width > s.width:
                im = im.resize((s.width, int(im.height * s.width / im.width)))
            buf = io.BytesIO()
            im.save(buf, "PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            parts.append(f"<img src='data:image/png;base64,{b64}'>")
        parts.append("</div>")
    parts.append("</body></html>")
    with open(out_path, "w") as fh:
        fh.write("\n".join(parts))
    return out_path
