"""Per-action frame recorder — capture (screenshot, tap-target) at every tap so a
flow can be replayed and analysed frame-by-frame afterwards.

Motivation: reading logs to infer "did the bot tap the Purchase button?" is
guesswork. This records, for EVERY tap, the frame the bot saw *before* tapping
plus the exact tap point — so after a run we can look at each frame one by one and
see precisely what action was taken on it (like the navigation tick-frame dumps).

Usage (wrap a run):
    from actions import action_trace
    action_trace.start("london_amsterdam")
    ...run the flow...
    action_trace.stop()

Output: data/sessions/trace_<name>_<ts>/
    frame_0000.png            the pre-tap screen
    frame_0000_marked.png     same, with a red crosshair at the tap point
    actions.jsonl             one row per action: {idx,kind,x,y,label,t,frame}

Analyse with tools/analyze_action_trace.py.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from loguru import logger

_SESSIONS = Path("data/sessions")

_dir: Optional[Path] = None
_idx = 0
_label: Optional[str] = None      # optional semantic hint set by callers


def start(name: str) -> Path:
    """Begin recording taps to a fresh session dir; returns its path."""
    global _dir, _idx
    ts = time.strftime("%Y-%m-%dT%H-%M-%S")
    _dir = _SESSIONS / f"trace_{name}_{ts}"
    _dir.mkdir(parents=True, exist_ok=True)
    _idx = 0
    logger.info(f"[action_trace] recording taps → {_dir}")
    return _dir


def stop() -> Optional[Path]:
    global _dir
    d = _dir
    _dir = None
    if d is not None:
        logger.info(f"[action_trace] stopped after {_idx} action(s) → {d}")
    return d


def active() -> bool:
    return _dir is not None


def set_label(label: Optional[str]) -> None:
    """Optional: a caller can name the action it is about to take; recorded on
    the next tap and then cleared."""
    global _label
    _label = label


def _capture_with_perception():
    """Capture the pre-action frame and, when it matches the frame the bot JUST perceived
    (coarse diff), attach that perceive's already-computed OmniParser / OCR / state — so the
    viewer reuses it instead of re-running OmniParser (~2 s/frame).  The omni/ocr reads are
    cache HITS on the perceive frame (no inference).  Returns (frame, perception|None)."""
    from capture.adb_capture import capture_screen
    frame = capture_screen()
    try:
        from brain import perceive as _p
        pf, pr = _p._PERCEIVE_LAST_FRAME, _p._PERCEIVE_LAST_RESULT
        if pf is None or pr is None:
            return frame, None
        from vision.frame_diff import classify_action_outcome
        if classify_action_outcome(pf, frame).kind != "unchanged":
            return frame, None                          # different screen — don't misattribute
        from vision.omniparser import parse_fast_cached
        from actions.sail_actions import _ocr_frame
        omni = [e.to_dict() for e in parse_fast_cached(pf)]          # cache hit
        ocr = [{"text": t, "conf": round(float(c), 2), "cx": int(cx), "cy": int(cy)}
               for t, c, cx, cy in _ocr_frame(pf, 0.3)]              # cache hit
        return frame, {"omni": omni, "ocr": ocr,
                       "state": getattr(pr, "state", None),
                       "detail": getattr(pr, "detail", None)}
    except Exception as exc:
        logger.debug(f"[action_trace] perception attach skipped: {exc}")
        return frame, None


def record_tap(x: int, y: int, kind: str = "tap") -> None:
    """Capture the PRE-action frame + the tap target. Called by the tap/back
    primitives when a session is active. Never raises."""
    global _idx, _label
    if _dir is None:
        return
    try:
        frame, perception = _capture_with_perception()
        fn = f"frame_{_idx:04d}.png"
        frame.save(_dir / fn)
        if kind == "tap":
            _save_marked(frame, x, y, fn)
        entry = {"idx": _idx, "kind": kind, "x": x, "y": y,
                 "label": _label, "t": time.strftime("%H:%M:%S"), "frame": fn}
        with (_dir / "actions.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        if perception is not None:                      # viewer reuses this — no OmniParser re-run
            (_dir / f"frame_{_idx:04d}.json").write_text(json.dumps(perception))
        _idx += 1
    except Exception as exc:
        logger.debug(f"[action_trace] record failed: {exc}")
    finally:
        _label = None


def record_decision(inputs: dict, output: dict, model: str, label: str = "decision") -> None:
    """Capture the current frame + a decision: its INPUTS, the action CHOSEN, and
    WHICH model made it. Shown on the viewer's Decision tab. Never raises."""
    global _idx
    if _dir is None:
        return
    try:
        frame, perception = _capture_with_perception()
        fn = f"frame_{_idx:04d}.png"
        frame.save(_dir / fn)
        entry = {"idx": _idx, "kind": "decision", "x": -1, "y": -1, "label": label,
                 "t": time.strftime("%H:%M:%S"), "frame": fn,
                 "inputs": inputs, "output": output, "model": model}
        with (_dir / "actions.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        if perception is not None:
            (_dir / f"frame_{_idx:04d}.json").write_text(json.dumps(perception))
        _idx += 1
    except Exception as exc:
        logger.debug(f"[action_trace] record_decision failed: {exc}")


def _save_marked(frame, x: int, y: int, fn: str) -> None:
    try:
        from PIL import ImageDraw
        marked = frame.convert("RGB").copy()
        d = ImageDraw.Draw(marked)
        r = 30
        d.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=6)
        d.line([x - r, y, x + r, y], fill=(255, 0, 0), width=3)
        d.line([x, y - r, x, y + r], fill=(255, 0, 0), width=3)
        marked.save(_dir / fn.replace(".png", "_marked.png"))
    except Exception:
        pass
