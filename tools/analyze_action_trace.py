"""Analyse an action-trace session frame-by-frame.

For each recorded tap, identify what was under the tap point (the element the bot
actually hit) and a coarse screen id, so you can see exactly what action was taken
on each frame — e.g. whether the Purchase commit button was ever tapped, or the
tap landed somewhere else.

    python -m tools.analyze_action_trace data/sessions/trace_<name>_<ts>/

Writes analysis.md (text) and report.html (marked frames inline) into the dir,
and prints the frame-by-frame lines.
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def _tokens(frame):
    from vision.ocr import _get_reader
    return _get_reader().readtext(np.array(frame), detail=1)   # [(bbox, text, conf)]


def _nearest(tokens, x, y):
    """(token_under_point, nearest_token) — each (text, conf, cx, cy, dist)."""
    inside = None
    best = None
    best_d = 1e18
    for bbox, text, conf in tokens:
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        d = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if x1 <= x <= x2 and y1 <= y <= y2:
            inside = (text, conf, int(cx), int(cy), 0)
        if d < best_d:
            best_d = d
            best = (text, conf, int(cx), int(cy), int(d))
    return inside, best


def _screen_id(tokens):
    """Top-left title token as a coarse screen identity."""
    cand = []
    for bbox, text, conf in tokens:
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        if min(ys) < 95 and min(xs) < 420 and conf > 0.4 and len(text.strip()) > 1:
            cand.append((min(xs), text.strip()))
    cand.sort()
    return cand[0][1] if cand else "?"


def main() -> None:
    d = Path(sys.argv[1])
    rows = [l for l in (d / "actions.jsonl").read_text().splitlines() if l.strip()]
    md = [f"# Action trace — {d.name}", f"{len(rows)} action(s)", ""]
    html = ["<html><meta charset='utf-8'><body style='font-family:monospace'>",
            f"<h2>{d.name} — {len(rows)} action(s)</h2>"]
    for l in rows:
        e = json.loads(l)
        frame = Image.open(d / e["frame"]).convert("RGB")
        toks = _tokens(frame)
        screen = _screen_id(toks)
        if e["kind"] == "back":
            desc = "BACK key"
        else:
            inside, best = _nearest(toks, e["x"], e["y"])
            if inside:
                desc = f"ON '{inside[0]}'"
            elif best:
                desc = f"near '{best[0]}' (d={best[4]}px) — nothing under the point"
            else:
                desc = "(no text near tap)"
        label = f" [{e['label']}]" if e.get("label") else ""
        line = (f"[{e['idx']:04d}] {e['t']} screen≈{screen!r}  "
                f"{e['kind']}({e['x']},{e['y']}){label} → {desc}")
        md.append(line)
        img = e["frame"].replace(".png", "_marked.png") if e["kind"] == "tap" else e["frame"]
        html.append(f"<div style='margin:10px 0;border-top:1px solid #ccc'>"
                    f"<b>{line}</b><br><img src='{img}' width='680'></div>")
    html.append("</body></html>")
    (d / "analysis.md").write_text("\n".join(md))
    (d / "report.html").write_text("\n".join(html))
    print(f"wrote {d / 'analysis.md'} and {d / 'report.html'}\n")
    print("\n".join(md[3:]))


if __name__ == "__main__":
    main()
