# supervisor/capture_session.py
# Continuous screenshot capture during manual gameplay.
#
# Run this while you play the game manually. It saves a screenshot every
# N seconds (default 2.5s), skipping frames that are too similar to the
# previous one (character walking animation, not a new screen).
#
# Saved frames are the raw training data for the screen classifier.
#
# Usage:
#   python -m supervisor.capture_session
#   python -m supervisor.capture_session --interval 2.0
#
# Output:
#   data/sessions/<YYYY-MM-DD_HH-MM-SS>/
#     metadata.json     session info and frame list
#     frames/           PNG screenshots

from __future__ import annotations

import argparse
import json
import sys
import termios
import tty
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from loguru import logger
from PIL import Image

# Project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from capture.adb_capture import capture_screen

# ── defaults ──────────────────────────────────────────────────────────────────

CAPTURE_INTERVAL: float = 3.0   # seconds between capture attempts
DEDUP_THRESHOLD: float = 8.0    # chrome diff below this = skip duplicate
#
# Tuning notes:
#   CAPTURE_INTERVAL — controls how quickly transitions are caught.
#     3s is a good default: fast enough to catch brief dialogs and screen
#     changes, slow enough not to fire too often.
#
#   DEDUP_THRESHOLD — chrome-only pixel diff threshold.
#     Dedup compares only the persistent UI controls (top-left title/back
#     arrow, top-right home button, right panel tab bar + mini map).
#     The scrolling game world is intentionally ignored — a character
#     running past different buildings still has the same chrome as any
#     other port_overworld frame.
#     8 works well in practice. Raise to 15 if too many frames are saved;
#     lower to 4 if screen-type transitions are being missed.

DATA_DIR = Path(__file__).parent.parent / "data"
SESSIONS_DIR = DATA_DIR / "sessions"


