"""Probe whether VLMs can describe land positions around the ship
using 8-direction compass labels with coarse distance categories.

Tests the "perception not decision" pattern: ask the VLM only what
it sees (land present at this direction?), not what to do.  The
steering decision is made downstream by the planner code from the
structured description, not by the VLM.

For each tick × model, asks the same 8-direction land prompt,
parses the response, and reports the result side-by-side with the
saved minimap.

Usage
─────
  python -m tools.probe_l4_land_directions \\
      data/sessions/ai_nav_live_palma \\
      --models moondream,llava:7b \\
      --ticks 1,35,90,149,187
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


DIRECTIONS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


PROMPT = """\
This is the top-down minimap from a sailing game. The green ship \
icon is in the center.

Water is TRANSLUCENT with WAVY decorative lines. Because water is \
see-through, colors from behind the minimap bleed through, so \
apparent color varies (bluish, brownish, tan, yellowish, …). The \
wavy line decoration plus translucency is the reliable signature \
of water.

Land is SOLID and OPAQUE — no wavy lines, nothing bleeds through. \
Land is typically whitish-gray.

Ignore: yellow circular sprites (other ships and NPCs), white \
diamond markers (port indicators), and text labels. They are not \
land.

Looking outward from the green ship at the center, for each of the \
8 compass directions, tell me how close the nearest land is in \
that direction. Use one of these labels:
  close  — land within one-third of the way to the frame edge
  far    — land near the frame edge
  none   — no land visible in that direction, only water out to the edge

Reply with exactly 8 lines in this format (and nothing else):
N: <close/far/none>
NE: <close/far/none>
E: <close/far/none>
SE: <close/far/none>
S: <close/far/none>
SW: <close/far/none>
W: <close/far/none>
NW: <close/far/none>
"""


def parse_8dir(text: str) -> dict[str, str | None]:
    """Parse the 8-direction response.  Returns {dir: 'close'|'far'|'none'|None}."""
    out: dict[str, str | None] = {d: None for d in DIRECTIONS}
    if not text:
        return out
    for line in text.splitlines():
        m = re.match(r"\s*(N|NE|E|SE|S|SW|W|NW)\s*[:=]\s*(close|far|none)",
                     line.strip(), re.I)
        if m:
            d = m.group(1).upper()
            v = m.group(2).lower()
            out[d] = v
    # If structured format failed, try loose keyword extraction
    # (one direction-label per line is the most likely failure mode)
    if all(v is None for v in out.values()):
        for d in DIRECTIONS:
            m = re.search(rf"\b{d}\b.*?\b(close|far|none)\b", text, re.I | re.S)
            if m:
                out[d] = m.group(1).lower()
    return out


def _ask(model_name: str, img: Image.Image) -> str:
    from vision.local_vision import LocalVision
    v = LocalVision(model=model_name)
    if not v.check_available():
        return "[model unavailable]"
    try:
        return v.ask(PROMPT, frame=img)
    except Exception as e:
        return f"[error: {e}]"


def _summary(d: dict[str, str | None]) -> str:
    """One-line per-direction summary, color-coded as text."""
    return " | ".join(
        f"{k}={str(v or '?'):>5s}" for k, v in d.items()
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--models", required=True)
    ap.add_argument("--ticks", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",")]
    ticks = [int(t) for t in args.ticks.split(",")]

    trace = {
        json.loads(l)["tick"]: json.loads(l)
        for l in (args.session_dir / "trace.jsonl").read_text().splitlines()
        if l.strip()
    }

    n_rows = len(ticks)
    n_cols = 1 + len(models)
    fig = plt.figure(figsize=(4.0 + 5.5 * len(models), 3.4 * n_rows))
    gs = gridspec.GridSpec(
        n_rows, n_cols, figure=fig,
        width_ratios=[1.5] + [2.0] * len(models),
        hspace=0.35, wspace=0.15,
    )

    for row, tick in enumerate(ticks):
        rec = trace.get(tick, {})
        crop_path = args.session_dir / f"tick_{tick:04d}.png"
        if not crop_path.exists():
            continue
        raw_img = Image.open(crop_path).convert("RGB")
        sent_img = raw_img.copy()
        sent_img.thumbnail((800, 400))

        ax_img = fig.add_subplot(gs[row, 0])
        ax_img.imshow(np.asarray(sent_img))
        ax_img.set_xticks([]); ax_img.set_yticks([])
        ax_img.set_title(
            f"t{tick}  hdg={rec.get('heading_deg', 0):.0f}°\n"
            f"sent: {sent_img.size[0]}×{sent_img.size[1]}",
            fontsize=9,
        )

        for col, model in enumerate(models, start=1):
            raw = _ask(model, sent_img)
            parsed = parse_8dir(raw)

            ax_txt = fig.add_subplot(gs[row, col])
            ax_txt.axis("off")
            n_close = sum(1 for v in parsed.values() if v == "close")
            n_far = sum(1 for v in parsed.values() if v == "far")
            n_none = sum(1 for v in parsed.values() if v == "none")
            n_unparsed = sum(1 for v in parsed.values() if v is None)
            ax_txt.set_title(
                f"{model}   close:{n_close} far:{n_far} "
                f"none:{n_none} ?:{n_unparsed}",
                fontsize=9,
            )

            wrap = lambda s, w=46: "\n".join(textwrap.wrap(s, width=w)) \
                                   or "(empty)"
            body = (
                f"PARSED:\n  {_summary(parsed)}\n"
                f"{'─' * 50}\n"
                f"RAW:\n{wrap(raw or '(empty string)')}"
            )
            ax_txt.text(
                0.0, 1.0, body, fontsize=7.5, family="monospace",
                va="top", ha="left", transform=ax_txt.transAxes,
            )

    out = args.out or (args.session_dir / "l4_8dir.png")
    fig.tight_layout()
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"→ {out}")


if __name__ == "__main__":
    main()
