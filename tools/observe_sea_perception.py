"""Supervised perception observation loop — read-only, no steering.

Captures live frames from the phone via ADB, runs the perceive
pipeline, and prints what the mini-map + shoreline readers see.  Also
saves every captured frame plus its verdict to a new session
directory under data/sessions/, so you can review disagreements
offline and decide if/where the perception needs more training data.

The bot DOES NOT act — you steer the ship (or set an auto-sail
destination) and the script just watches.  Hit Ctrl-C to stop.

Usage:
    python tools/observe_sea_perception.py
    python tools/observe_sea_perception.py --interval 3 --session my_test
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _fmt_tags(tagset) -> str:
    if not tagset:
        return "—"
    return ", ".join(sorted(t.replace("minimap:", "") for t in tagset))


def _fmt_shoreline(v) -> str:
    if v is None:
        return "unknown"
    bits = []
    if v.land_ahead:     bits.append("AHEAD")
    if v.land_port:      bits.append("port")
    if v.land_starboard: bits.append("starboard")
    if v.beam_present:   bits.append("beam")
    base = " + ".join(bits) if bits else "open sea"
    # also show top probability
    if v.probs:
        top = max(v.probs.items(), key=lambda kv: kv[1])
        base += f"   (top={top[0].split(':')[-1]} {top[1]:.2f})"
    return base


_running = True


def _sigint(_signum, _frame):
    global _running
    _running = False
    print("\n[observe] interrupt — finishing current tick…", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.5,
                    help="base seconds between ticks (jittered ±25%%)")
    ap.add_argument("--session", default=None,
                    help="session id under data/sessions/; default = "
                         "observe_<timestamp>")
    ap.add_argument("--max-ticks", type=int, default=None,
                    help="stop after N ticks (default: until Ctrl-C)")
    ap.add_argument("--no-save", action="store_true",
                    help="don't save frames+verdicts to disk")
    args = ap.parse_args()

    from PIL import Image
    from capture.adb_capture import capture_screen
    from brain.perceive import perceive
    from brain import observation as obs_module

    session_id = args.session or "observe_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = _PROJECT_ROOT / "data" / "sessions" / session_id
    frames_dir = session_dir / "frames"
    verdicts_path = session_dir / "verdicts.jsonl"
    metadata_path = session_dir / "metadata.json"
    started_at = datetime.now().isoformat()
    if not args.no_save:
        frames_dir.mkdir(parents=True, exist_ok=True)
    saved_frames: list[dict] = []

    print(f"[observe] session: {session_id}")
    print(f"[observe] interval: {args.interval:.1f}s (jittered)")
    if args.no_save:
        print("[observe] save disabled — frames will not be written")
    else:
        print(f"[observe] writing frames to {frames_dir}")
        print(f"[observe] writing verdicts to {verdicts_path}")
    print()
    print(f"  {'tick':>4s}  {'state':<18s}  {'shoreline':<55s}  minimap")
    print("  " + "-" * 110)

    signal.signal(signal.SIGINT, _sigint)

    tick = 0
    start_t = time.monotonic()
    while _running and (args.max_ticks is None or tick < args.max_ticks):
        tick += 1
        try:
            frame = capture_screen()
        except Exception as e:
            print(f"  {tick:>4d}  [capture failed: {type(e).__name__}: {e}]")
            time.sleep(args.interval)
            continue

        try:
            result = perceive(frame)
        except Exception as e:
            print(f"  {tick:>4d}  [perceive failed: {type(e).__name__}: {e}]")
            time.sleep(args.interval)
            continue

        cur = obs_module.current()
        mm = cur.minimap if cur else None
        sh = cur.shoreline if cur else None

        state = result.state
        if state == "sea":
            mm_tags = mm.tags if mm else frozenset()
            shore_str = _fmt_shoreline(sh)
            mm_str = _fmt_tags(mm_tags)
        else:
            shore_str = "(non-sea screen)"
            mm_str = "(non-sea screen)"

        print(f"  {tick:>4d}  {state:<18s}  {shore_str:<55s}  {mm_str}")

        if not args.no_save:
            fname = f"{tick:04d}_{datetime.now().strftime('%H%M%S%f')[:-3]}.png"
            ts_iso = datetime.now().isoformat()
            try:
                frame.save(frames_dir / fname)
                saved_frames.append({"file": fname, "timestamp": ts_iso})
            except Exception as e:
                print(f"        (save failed: {e})")
            row = {
                "tick":      tick,
                "file":      fname,
                "session_id": session_id,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "state":     result.state,
                "detail":    result.detail,
                "shoreline": (
                    {
                        "land_ahead":     bool(sh.land_ahead) if sh else None,
                        "land_port":      bool(sh.land_port) if sh else None,
                        "land_starboard": bool(sh.land_starboard) if sh else None,
                        "beam_present":   bool(sh.beam_present) if sh else None,
                        "probs":          (sh.probs if sh else None),
                    } if sh else None
                ),
                "minimap": (
                    {
                        "tags":  sorted(mm.tags) if mm else [],
                        "probs": (mm.probs if mm else None),
                    } if mm else None
                ),
            }
            try:
                with open(verdicts_path, "a") as f:
                    f.write(json.dumps(row) + "\n")
            except Exception as e:
                print(f"        (verdict log failed: {e})")

        # Jittered sleep — same anti-cadence rule as the action layer.
        sleep = args.interval * random.uniform(0.75, 1.25)
        time.sleep(sleep)

    elapsed = time.monotonic() - start_t
    if not args.no_save and saved_frames:
        metadata = {
            "session_id":  session_id,
            "started_at":  started_at,
            "mode":        "observe",
            "frames":      saved_frames,
            "ended_at":    datetime.now().isoformat(),
            "total_saved": len(saved_frames),
        }
        try:
            metadata_path.write_text(json.dumps(metadata, indent=2))
        except Exception as e:
            print(f"[observe] (metadata write failed: {e})")
    print()
    print(f"[observe] done.  {tick} ticks in {elapsed:.0f}s")
    if not args.no_save:
        print(f"[observe] frames + verdicts saved under {session_dir}")
        print()
        print("Next step: open the labeler with this session, scan the")
        print("frames that disagreed with the printed verdict, and add")
        print("corrections — they become training data for the next round.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
