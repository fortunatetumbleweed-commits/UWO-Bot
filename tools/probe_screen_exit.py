"""Probe the exit_current_screen() helper on whatever screen the phone
is currently showing.

Captures one frame, runs all the detectors the helper depends on, and
reports:
  - perceived state
  - chrome flags (has_home, has_back_arrow, etc.)
  - whether a DialogModel was detected (and where its close-X is)
  - which method the helper WOULD pick (dry_run by default — no taps)

Usage:
    python tools/probe_screen_exit.py            # dry run — read-only
    python tools/probe_screen_exit.py --execute  # actually tap

Suggested stations:
    Inside a building              expect: home_or_menu_x
    Inside a sub_menu (no home)    expect: back_arrow
    Main menu (hamburger open)     expect: home_or_menu_x  (X variant)
    Info dialog open               expect: dialog_close
    port_overworld (no popups)     expect: refused
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--execute", action="store_true",
        help="Actually tap / press_back.  Default is dry run (read-only).",
    )
    ap.add_argument(
        "--no-overworld-refuse", action="store_true",
        help="Allow press_back fallback even on port_overworld.  "
             "DANGEROUS — would open Exit Game?.  Off by default.",
    )
    args = ap.parse_args()

    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.dialog import detect_dialog
    from vision.chrome_detector import get_chrome_detector
    from brain.perceive import perceive
    from actions.screen_exit import exit_current_screen

    frame = capture_screen()
    print(f"frame size: {frame.size}")
    print()

    # ── Perceived state ────────────────────────────────────────────
    state = perceive(frame)
    print("─── perceive ─────────────────────────────────────────")
    print(f"  state      : {state.state!r}")
    print(f"  port       : {state.port!r}")
    print(f"  detail     : {(state.detail or '')[:120]!r}")
    print(f"  in_flow    : {getattr(state, 'in_flow', False)!r}")
    print()

    # ── Chrome flags ───────────────────────────────────────────────
    chrome = get_chrome_detector().detect(frame)
    print("─── chrome_detector ──────────────────────────────────")
    print(f"  has_hamburger    : {chrome.has_hamburger}")
    print(f"  has_home         : {chrome.has_home}")
    print(f"  has_back_arrow   : {chrome.has_back_arrow}")
    if hasattr(chrome, "has_right_panel"):
        print(f"  has_right_panel  : {chrome.has_right_panel}")
    if hasattr(chrome, "has_world_map_btn"):
        print(f"  has_world_map_btn: {chrome.has_world_map_btn}")
    print()

    # ── Dialog detection ───────────────────────────────────────────
    elements = parse_fast_cached(frame)
    dlg = detect_dialog(elements, frame.width, frame.height)
    print("─── DialogModel ──────────────────────────────────────")
    if dlg is None:
        print("  (no dialog detected)")
    else:
        print(f"  kind          : {dlg.kind()!r}")
        print(f"  bbox          : {dlg.bbox}")
        print(f"  close_button  : {dlg.close_button}")
        action_summary = [(a.label, a.is_positive) for a in dlg.actions]
        print(f"  actions       : {action_summary}")
        print(f"  anchors_fired : {dlg.anchors_fired}")
    print()

    # ── exit_current_screen decision ───────────────────────────────
    print("─── exit_current_screen ──────────────────────────────")
    result = exit_current_screen(
        frame=frame,
        refuse_on_overworld=not args.no_overworld_refuse,
        dry_run=not args.execute,
    )
    verb = "EXECUTED" if args.execute else "WOULD"
    print(f"  {verb}: method={result.method!r}  ok={result.ok}")
    if result.note:
        print(f"  note  : {result.note}")
    print()
    if not args.execute:
        print("  (re-run with --execute to actually fire the action)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
