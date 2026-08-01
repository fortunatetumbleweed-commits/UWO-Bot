"""Manual sea-view controls — Phase 1 of the clear-cloud (coast-explore)
task.

This module provides the four primitives the bot needs to fly the ship
under its own steering, instead of letting the game auto-sail a chosen
destination:

  - sail_start()        tap rudder → ship begins forward (latched)
  - sail_stop()         tap anchor → ship stops
  - turn_left(taps=1)   tap ( arrow N times
  - turn_right(taps=1)  tap ) arrow N times

The sea screen's bottom-left has a circular wheel.  Three tappable
icons sit on it:

  - **rudder/anchor toggle** (centre column, ABOVE the big wheel circle).
    A SINGLE icon position whose sprite swaps based on sailing state:
      ship stopped → rudder (ship's-wheel symbol)
      ship moving  → anchor
    Tapping the toggle while stopped → start sailing (latches forward).
    Tapping while moving → stop.

  - **left arrow `(`** inside the big wheel, lower-mid-left.
  - **right arrow `)`** inside the big wheel, lower-mid-right.

Swipes on the sea view CHANGE THE CAMERA, not the heading.  All
control input here is single discrete taps.  Tap-and-hold is
equivalent to tap-at-fixed-interval (per the user's 2026-05-24 note),
so a multi-tap turn is implemented as N discrete taps with a small
intra-tap delay.

Coordinates were measured live on 2026-05-24 against a 2400×1080
sea frame.  Reference frame: data/sessions/2026-05-24_12-38-56/
frames/0000_1239004652.png (rudder visible, ship not moving).

Origin: 2026-05-24 calibration session — sail_start / sail_stop /
turn_left all verified end-to-end on the live device (rudder/anchor
icon swap + ship wake confirmed).  See /tmp/sail_dev/ for the
before/after captures.
"""
from __future__ import annotations

import time
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from actions.adb_actions import tap
from capture.adb_capture import capture_screen


# ── Calibrated control coordinates (2400×1080) ─────────────────────────────
# Re-calibrated 2026-06-12 against live capture at Cairo — game UI shifted
# ~+120-130 px right and ~5-10 px up vs the 2026-05-24 calibration.  Same
# game update that moved MINIMAP_CROP (see
# memory/project_minimap_crop_drifts_with_game_updates.md).
# Prior values:
#   2026-06-27 (game UI update): shifted +130,-10 from earlier
#     RUDDER_TOGGLE_XY: (331, 731)
#     ARROW_LEFT_XY:    (235, 978)
#     ARROW_RIGHT_XY:   (418, 975)
#     RUDDER_TOGGLE_BBOX: (281, 681, 381, 781)
#   even earlier baseline:
#     RUDDER_TOGGLE_XY: (201, 741)
#     ARROW_LEFT_XY:    (115, 983)
#     ARROW_RIGHT_XY:   (298, 980)

# Recalibrated 2026-07-21 by bright-pixel centroid on the wheel-region
# crop of a live sea-view screencap.  Prior values (247, 960 / 417, 960)
# landed at the wheel centre (near sail-stop) and on the crossed-swords
# "Muhiddin Expedition" button — ship barely moved and opened random
# dialogs.  Verified identical across both "camera left" and "camera
# right" game states — the wheel + arrows stay pinned to the bottom-
# left corner regardless of camera-view mode.
RUDDER_TOGGLE_XY: tuple[int, int] = (334, 715)   # rudder ↔ anchor toggle
                                                 # midpoint of L/R arrows x
ARROW_LEFT_XY:    tuple[int, int] = (234, 974)   # `(` left turn
ARROW_RIGHT_XY:   tuple[int, int] = (434, 975)   # `)` right turn
# 2026-07-21: the whole game UI slid ~120 px right between two
# screencaps 90 min apart (same game, same location).  The bot has
# no way to know when this happens; the run_ai_nav_live pre-flight
# check covers only MINIMAP_CROP.  If tap coords look wrong at
# runtime, re-run the bright-pixel centroid on a live wheel crop:
#   region = im[870:1050, 0:600];  white = (r>200)&(g>200)&(b>200);
#   split at largest x-gap; centroid each side.

