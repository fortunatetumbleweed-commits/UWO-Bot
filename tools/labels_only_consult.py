#!/usr/bin/env python
"""Experiment: feed Claude ONLY the OmniParser+OCR labels (no image) +
the active goal, and ask "what should I tap?"  Useful for measuring
how much the visual channel adds over pure-text reasoning, and for
sanity-checking the architectural alternative of "skip the image, send
labels only".

Usage:
    python -m tools.labels_only_consult <png>

Reads the frame, runs OmniParser + OCR, builds a labels-only
prompt with the active goal, sends to Claude, prints the response.

Set ANTHROPIC_API_KEY for the call to actually fire.  Without the key
the script prints the prompt that would have been sent, so you can
inspect what the LLM gets to work with.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image
from loguru import logger


GOAL = (
    "I am blocked by 'Not Enough Crew' on the harbour Departure tab. "
    "I need to recruit crew to reach the minimum required for sailing. "
    "What is the next button I should tap?  Answer with: "
    "(1) the exact label of the button, (2) its (cx,cy) coordinates, "
    "(3) one-line reason."
)


def _build_prompt(elements, ocr_tokens, goal: str) -> str:
    lines = [
        "I am an autonomous bot playing 'Uncharted Waters Origin'.",
        "I am stuck on a screen.  I cannot send you the image — only the "
        "labels and positions of UI elements that my screen-parser detected.",
        "",
        f"Active goal: {goal}",
        "",
        "Screen resolution: 2400×1080.",
        "",
        "OmniParser elements (label, element_type, centre, bbox):",
    ]
    for i, el in enumerate(elements):
        lines.append(
            f"  [{i:02d}] type={el.element_type:6s} "
            f"label={el.label!r}  "
            f"cx={el.cx} cy={el.cy}  "
            f"bbox=({el.x1},{el.y1},{el.x2},{el.y2})  "
            f"conf={el.confidence:.2f}"
        )
    lines.append("")
    lines.append("OCR tokens (text, conf, centre):")
    for i, (text, conf, cx, cy) in enumerate(ocr_tokens):
        lines.append(f"  [{i:02d}] {text!r}  conf={conf:.2f}  ({cx},{cy})")
    lines.append("")
    lines.append(
        "Recommend ONE next tap.  If no relevant button exists, say so "
        "and suggest pressing Back instead."
    )
    return "\n".join(lines)


def _consult_claude(prompt: str) -> str:
    import anthropic  # type: ignore
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "(ANTHROPIC_API_KEY not set — skipping API call)"
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.split("Usage:", 1)[1].splitlines()[1].strip(), file=sys.stderr)
        return 2
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    png = Path(sys.argv[1]).expanduser().resolve()
    if not png.exists():
        print(f"error: {png} not found", file=sys.stderr)
        return 2

    frame = Image.open(png).convert("RGB")
    from vision.omniparser import parse_fast_cached
    from actions.sail_actions import _ocr_frame, clear_ocr_frame_cache
    clear_ocr_frame_cache()
    elements = parse_fast_cached(frame)
    tokens = _ocr_frame(frame, min_conf=0.30)

    prompt = _build_prompt(elements, tokens, GOAL)
    print(f"=== PROMPT ({len(prompt)} chars, sent to Claude as text only) ===")
    print(prompt)
    print()
    print("=== CLAUDE RESPONSE ===")
    print(_consult_claude(prompt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
