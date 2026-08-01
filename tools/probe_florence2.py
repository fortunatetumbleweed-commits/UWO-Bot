"""Probe Florence-2 on saved minimap frames.

Florence-2 is a Microsoft task-conditioned vision model.  Open
weights, ~0.23B (base) or ~0.77B (large) params, runs locally on
Mac mini via PyTorch+MPS.  Each call takes a task token + image
(and optionally a text query) and returns structured output.

Tasks tested:
  <MORE_DETAILED_CAPTION>      — long natural-language scene description
  <OPEN_VOCABULARY_DETECTION>  — finds objects matching a text query,
                                 returns bounding boxes + labels
  <REFERRING_EXPRESSION_SEGMENTATION>
                               — returns a binary mask for the
                                 referenced phrase (the direct L2
                                 replacement candidate)

Usage
─────
  python -m tools.probe_florence2 \\
      data/sessions/ai_nav_live_palma \\
      --ticks 1,35,90,149,187
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


_MODEL = None
_PROCESSOR = None
_DEVICE = None


def _ensure_model(model_id: str = "microsoft/Florence-2-base"):
    """Lazy-load the model once.  Returns (model, processor, device)."""
    global _MODEL, _PROCESSOR, _DEVICE
    if _MODEL is not None:
        return _MODEL, _PROCESSOR, _DEVICE
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor

    _DEVICE = torch.device("mps") if torch.backends.mps.is_available() \
              else torch.device("cpu")
    dtype = torch.float32   # mps is happier with float32 for now
    print(f"[florence] loading {model_id} on {_DEVICE} (dtype={dtype}) …")
    t0 = time.time()
    # Florence needs trust_remote_code=True (custom modeling code in repo).
    _MODEL = AutoModelForCausalLM.from_pretrained(
        model_id, torch_dtype=dtype, trust_remote_code=True,
    ).to(_DEVICE).eval()
    _PROCESSOR = AutoProcessor.from_pretrained(
        model_id, trust_remote_code=True,
    )
    print(f"[florence] loaded in {time.time() - t0:.1f}s")
    return _MODEL, _PROCESSOR, _DEVICE


def run_task(image, task: str, text_input: str = "") -> tuple[dict, float]:
    """Run a single Florence-2 task.  Returns (parsed_output, wall_seconds).

    `task` is a Florence task token like "<CAPTION>",
    "<MORE_DETAILED_CAPTION>", "<OPEN_VOCABULARY_DETECTION>",
    "<REFERRING_EXPRESSION_SEGMENTATION>", etc.
    `text_input` is the query for tasks that take one.
    """
    import torch
    model, processor, device = _ensure_model()
    prompt = task if not text_input else f"{task} {text_input}"
    inputs = processor(text=prompt, images=image, return_tensors="pt").to(device)
    t0 = time.time()
    with torch.no_grad():
        gen = model.generate(
            input_ids=inputs["input_ids"],
            pixel_values=inputs["pixel_values"],
            max_new_tokens=1024,
            do_sample=False,
            num_beams=3,
        )
    raw_text = processor.batch_decode(gen, skip_special_tokens=False)[0]
    parsed = processor.post_process_generation(
        raw_text, task=task,
        image_size=(image.width, image.height),
    )
    return parsed, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--ticks", required=True,
                   help="Comma-separated tick numbers")
    ap.add_argument("--model", default="microsoft/Florence-2-base",
                   choices=("microsoft/Florence-2-base",
                            "microsoft/Florence-2-large"))
    args = ap.parse_args()

    from PIL import Image
    _ensure_model(args.model)

    ticks = [int(t) for t in args.ticks.split(",")]
    for t in ticks:
        img_path = args.session_dir / f"tick_{t:04d}.png"
        if not img_path.exists():
            print(f"\n=== t{t}: MISSING ===")
            continue
        img = Image.open(img_path).convert("RGB")
        print(f"\n=== t{t}  ({img.size[0]}×{img.size[1]}) ===")

        # 1. Detailed caption — does the model describe what's in the image?
        out, dt = run_task(img, "<MORE_DETAILED_CAPTION>")
        cap = out.get("<MORE_DETAILED_CAPTION>", "(none)")
        print(f"\n  [{dt:.2f}s] CAPTION:\n    {cap}")

        # 2. Open-vocabulary detection — find land
        out, dt = run_task(img, "<OPEN_VOCABULARY_DETECTION>",
                          text_input="whitish gray land")
        det = out.get("<OPEN_VOCABULARY_DETECTION>", {})
        print(f"\n  [{dt:.2f}s] DETECT 'whitish gray land':")
        labels = det.get("bboxes_labels", [])
        bboxes = det.get("bboxes", [])
        if not bboxes:
            print("    (no detections)")
        else:
            for lbl, bbox in zip(labels, bboxes):
                x1, y1, x2, y2 = [round(v, 1) for v in bbox]
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                w, h = x2 - x1, y2 - y1
                print(f"    {lbl}: bbox=({x1},{y1},{x2},{y2})  "
                      f"center=({cx:.0f},{cy:.0f})  size=({w:.0f}×{h:.0f})")

        # 3. Referring-expression segmentation — give it the phrase, get a mask
        out, dt = run_task(img, "<REFERRING_EXPRESSION_SEGMENTATION>",
                          text_input="the solid whitish gray land area")
        seg = out.get("<REFERRING_EXPRESSION_SEGMENTATION>", {})
        polys = seg.get("polygons", [])
        labels = seg.get("labels", [])
        print(f"\n  [{dt:.2f}s] SEG 'the solid whitish gray land area':")
        if not polys:
            print("    (no polygons)")
        else:
            for lbl, poly in zip(labels, polys):
                # poly is a list of polygons (one segment = list of (x,y))
                # Each is a flat [x1,y1,x2,y2,...]
                if not poly:
                    continue
                pts = poly[0]
                xs = pts[0::2]; ys = pts[1::2]
                if not xs:
                    print(f"    {lbl}: empty polygon")
                    continue
                bb_x1, bb_x2 = min(xs), max(xs)
                bb_y1, bb_y2 = min(ys), max(ys)
                cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
                print(f"    {lbl}: {len(xs)} points  "
                      f"bbox=({bb_x1:.0f},{bb_y1:.0f}-{bb_x2:.0f},{bb_y2:.0f})  "
                      f"centroid=({cx:.0f},{cy:.0f})")


if __name__ == "__main__":
    main()