# Bounding box for region-diff confirmation that an icon swap happened.
# Recalibrated 2026-07-14 to the new RUDDER_TOGGLE_XY (325, 715).
RUDDER_TOGGLE_BBOX: tuple[int, int, int, int] = (275, 665, 375, 765)
# 2026-06-27: game UI updated; the toggle icon is now centred at
# RUDDER_TOGGLE_XY (331, 731) with a ~100 px circular icon.  Prior
# bbox (409, 676, 507, 786) was reading the captain-hat button to
# the right and template-matching it as 'anchor' → sail_start was
# falsely no-op'd ("ship already moving") and the bot never sailed.
# Same drift pattern as MINIMAP/LATLON/SPEED crops shifted this week.


# ── Reference templates for is_ship_moving() ───────────────────────────────
_TEMPLATE_DIR = (Path(__file__).resolve().parent.parent
                 / "vision" / "assets" / "sea_controls")
_RUDDER_TEMPLATE_PATH = _TEMPLATE_DIR / "rudder_stopped.png"
_ANCHOR_TEMPLATE_PATH = _TEMPLATE_DIR / "anchor_moving.png"


# ── Settings ───────────────────────────────────────────────────────────────
_POST_TAP_SETTLE_S = 1.5    # for icon-swap taps (sail_start/sail_stop)
_POST_TURN_SETTLE_S = 0.1   # for L/R steering — no animation to wait for
_INTER_TAP_DELAY_S = (0.30, 0.45)  # jitter between multi-taps for L/R
# With per-tap internal ADB delay 0.3-0.8s, the 0.3-0.45s gap keeps
# 3 taps spread over ≥1.2s — safely outside the anti-cheat "3 taps
# in <1s" threshold (per CLAUDE.md).  Allows the policy to fire
# n=3 bursts when an ahead emergency demands harder turns.
# Each L/R tap = ~3° rotation (calibrated 2026-05-28), no rudder
# accumulation.


# ── Result type ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SeaActionResult:
    """Return value for sail_start / sail_stop / turn_left / turn_right.

    Fields:
      ok:           True if the action's expected post-condition was met.
      action:       Human-readable name of the action that ran.
      detail:       One-line explanation, especially useful on failure.
      icon_state:   After-state of the toggle, or None if not checked.
                    Values: 'rudder' (stopped), 'anchor' (moving), 'unknown'.
    """
    ok:         bool
    action:     str
    detail:     str = ""
    icon_state: Optional[str] = None


# ── Public primitives ──────────────────────────────────────────────────────


def sail_start() -> SeaActionResult:
    """Tap the toggle to start the ship moving.

    Aborts unless the bot is currently on the sea screen.  After the
    tap, polls the toggle icon and reports whether it swapped to the
    anchor sprite (moving) or stayed as rudder (no-op).
    """
    if not _assert_on_sea("sail_start"):
        return SeaActionResult(False, "sail_start", "not on sea screen")

    pre_state = _read_icon_state()
    if pre_state == "anchor":
        return SeaActionResult(
            True, "sail_start",
            "ship already moving (icon already anchor) — no-op",
            icon_state="anchor",
        )

    logger.info(f"[sea_actions] sail_start → tap rudder @ {RUDDER_TOGGLE_XY}")
    tap(*RUDDER_TOGGLE_XY)
    time.sleep(_POST_TAP_SETTLE_S)

    post_state = _read_icon_state()
    ok = post_state == "anchor"
    return SeaActionResult(
        ok, "sail_start",
        f"icon: {pre_state} → {post_state}",
        icon_state=post_state,
    )


def sail_stop() -> SeaActionResult:
    """Tap the toggle to stop the ship.

    Mirror of `sail_start`.  No-op when ship is already stopped.
    """
    if not _assert_on_sea("sail_stop"):
        return SeaActionResult(False, "sail_stop", "not on sea screen")

    pre_state = _read_icon_state()
    if pre_state == "rudder":
        return SeaActionResult(
            True, "sail_stop",
            "ship already stopped (icon already rudder) — no-op",
            icon_state="rudder",
        )

    logger.info(f"[sea_actions] sail_stop → tap anchor @ {RUDDER_TOGGLE_XY}")
    tap(*RUDDER_TOGGLE_XY)
    time.sleep(_POST_TAP_SETTLE_S)

    post_state = _read_icon_state()
    ok = post_state == "rudder"
    return SeaActionResult(
        ok, "sail_stop",
        f"icon: {pre_state} → {post_state}",
        icon_state=post_state,
    )


def turn_left(taps: int = 1) -> SeaActionResult:
    """Tap the left-turn arrow `(` N times with small inter-tap jitter."""
    return _do_turn("turn_left", ARROW_LEFT_XY, taps)


