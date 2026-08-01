"""Visualize the exact image + prompt + response for each L4 VLM call.

For each selected tick:
  - load the saved minimap, thumbnail to the same size MoondreamTactical
    sends (800 × 400 max)
  - compose the same prompt MoondreamTactical builds (with the tick's
    real heading direction + side)
  - ask the model and capture the raw answer
  - render a single multi-panel PNG showing image + prompt + response
    per tick, side-by-side for any number of models

Useful for understanding *why* a model said what it said — see
exactly what pixels and text it was given.

Usage
─────
  python -m tools.show_l4_vlm_calls \\
      data/sessions/ai_nav_live_palma \\
      --models moondream,llava:7b \\
      --ticks 1,35,90,149,187
"""
from __future__ import annotations

import argparse
import json
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

from brain.ai_nav.layers.tactical import (  # noqa: E402
    _MOONDREAM_PROMPT_TEMPLATE, _bearing_to_compass_dir, parse_direction,
)


def _build_prompt(heading_deg: float, side: str) -> str:
    return _MOONDREAM_PROMPT_TEMPLATE.format(
        heading_dir=_bearing_to_compass_dir(heading_deg),
        heading_deg=heading_deg,
        side=side,
    )


def _thumbnail(frame: Image.Image) -> Image.Image:
    """Mirror what MoondreamTactical._consult does."""
    img = frame.copy()
    img.thumbnail((800, 400))
    return img


def _ask(model_name: str, img: Image.Image, prompt: str) -> str:
    from vision.local_vision import LocalVision
    v = LocalVision(model=model_name)
    if not v.check_available():
        return "[model unavailable]"
    try:
        return v.ask(prompt, frame=img)
    except Exception as e:
        return f"[error: {e}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--models", required=True,
                   help="Comma-separated model names")
    ap.add_argument("--ticks", required=True,
                   help="Comma-separated tick numbers")
    ap.add_argument("--side", choices=("port", "starboard"), default="port")
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
    n_cols = 1 + len(models)   # image column + one column per model

    fig = plt.figure(figsize=(4.0 + 4.8 * len(models), 3.0 * n_rows))
    gs = gridspec.GridSpec(
        n_rows, n_cols, figure=fig,
        width_ratios=[1.5] + [1.8] * len(models),
        hspace=0.35, wspace=0.15,
    )

    for row, tick in enumerate(ticks):
        rec = trace.get(tick, {})
        heading_deg = rec.get("heading_deg", 0.0)
        crop_path = args.session_dir / f"tick_{tick:04d}.png"
        if not crop_path.exists():
            continue

        raw_img = Image.open(crop_path).convert("RGB")
        sent_img = _thumbnail(raw_img)
        prompt = _build_prompt(heading_deg, args.side)

        # Image column
        ax_img = fig.add_subplot(gs[row, 0])
        ax_img.imshow(np.asarray(sent_img))
        ax_img.set_xticks([]); ax_img.set_yticks([])
        ax_img.set_title(
            f"t{tick}  hdg={heading_deg:.0f}° ({_bearing_to_compass_dir(heading_deg)})\n"
            f"sent to model: {sent_img.size[0]}×{sent_img.size[1]}",
            fontsize=9,
        )

        # One column per model with prompt + response
        for col, model in enumerate(models, start=1):
            raw = _ask(model, sent_img, prompt)
            parsed = parse_direction(raw) or "—"

            ax_txt = fig.add_subplot(gs[row, col])
            ax_txt.axis("off")

            # Title: model name + parsed verdict, color-coded
            color = {"left": "#cc3300", "right": "#cc3300",
                     "forward": "#225522", "back": "#cc3300"}.get(
                parsed, "#666666")
            ax_txt.set_title(f"{model}  → parsed: {parsed}",
                             fontsize=10, color=color)

            # Body: prompt (wrapped) + horizontal rule + response (wrapped)
            wrap = lambda s, w=42: "\n".join(textwrap.wrap(s, width=w)) or "(empty)"
            body = (
                f"PROMPT:\n{wrap(prompt)}\n"
                f"{'─' * 42}\n"
                f"ANSWER:\n{wrap(raw or '(empty string)')}"
            )
            ax_txt.text(0.0, 1.0, body, fontsize=7.5, family="monospace",
                        va="top", ha="left", wrap=True,
                        transform=ax_txt.transAxes)

    out = args.out or (args.session_dir / "l4_vlm_calls.png")
    fig.tight_layout()
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"→ {out}")


if __name__ == "__main__":
    main()
