"""Measure the average heading change per L/R arrow tap.

Captures the current sea frame, fires N taps in the chosen direction,
waits for the rotation to play out, then captures again and reports
the heading delta read from the mini-map navigation view.

Run on a clear stretch of open sea (no land, no fleets close by)
so the ship's heading detection is unambiguous.

Usage:
    python tools/calibrate_turn_angle.py --side right --taps 10
    python tools/calibrate_turn_angle.py --side left  --taps 5 --pause 4

Reads ship_heading_deg from BotObservation.nav (compass: 0=N).  The
delta is wrapped into (-180, 180] so a 5° right turn shows as +5°.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _wrap_signed(delta: float) -> float:
    """Wrap an angle delta into (-180, 180]."""
    delta = (delta + 180) % 360 - 180
    return delta


def _read_heading() -> float | None:
    from capture.adb_capture import capture_screen
    from vision.minimap_navigation_view import read_navigation_view, _reset_for_test
    _reset_for_test()
    frame = capture_screen()
    nav = read_navigation_view(frame)
    return nav.ship_heading_deg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=("left", "right"), required=True)
    ap.add_argument("--taps", type=int, default=10,
                    help="number of taps to fire (default 10)")
    ap.add_argument("--pause", type=float, default=3.0,
                    help="seconds to wait after the last tap before "
                         "re-reading heading (default 3)")
    args = ap.parse_args()

    from actions import sea_actions

    print(f"[calibrate] capturing initial heading…")
    h0 = _read_heading()
    if h0 is None:
        print("[calibrate] could not read ship heading from mini-map.  "
              "Make sure the bot is on the sea screen with a clear view.",
              file=sys.stderr)
        return 1
    print(f"[calibrate] heading before: {h0:.1f}°")

    print(f"[calibrate] firing {args.taps} {args.side.upper()} taps…")
    if args.side == "left":
        sea_actions.turn_left(taps=args.taps)
    else:
        sea_actions.turn_right(taps=args.taps)

    print(f"[calibrate] waiting {args.pause:.1f}s for rotation to settle…")
    time.sleep(args.pause)

    h1 = _read_heading()
    if h1 is None:
        print("[calibrate] could not read post-turn heading.  "
              "Sea view may be obscured.", file=sys.stderr)
        return 1
    print(f"[calibrate] heading after:  {h1:.1f}°")

    delta = _wrap_signed(h1 - h0)
    print(f"[calibrate] Δheading: {delta:+.1f}°")
    per_tap = delta / args.taps
    print(f"[calibrate] per-tap:  {per_tap:+.2f}°  ({args.taps} taps over {args.pause}s)")
    if abs(per_tap) < 0.5:
        print("[calibrate] very small per-tap result — heading detection")
        print("[calibrate] might be noisy.  Try more taps or a longer pause.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