def turn_right(taps: int = 1) -> SeaActionResult:
    """Tap the right-turn arrow `)` N times with small inter-tap jitter."""
    return _do_turn("turn_right", ARROW_RIGHT_XY, taps)


def hold_left(duration_ms: int = 500) -> SeaActionResult:
    """Press-and-hold the left arrow for `duration_ms` milliseconds.

    Implemented via `adb shell input swipe X Y X Y duration_ms` with
    start = end coords — the device sees this as a sustained touch.
    The ship turns continuously for the duration; you cannot interrupt
    mid-flight (ADB swipe is blocking).
    """
    return _do_hold("hold_left", ARROW_LEFT_XY, duration_ms)


def hold_right(duration_ms: int = 500) -> SeaActionResult:
    """Press-and-hold the right arrow for `duration_ms` milliseconds."""
    return _do_hold("hold_right", ARROW_RIGHT_XY, duration_ms)


def is_ship_moving() -> Optional[bool]:
    """Read the toggle icon and report sailing state.

    Returns:
      True  — ship is moving (anchor icon visible)
      False — ship is stopped (rudder icon visible)
      None  — could not determine (not on sea, or icon recognition failed)
    """
    if not _assert_on_sea("is_ship_moving", log_warning=False):
        return None
    state = _read_icon_state()
    if state == "anchor":   return True
    if state == "rudder":   return False
    return None


# ── Arrow-tap audit log ────────────────────────────────────────────────
#
# Standalone append-only log of EVERY physical arrow-tap or hold issued
# to the game.  Independent of the per-tick trace.jsonl so we can see
# things the per-tick aggregation hides — e.g., two holds firing
# back-to-back inside one tick window from different code paths, or a
# `turn_left(taps=N)` happening between ticks.
#
# Each line is one JSONL record:
#   {"ts": "<ISO>", "name": "hold_left|hold_right|turn_left|turn_right",
#    "xy": [x,y], "hold_ms": int_or_null, "taps": int_or_null,
#    "callsite": "<file>:<line> in <func>"}
#
# File path: /tmp/uwo_arrow_taps.jsonl by default (overridable via the
# UWO_ARROW_TAP_LOG environment variable so test scripts can redirect).

import os as _os
import json as _json
from datetime import datetime as _dt
import traceback as _tb

_ARROW_TAP_LOG_PATH = _os.environ.get(
    "UWO_ARROW_TAP_LOG", "/tmp/uwo_arrow_taps.jsonl"
)


def _callsite_above_sea_actions() -> str:
    """Find the first stack frame OUTSIDE actions/sea_actions.py — i.e.
    the planner/goal/runner code that asked us to tap.  Returns one
    line of "path:line in function" or "<unknown>" on failure."""
    try:
        for frame in _tb.extract_stack()[::-1]:
            if "sea_actions.py" in frame.filename:
                continue
            # Trim repo root if present, leave a short relative-ish path.
            short = frame.filename
            for marker in ("/uwo_v2/", "/site-packages/"):
                if marker in short:
                    short = short.split(marker, 1)[1]
                    break
            return f"{short}:{frame.lineno} in {frame.name}"
    except Exception:
        pass
    return "<unknown>"


def _log_arrow_tap(name: str, xy: tuple[int, int],
                   hold_ms: int | None = None,
                   taps: int | None = None) -> None:
    rec = {
        "ts": _dt.now().isoformat(),
        "name": name,
        "xy": list(xy),
        "hold_ms": hold_ms,
        "taps": taps,
        "callsite": _callsite_above_sea_actions(),
    }
    try:
        with open(_ARROW_TAP_LOG_PATH, "a") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception:
        # Never let logging failure kill the bot.
        pass


# ── Helpers ────────────────────────────────────────────────────────────────


def _do_turn(name: str, xy: tuple[int, int], taps: int) -> SeaActionResult:
    if taps < 1:
        return SeaActionResult(False, name, f"non-positive tap count: {taps}")
    if not _assert_on_sea(name):
        return SeaActionResult(False, name, "not on sea screen")

    logger.info(f"[sea_actions] {name} × {taps} → tap {xy}")
    _log_arrow_tap(name, xy, taps=taps)
    for i in range(taps):
        tap(*xy)
        if i < taps - 1:
            time.sleep(random.uniform(*_INTER_TAP_DELAY_S))

    # No icon-state change expected for L/R, just brief settle.
    time.sleep(_POST_TURN_SETTLE_S)
    return SeaActionResult(True, name, f"{taps} tap(s)")


