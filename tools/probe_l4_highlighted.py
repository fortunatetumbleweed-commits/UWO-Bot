"""Send V11-highlighted minimap frames to VLMs and see if explicit
land-highlighting unblocks the perception layer.

Pipeline per tick:
  1. Load raw minimap
  2. Run V11 mask (`channel_mask_from_rgb`) → boolean water mask
  3. Compose a highlighted frame:
      - water region: unchanged
      - land region: blended with bright red so it's visually obvious
      - draw compass tick labels at the frame edges (N/E/S/W)
  4. Send highlighted frame to VLMs with a much simpler prompt
     ("where are the RED regions relative to the center?")
  5. Render image + highlighted + each model's answer side-by-side

If the highlighting works, this is the cheapest way to get usable
L4 perception without fine-tuning or pulling a bigger model.

Usage
─────
  python -m tools.probe_l4_highlighted \\
      data/sessions/ai_nav_live_palma \\
      --models moondream,llava:7b \\
      --ticks 1,35,90,149,187
"""
from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from PIL import Image, ImageDraw, ImageFont


PROMPT = """\
This is a top-down minimap.  Land has been highlighted in BRIGHT \
RED.  Everything else is water.  Compass directions are labeled at \
the frame edges: N (top), E (right), S (bottom), W (left).  The \
green ship icon is in the center of the map.

Looking outward from the green ship, for each of the 8 compass \
directions (N, NE, E, SE, S, SW, W, NW), tell me whether you see \
RED land in that direction.

Reply with exactly 8 lines in this format (and nothing else):
N: <yes/no>
NE: <yes/no>
E: <yes/no>
SE: <yes/no>
S: <yes/no>
SW: <yes/no>
W: <yes/no>
NW: <yes/no>
"""


def highlight_land(rgb: np.ndarray) -> Image.Image:
    """Run V11 mask, blend bright red over land, bright blue over
    water, draw compass labels.  Binary recoloring lets us read
    V11's segmentation directly — every pixel is either red (land)
    or blue (water).
    """
    from tools.centerline_extraction_prototype import channel_mask_from_rgb
    rgb_trim, water_mask = channel_mask_from_rgb(rgb)

    H, W = water_mask.shape
    land = ~water_mask

    out = rgb_trim.copy().astype(np.float32)
    red  = np.array([255,   0,   0], dtype=np.float32)
    blue = np.array([  0, 100, 255], dtype=np.float32)
    alpha = 0.65
    out[land]       = out[land]       * (1 - alpha) + red  * alpha
    out[water_mask] = out[water_mask] * (1 - alpha) + blue * alpha

    pil = Image.fromarray(out.clip(0, 255).astype(np.uint8))

    # Draw compass labels at edges.
    draw = ImageDraw.Draw(pil)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
    except Exception:
        font = ImageFont.load_default()
    margin = 4
    labels = [
        ("N", W // 2 - 8, margin),
        ("S", W // 2 - 8, H - 22),
        ("E", W - 18, H // 2 - 10),
        ("W", margin, H // 2 - 10),
    ]
    for txt, x, y in labels:
        # White text with black outline for visibility
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            draw.text((x + dx, y + dy), txt, fill=(0, 0, 0), font=font)
        draw.text((x, y), txt, fill=(255, 255, 255), font=font)

    return pil


def ask_model(model_name: str, img: Image.Image) -> str:
    from vision.local_vision import LocalVision
    v = LocalVision(model=model_name)
    if not v.check_available():
        return "[model unavailable]"
    try:
        return v.ask(PROMPT, frame=img)
    except Exception as e:
        return f"[error: {e}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--models", required=True)
    ap.add_argument("--ticks", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",")]
    ticks = [int(t) for t in args.ticks.split(",")]

    n_rows = len(ticks)
    n_cols = 2 + len(models)   # raw + highlighted + one per model

    fig = plt.figure(figsize=(4.0 + 4.5 * (n_cols - 1), 3.0 * n_rows))
    gs = gridspec.GridSpec(
        n_rows, n_cols, figure=fig,
        width_ratios=[1.2, 1.2] + [1.8] * len(models),
        hspace=0.35, wspace=0.15,
    )

    for row, tick in enumerate(ticks):
        path = args.session_dir / f"tick_{tick:04d}.png"
        if not path.exists():
            continue
        rgb_raw = np.asarray(Image.open(path).convert("RGB"))

        # raw
        ax_raw = fig.add_subplot(gs[row, 0])
        ax_raw.imshow(rgb_raw)
        ax_raw.set_xticks([]); ax_raw.set_yticks([])
        ax_raw.set_title(f"t{tick} — raw", fontsize=9)

        # highlighted
        highlighted = highlight_land(rgb_raw)
        highlighted_for_vlm = highlighted.copy()
        # Thumbnail for VLM (mirror MoondreamTactical).
        highlighted_for_vlm.thumbnail((800, 400))
        ax_h = fig.add_subplot(gs[row, 1])
        ax_h.imshow(np.asarray(highlighted))
        ax_h.set_xticks([]); ax_h.set_yticks([])
        ax_h.set_title("V11-highlighted (sent to VLMs)", fontsize=9)

        # one column per model
        for col, model in enumerate(models, start=2):
            raw = ask_model(model, highlighted_for_vlm)
            ax_txt = fig.add_subplot(gs[row, col])
            ax_txt.axis("off")
            ax_txt.set_title(model, fontsize=10)
            body = (
                f"ANSWER:\n{textwrap.fill(raw or '(empty string)', width=46) or '(empty)'}"
            )
            ax_txt.text(0.0, 1.0, body, fontsize=8, family="monospace",
                        va="top", ha="left", transform=ax_txt.transAxes)

    out = args.out or (args.session_dir / "l4_highlighted.png")
    fig.tight_layout()
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"→ {out}")


if __name__ == "__main__":
    main()
