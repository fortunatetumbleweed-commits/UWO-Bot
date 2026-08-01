#!/usr/bin/env python
"""Dump the perception pipeline's output at every layer for a list of
frames.  Used to inspect what information the bot actually extracts from
a screen — useful when diagnosing why a screen isn't recognised, why a
drain picked the wrong button, or why a fingerprint missed.

Usage:
    python -m tools.perceive_dump <png> [<png> ...]

Each frame is run through:
  L0 — capture (just loads from disk here)
  L1 — OmniParser parse_fast (cached) → element list
  L2 — EasyOCR full-frame             → token list
  L3 — Chrome detector                → flags
  L4 — Fingerprint registry classify  → state + detail + matched signals
  L5 — Interruptor detection          → list of interruptor ids
  L6 — Active flow detection          → flow + step
  L7 — find_positive_button_for_context with example goal_keywords
       (context-aware drain preview)
  L8 — Full perceive() pipeline       → final PerceiveResult

A short caption (file path + label tag if present) is printed before each
frame.  Output is plain text — pipe to a file when frames are noisy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image
from loguru import logger


def _load_label_for(frame_path: Path) -> str:
    """Read data/labels.jsonl and return a short caption for *frame_path*."""
    labels_path = Path("data/labels.jsonl")
    if not labels_path.exists():
        return ""
    session_id = frame_path.parent.parent.name  # data/sessions/<id>/frames/x.png
    captions: list[str] = []
    for line in labels_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("session_id") == session_id and r.get("file") == frame_path.name:
            screen_type = r.get("screen_type", "?")
            tags = r.get("tags") or []
            notes = r.get("notes", "")
            tag_str = ", ".join(tags) if tags else ""
            captions.append(f"{screen_type} | {tag_str} | {notes}")
    return " || ".join(captions) if captions else "(no label)"


def _print_section(title: str) -> None:
    print()
    print(f"  ── {title} ".ljust(78, "─"))


def _print_elements(elements, frame_w: int, frame_h: int) -> None:
    """Pretty-print OmniParser elements — every field, no truncation."""
    print(f"  total={len(elements)}  (frame {frame_w}×{frame_h})")
    print(f"  fields per element: label, element_type, x1, y1, x2, y2, "
          f"confidence  (cx, cy, width, height are computed)")
    print()
    print(f"  {'idx':>3s}  {'type':<7s}  "
          f"{'x1':>5s} {'y1':>5s} {'x2':>5s} {'y2':>5s}  "
          f"{'cx':>5s} {'cy':>5s}  {'w':>4s}×{'h':<4s}  "
          f"{'cx_n':>5s} {'cy_n':>5s}  {'conf':>5s}  label")
    for i, el in enumerate(elements):
        cx_n = el.cx / frame_w
        cy_n = el.cy / frame_h
        label = (el.label or "").replace("\n", " ")
        print(
            f"  {i:>3d}  {el.element_type:<7s}  "
            f"{el.x1:>5d} {el.y1:>5d} {el.x2:>5d} {el.y2:>5d}  "
            f"{el.cx:>5d} {el.cy:>5d}  {el.width:>4d}×{el.height:<4d}  "
            f"{cx_n:>5.2f} {cy_n:>5.2f}  {el.confidence:>5.2f}  "
            f"{label!r}"
        )


def _print_ocr_raw(frame, max_show=None) -> None:
    """OCR with FULL bounding-box quad — what _get_reader().readtext returns
    before the _ocr_frame helper collapses to centroids."""
    import numpy as np
    from vision.ocr import _get_reader
    raw = _get_reader().readtext(np.array(frame), detail=1)
    print(f"  total={len(raw)} raw entries (no min_conf filter)")
    print(f"  fields per entry: bbox=[(x,y), ...] (4 corners), text, confidence")
    print()
    for i, entry in enumerate(raw):
        if max_show is not None and i >= max_show:
            print(f"  … and {len(raw) - max_show} more")
            break
        bbox, text, conf = entry
        # bbox is [[x,y], [x,y], [x,y], [x,y]] (TL, TR, BR, BL)
        bbox_str = ", ".join(f"({int(p[0])},{int(p[1])})" for p in bbox)
        text_show = (text or "").replace("\n", " ")
        print(f"  {i:>3d}  conf={conf:.3f}  bbox=[{bbox_str}]  text={text_show!r}")


def dump_frame(frame_path: Path) -> None:
    print()
    print("=" * 78)
    print(f"FRAME  {frame_path}")
    label = _load_label_for(frame_path)
    if label:
        print(f"LABEL  {label}")
    print("=" * 78)

    frame = Image.open(frame_path).convert("RGB")
    fw, fh = frame.width, frame.height

    # ── L1 — OmniParser ───────────────────────────────────────────────────────
    _print_section("L1  OmniParser parse_fast")
    from vision.omniparser import parse_fast_cached
    elements = parse_fast_cached(frame)
    _print_elements(elements, fw, fh)

    # ── L2 — Full-frame OCR (raw readtext output) ─────────────────────────────
    _print_section("L2  EasyOCR readtext (raw, before _ocr_frame collapses to centroids)")
    _print_ocr_raw(frame)
    # Also collect the centroid form for downstream layers that need it.
    from actions.sail_actions import _ocr_frame, clear_ocr_frame_cache
    clear_ocr_frame_cache()
    tokens = _ocr_frame(frame, min_conf=0.30)

    # ── L3 — Chrome detector ──────────────────────────────────────────────────
    _print_section("L3  Chrome detector")
    from vision.chrome_detector import get_chrome_detector
    chrome = get_chrome_detector().detect(frame)
    print(f"  has_home          = {chrome.has_home}")
    print(f"  has_back_arrow    = {chrome.has_back_arrow}")
    print(f"  has_hamburger     = {chrome.has_hamburger}")
    print(f"  has_right_panel   = {chrome.has_right_panel}")

    # ── L4 — Fingerprint classify ─────────────────────────────────────────────
    _print_section("L4  Fingerprint registry classify_via_registry")
    from vision.state_fingerprints import classify_via_registry
    result = classify_via_registry(elements, fw, fh)
    if result is None:
        print("  no fingerprint matched")
    else:
        print(f"  state      = {result.state!r}")
        print(f"  detail     = {result.detail!r}")
        print(f"  confidence = {result.confidence!r}")
        print(f"  signals    = {result.signals}")

    # ── L5 — Interruptor scan ─────────────────────────────────────────────────
    _print_section("L5  Interruptor detection")
    from brain.perceive import _detect_interruptors
    interruptors = _detect_interruptors(frame, tokens)
    print(f"  detected = {interruptors}")

    # ── L6 — Active flow detection ────────────────────────────────────────────
    _print_section("L6  Active flow detection")
    from brain.perceive import _detect_active_flow
    nav_state = result.state if result else "unknown"
    nav_detail = result.detail if result else ""
    flow_id, flow_step = _detect_active_flow(frame, nav_state, nav_detail)
    print(f"  flow      = {flow_id!r}")
    print(f"  flow_step = {flow_step!r}")

    # ── L7 — Context-aware drain preview ──────────────────────────────────────
    _print_section("L7  find_positive_button_for_context (preview)")
    from brain.commit_actions import (
        find_positive_button, find_positive_button_for_context,
    )
    legacy = find_positive_button(elements, frame_w=fw, frame_h=fh)
    print(f"  legacy any-positive   = "
          f"{(legacy.label, legacy.cx, legacy.cy) if legacy else None}")
    for goal_label, kws in [
        ("recruit-crew", ["recruit", "hire", "crew", "mate"]),
        ("supply",       ["supply", "auto supply", "supplies"]),
    ]:
        ctx = find_positive_button_for_context(
            elements, frame_w=fw, frame_h=fh, goal_keywords=kws,
        )
        print(f"  goal={goal_label:14s} kws={kws}")
        print(f"    → {(ctx.label, ctx.cx, ctx.cy) if ctx else None}")

    # ── L8 — Full perceive() ──────────────────────────────────────────────────
    _print_section("L8  Full perceive() PerceiveResult")
    # perceive() captures its own frame by default; we'd have to monkey-patch
    # capture_screen for a clean dump.  Skip for this CLI — the per-layer
    # views above are the actual building blocks.
    print("  (skipped — per-layer dumps above are the components)")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.split("Usage:", 1)[1].splitlines()[1].strip(), file=sys.stderr)
        return 2
    # Quiet the loguru spam; we want clean dumps.
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    for arg in sys.argv[1:]:
        p = Path(arg).expanduser().resolve()
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            continue
        dump_frame(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