def _do_hold(name: str, xy: tuple[int, int], duration_ms: int) -> SeaActionResult:
    """Hold the touch at `xy` for `duration_ms` ms via ADB swipe with
    identical start/end coords."""
    if duration_ms < 50:
        return SeaActionResult(False, name, f"hold too short: {duration_ms}ms")
    if duration_ms > 4000:
        # Soft cap — a 4-second sustained hold should be enough for any
        # in-game rotation; longer than that risks anti-cheat scrutiny.
        return SeaActionResult(False, name, f"hold too long: {duration_ms}ms")
    if not _assert_on_sea(name):
        return SeaActionResult(False, name, "not on sea screen")

    import subprocess
    from config.settings import ADB_DEVICE_ID
    cmd = ["adb"]
    if ADB_DEVICE_ID:
        cmd += ["-s", ADB_DEVICE_ID]
    cmd += ["shell", "input", "swipe",
            str(xy[0]), str(xy[1]), str(xy[0]), str(xy[1]),
            str(duration_ms)]
    logger.info(f"[sea_actions] {name} ({duration_ms}ms) → hold {xy}")
    _log_arrow_tap(name, xy, hold_ms=duration_ms)
    try:
        subprocess.run(cmd, check=False, timeout=duration_ms / 1000 + 3)
    except subprocess.TimeoutExpired:
        return SeaActionResult(False, name, "adb swipe timed out")

    time.sleep(_POST_TURN_SETTLE_S)
    return SeaActionResult(True, name, f"held for {duration_ms}ms")


def _assert_on_sea(action_name: str, log_warning: bool = True) -> bool:
    """Refuse to tap a sea-control coordinate when the bot isn't on sea.

    Avoids the 2026-05-24 incident where a tap at the rudder coord
    fired on a post-arrival port_overworld and opened an unrelated
    quest dialog.  All transactional primitives gate on this.

    Fast path: if BotObservation.current().nav is populated with a
    valid ship_heading_deg, that means the most recent perceive cycle
    (or fast nav read) saw the standard sea-HUD ship icon — strong
    proof we're at sea, with NO popup overlaying the rudder zone.
    Skip the full perceive() call in that case.
    """
    # Fast path via cached navigation view.
    try:
        from brain import observation as _obs
        cur = _obs.current()
        if (cur is not None
                and cur.nav is not None
                and getattr(cur.nav, "ship_heading_deg", None) is not None):
            return True
    except Exception:
        pass

    # Slow path — full perceive() to verify state.
    try:
        from brain.perceive import perceive
    except Exception as e:
        if log_warning:
            logger.warning(f"[sea_actions] cannot import perceive: {e}")
        return False
    state = perceive()
    if state.state not in ("sea", "sea_cinematic"):
        if log_warning:
            logger.warning(
                f"[sea_actions] {action_name} refused — state is "
                f"{state.state!r}, not 'sea'.  detail: {state.detail!r}"
            )
        return False
    return True


def _read_icon_state() -> str:
    """Inspect the toggle button and classify as 'rudder' (ship stopped)
    or 'anchor' (ship sailing).

    Delegates to `vision.sea_button_state.classify_sail_button` —
    grayscale MSE against reference crops at the actual button location
    (centred on RUDDER_TOGGLE_XY).  Verified 100% correct across 17
    calibration samples spanning both ship states.

    Returns 'rudder' | 'anchor' | 'unknown'.

    Note: the legacy implementation here cropped at RUDDER_TOGGLE_BBOX,
    which was ~80 px right of the actual button (it landed on plain
    water!), so it matched water-vs-water against the templates and
    classified essentially at random.  Caught 2026-05-30 during
    calibration: sail_start in Phase B was a silent no-op because the
    classifier returned the wrong pre-state.
    """
    from vision.sea_button_state import classify_sail_button
    try:
        frame = capture_screen()
    except Exception as e:
        logger.debug(f"[sea_actions] capture failed: {e}")
        return "unknown"
    state, conf = classify_sail_button(frame)
    if state is None or conf < 0.15:
        logger.debug(
            f"[sea_actions] icon classifier low-confidence "
            f"(state={state!r}, conf={conf:.2f}) → unknown"
        )
        return "unknown"
    return state