# ── chrome regions used for dedup ────────────────────────────────────────────
# We compare only the persistent UI chrome regions, not the full frame.
# The game world behind the chrome (buildings, NPCs, sea, trees) changes
# constantly as the character moves — but the chrome only changes when the
# screen TYPE changes (e.g. overworld → port map → building interior).
# Comparing chrome only means we save one frame per distinct screen state,
# not one frame per animation tick.
#
# Regions: (left, top, right, bottom) at 2400×1080
_CHROME_REGIONS: list[tuple[int, int, int, int]] = [
    (0,    0,    220,  80),    # top-left: back arrow / title area
    (2280, 5,    2400, 80),    # top-right: home / hamburger button
    (2050, 100,  2400, 420),   # right panel: tab bar + mini map
    (0,    880,  320,  1080),  # bottom-left: world map button (port map only)
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _chrome_diff(a: Image.Image, b: Image.Image) -> float:
    """
    Mean absolute pixel difference across chrome regions only.

    Ignores the scrolling game world (buildings, NPCs, sea) and compares
    only the stable UI controls. Returns 0 when chrome is identical,
    large values when the screen type has changed.
    """
    total_diff = 0.0
    total_pixels = 0

    for region in _CHROME_REGIONS:
        crop_a = np.array(a.crop(region).convert("RGB"), dtype=float)
        crop_b = np.array(b.crop(region).convert("RGB"), dtype=float)
        total_diff += float(np.abs(crop_a - crop_b).sum())
        total_pixels += crop_a.size

    return total_diff / total_pixels if total_pixels > 0 else 0.0


# ── raw keypress (no Enter needed) ───────────────────────────────────────────

def _read_key() -> str:
    """Read a single keypress from stdin without requiring Enter."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


# ── main ──────────────────────────────────────────────────────────────────────

def run(
    interval: float = CAPTURE_INTERVAL,
    dedup: float = DEDUP_THRESHOLD,
) -> None:
    """
    Capture screenshots continuously until Ctrl+C.

    Deduplication: frames that differ from the previous saved frame by less
    than *dedup* are skipped — they represent small animation changes
    (walking, sailing, blinking NPC) rather than a meaningful new screen state.
    """
    session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    frames_dir = SESSIONS_DIR / session_id / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    meta_path = SESSIONS_DIR / session_id / "metadata.json"

    metadata: dict = {
        "session_id": session_id,
        "started_at": datetime.now().isoformat(),
        "interval_s": interval,
        "dedup_threshold": dedup,
        "frames": [],
    }

    logger.info(f"╔══ Capture session started ══════════════════════════════")
    logger.info(f"║  Session : {session_id}")
    logger.info(f"║  Output  : {frames_dir}")
    logger.info(f"║  Interval: {interval}s   Dedup threshold: {dedup}")
    logger.info(f"╚══ Press Ctrl+C to stop ════════════════════════════════")

    prev_frame: Image.Image | None = None
    total_captures = 0
    saved_count = 0

    try:
        while True:
            t_start = time.monotonic()

            try:
                frame = capture_screen()
            except Exception as exc:
                logger.warning(f"Capture failed: {exc} — retrying")
                time.sleep(interval)
                continue

            total_captures += 1

            diff = _chrome_diff(frame, prev_frame) if prev_frame is not None else 999.0

            if diff < dedup:
                logger.debug(f"#{total_captures:4d}  chrome_diff={diff:5.2f}  same screen — skipped")
            else:
                ts = datetime.now()
                filename = f"{saved_count:04d}_{ts.strftime('%H%M%S%f')[:10]}.png"
                frame.save(frames_dir / filename)

                metadata["frames"].append({
                    "file": filename,
                    "timestamp": ts.isoformat(),
                    "capture_index": total_captures,
                })

                # Write metadata after every saved frame so it survives a crash
                with open(meta_path, "w") as f:
                    json.dump(metadata, f, indent=2)

                saved_count += 1
                prev_frame = frame
                logger.info(
                    f"#{total_captures:4d}  chrome_diff={diff:5.2f}  "
                    f"saved frame {saved_count:04d}  ({filename})"
                )

            elapsed = time.monotonic() - t_start
            time.sleep(max(0.0, interval - elapsed))

    except KeyboardInterrupt:
        pass

    # Final metadata
    metadata["ended_at"] = datetime.now().isoformat()
    metadata["total_captures"] = total_captures
    metadata["total_saved"] = saved_count
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)

    skipped = total_captures - saved_count
    logger.info(
        f"\n{'─' * 55}\n"
        f"  Session   : {session_id}\n"
        f"  Captured  : {total_captures} frames\n"
        f"  Saved     : {saved_count} unique frames\n"
        f"  Skipped   : {skipped} duplicates\n"
        f"  Label with: python -m supervisor.labeler\n"
        f"{'─' * 55}"
    )


def run_manual() -> None:
    """
    Manual capture mode — press SPACE to save a frame, Q to quit.

    No automatic timer. Every saved frame is unique by definition
    (you chose to press the key). Chrome diff is shown for awareness
    but never blocks the save.
    """
    session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    frames_dir = SESSIONS_DIR / session_id / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    meta_path = SESSIONS_DIR / session_id / "metadata.json"

    metadata: dict = {
        "session_id": session_id,
        "started_at": datetime.now().isoformat(),
        "mode": "manual",
        "frames": [],
    }

    print(f"\n  Manual capture  —  session {session_id}")
    print(f"  Output : {frames_dir}")
    print(f"  SPACE  : capture frame")
    print(f"  Q / Ctrl+C : finish\n")

    prev_frame: Image.Image | None = None
    saved_count = 0

    try:
        while True:
            key = _read_key()

            # Ctrl+C sends \x03
            if key in ("q", "Q", "\x03"):
                break

            if key != " ":
                continue

            try:
                frame = capture_screen()
            except Exception as exc:
                print(f"  ! capture failed: {exc}")
                continue

            diff = _chrome_diff(frame, prev_frame) if prev_frame is not None else 0.0
            ts = datetime.now()
            filename = f"{saved_count:04d}_{ts.strftime('%H%M%S%f')[:10]}.png"
            frame.save(frames_dir / filename)

            metadata["frames"].append({
                "file": filename,
                "timestamp": ts.isoformat(),
            })
            with open(meta_path, "w") as f:
                json.dump(metadata, f, indent=2)

            saved_count += 1
            diff_note = f"  chrome_diff={diff:.1f}" if prev_frame is not None else ""
            print(f"  saved {saved_count:04d}  {filename}{diff_note}")
            prev_frame = frame

    except KeyboardInterrupt:
        pass

    metadata["ended_at"] = datetime.now().isoformat()
    metadata["total_saved"] = saved_count
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"\n  Done — {saved_count} frames saved.")
    print(f"  Label with: python -m supervisor.labeler\n")


if __name__ == "__main__":
    from memory.logger import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Capture gameplay screenshots for training data"
    )
    parser.add_argument(
        "--manual", action="store_true",
        help="Manual mode: press SPACE to capture, Q to quit (no automatic timer)"
    )
    parser.add_argument(
        "--interval", type=float, default=CAPTURE_INTERVAL,
        help=f"Auto mode only — seconds between captures (default: {CAPTURE_INTERVAL})"
    )
    parser.add_argument(
        "--dedup", type=float, default=DEDUP_THRESHOLD,
        help=(
            f"Auto mode only — min chrome diff to save (default: {DEDUP_THRESHOLD}). "
            "Raise (e.g. 20) if too many duplicates are saved. "
            "Lower (e.g. 6) if transitions are being missed."
        )
    )
    args = parser.parse_args()

    if args.manual:
        run_manual()
    else:
        run(interval=args.interval, dedup=args.dedup)
