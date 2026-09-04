# actions/adb_actions.py
# ADB input primitives — tap, swipe, key press.
#
# Anti-detection approach:
#   `adb shell input tap` produces an instantaneous, zero-pressure, pixel-perfect
#   event that game anti-cheat SDKs fingerprint easily. Instead, tap() uses
#   `input swipe` from (x,y) to a tiny random offset over a realistic press
#   duration (80–220ms), simulating a real finger that lands, holds briefly,
#   drifts slightly, and lifts. A human-scale inter-action delay follows.

from __future__ import annotations

import math
import random
import shlex
import subprocess
import time

from config.settings import ADB_DEVICE_ID, ADB_TIMEOUT, TAP_DELAY_MIN, TAP_DELAY_MAX

# How long a finger physically presses the screen (milliseconds).
# Real taps measured on Android: ~80–220ms.
PRESS_DURATION_MIN_MS: int = 80
PRESS_DURATION_MAX_MS: int = 220

# Maximum finger drift during a tap (pixels). Finger is never perfectly still.
#
# This is a RADIUS, not a per-axis range, and that distinction is the whole point.
# Drifting each axis independently in [-4,4] lets both draw their maximum at once —
# a 5.66px displacement — and an app that calls anything past ~5px a DRAG rather
# than a click then never fires the control. Four of the 81 combinations (the
# corners) do that: 4.9%.
#
# Measured live 2026-09-02: 3 of 62 taps in one mission produced ZERO pixel change
# — 4.8%. Two were the world map's search box (so the keyboard never opened and
# 'Trip' never reached it), one was a market tile (so the cart stayed empty, the
# Purchase button stayed greyed, and a gather leg ended two rounds short).
# Nothing distinguished them from the 59 that worked except the draw.
#
# A real finger drifts 1-3px during a 150ms press, so bounding the radius is MORE
# faithful than the lattice it replaces, and it keeps every property the anti-
# detection note above is actually about: a press with duration, motion and lift.
TAP_DRIFT_MAX: float = 3.0


def _drift_offset() -> tuple[int, int]:
    """How far the finger slides between touch-down and lift, as (dx, dy).

    A bounded RADIUS in a random direction — never a per-axis draw. Drawing each
    axis independently lets both take their maximum at once, and that corner is
    what the game reads as a drag instead of a click (see TAP_DRIFT_MAX).

    Its own function so the invariant can be tested: `tap` itself is replaced by a
    recorder in the unit suite, which is deliberate — no test may reach the phone.
    """
    drift = random.uniform(0.0, TAP_DRIFT_MAX)
    angle = random.uniform(0.0, 2.0 * math.pi)
    return int(round(drift * math.cos(angle))), int(round(drift * math.sin(angle)))


def _adb(args: list[str]) -> None:
    """Run an ADB shell command, raise on failure."""
    cmd = ["adb"]
    if ADB_DEVICE_ID:
        cmd += ["-s", ADB_DEVICE_ID]
    cmd += args
    result = subprocess.run(cmd, capture_output=True, timeout=ADB_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"ADB error: {result.stderr.decode().strip()}")


def shell_out(args: list[str]) -> str:
    """Run an ADB command and RETURN its stdout. `_adb` discards output; some questions
    (is the soft keyboard up?) are answered by the OS, and the answer is what we need."""
    cmd = ["adb"]
    if ADB_DEVICE_ID:
        cmd += ["-s", ADB_DEVICE_ID]
    cmd += args
    result = subprocess.run(cmd, capture_output=True, timeout=ADB_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"ADB error: {result.stderr.decode().strip()}")
    return result.stdout.decode(errors="replace")


def _human_delay() -> None:
    """Wait a human-scale delay between actions (5–10 s by default)."""
    time.sleep(random.uniform(TAP_DELAY_MIN, TAP_DELAY_MAX))



# ── the world moved because WE moved it ───────────────────────────────────────
# Every action here changes the screen, so whatever was last observed is superseded.
# Observers register to be told; nobody has to remember to say so at the call site, which
# is the whole point — 215 capture sites grew because that discipline was voluntary.
#
# A LIST, not a single slot: a tracer and the perceive repository both want to know, and a
# one-slot hook would have them fight over it.
_action_sinks: list = []


def add_action_sink(fn) -> None:
    """Register `fn(kind, settle_s)`, called after every action primitive."""
    if fn not in _action_sinks:
        _action_sinks.append(fn)


def remove_action_sink(fn) -> None:
    if fn in _action_sinks:
        _action_sinks.remove(fn)


def _acted(kind: str, settle_s: float = 0.0) -> None:
    """Tell the observers the screen has just been changed by us. Never raises."""
    for fn in list(_action_sinks):
        try:
            fn(kind, settle_s)
        except Exception:
            pass          # telling someone must never cost us the action itself


def tap(x: int, y: int) -> None:
    """
    Simulate a finger tap at (x, y).

    Uses `input swipe` with a tiny random drift and realistic press duration
    instead of `input tap`, so the touch-down/hold/touch-up event sequence
    looks like a real finger rather than an instantaneous ADB tap.
    """
    # Record the pre-tap frame + target when an action-trace session is active
    # (debugging: replay a flow frame-by-frame; see actions/action_trace.py).
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(int(x), int(y), "tap")
    except Exception:
        pass

    # Slight coordinate jitter — no human lands on exactly the same pixel
    x += random.randint(-2, 2)
    y += random.randint(-2, 2)

    # Finger drifts slightly during press.
    dx, dy = _drift_offset()
    x2, y2 = x + dx, y + dy

    duration_ms = random.randint(PRESS_DURATION_MIN_MS, PRESS_DURATION_MAX_MS)
    _adb(["shell", "input", "swipe",
          str(x), str(y), str(x2), str(y2), str(duration_ms)])
    _human_delay()
    _acted('tap', 0.8)

def long_press(x: int, y: int, duration_ms: int = 800) -> None:
    """
    Press and hold at (x, y) for duration_ms milliseconds, then release.

    Unity text fields require a long press to trigger showSoftInput() —
    a quick tap is not recognised as a keyboard-show request.
    No anti-cheat delay after (caller controls timing).
    """
    # Recorded like every other input: a trace that omits some primitives reads as if the
    # bot skipped steps, and the missing ones are exactly the hard-to-explain moments
    # (user 2026-08-22: "I am not sure why the steps before it was not recorded").
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(int(x), int(y), "long_press")
    except Exception:
        pass
    _adb(["shell", "input", "swipe",
          str(x), str(y), str(x), str(y), str(duration_ms)])
    time.sleep(0.3)
    _acted('long_press', 1.0)

def tap_fast(x: int, y: int, delay_ms: int = 150) -> None:
    """
    Fast tap for UI elements (panel tabs, buttons, soft keyboard keys).

    Uses `input tap` with a short inter-action delay.  Jitter (±3 px) is
    applied so repeated taps on the same target never land on exactly the
    same pixel.  The delay itself is randomised ±15 % around the requested
    value so the inter-tap cadence is never perfectly uniform.
    """
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(int(x), int(y), "tap")
    except Exception:
        pass
    x += random.randint(-3, 3)
    y += random.randint(-3, 3)
    _adb(["shell", "input", "tap", str(x), str(y)])
    actual_ms = int(delay_ms * random.uniform(0.85, 1.15))
    time.sleep(actual_ms / 1000)
    _acted('tap_fast', 0.4)

def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    """Swipe from (x1, y1) to (x2, y2) over *duration_ms* milliseconds.

    ±4 px jitter on the start position and ±6 px drift on the end position
    so no two swipes are identical.  Duration is randomised ±15 %.
    """
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(int(x1), int(y1), "swipe")
    except Exception:
        pass
    x1 += random.randint(-4, 4)
    y1 += random.randint(-4, 4)
    x2 += random.randint(-6, 6)
    y2 += random.randint(-6, 6)
    actual_ms = int(duration_ms * random.uniform(0.85, 1.15))
    _adb(["shell", "input", "swipe",
          str(x1), str(y1), str(x2), str(y2), str(actual_ms)])
    _human_delay()
    _acted('swipe', 0.8)

def swipe_fast(x1: int, y1: int, x2: int, y2: int,
               duration_ms: int = 300, settle_ms: int = 500) -> None:
    """
    Swipe without the anti-cheat human delay.
    Use for scrolling game UI lists (port list panel, market list, etc.)
    where the target is game UI not game-world input.

    ±4 px jitter on start, ±6 px drift on end, duration ±15 %.
    settle_ms is also randomised ±10 % so the inter-scroll cadence varies.
    """
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(int(x1), int(y1), "swipe")
    except Exception:
        pass
    x1 += random.randint(-4, 4)
    y1 += random.randint(-4, 4)
    x2 += random.randint(-6, 6)
    y2 += random.randint(-6, 6)
    actual_dur = int(duration_ms * random.uniform(0.85, 1.15))
    actual_set = int(settle_ms   * random.uniform(0.90, 1.10))
    _adb(["shell", "input", "swipe",
          str(x1), str(y1), str(x2), str(y2), str(actual_dur)])
    time.sleep(actual_set / 1000)
    _acted('swipe_fast', 0.5)

def press_back() -> None:
    """Send the Android BACK key."""
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(-1, -1, "back")
    except Exception:
        pass
    _adb(["shell", "input", "keyevent", "4"])
    _human_delay()
    _acted('press_back', 1.2)

def wake() -> None:
    """Wake the screen (KEYCODE_WAKEUP) — used before dismissing the lock/
    screensaver, which the game idles into between steps."""
    # Recorded like every other input: a trace that omits some primitives reads as if the
    # bot skipped steps, and the missing ones are exactly the hard-to-explain moments
    # (user 2026-08-22: "I am not sure why the steps before it was not recorded").
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(-1, -1, "wake")
    except Exception:
        pass
    _adb(["shell", "input", "keyevent", "224"])   # KEYCODE_WAKEUP
    _human_delay()
    _acted('wake', 1.5)

def pinch_zoom(
    center_x: int,
    center_y: int,
    start_radius: int = 480,
    end_radius: int = 80,
    steps: int = 10,
    duration_ms: int = 500,
    device: str = "/dev/input/event3",
) -> None:
    """
    Simulate a two-finger horizontal pinch using raw sendevent (multi-touch type B).

    start_radius > end_radius: fingers move together → zoom out.
    start_radius < end_radius: fingers spread apart  → zoom in.

    center_x, center_y: screen-space centre of the gesture (landscape 2400×1080).
    device: touchscreen input device — Pixel 7a: /dev/input/event3 (goodix_ts0).

    Coordinate mapping (Pixel 7a, landscape = 90° CCW from portrait):
        touch_X (ABS_MT_POSITION_X, 0–1079) = 1079 - screen_Y
        touch_Y (ABS_MT_POSITION_Y, 0–2399) = screen_X

    All sendevent commands are bundled into one adb shell call to avoid
    per-event round-trip latency (~30 ms each).
    """
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(int(center_x), int(center_y), "pinch_zoom")
    except Exception:
        pass
    def _to_touch(sx: int, sy: int):
        tx = max(0, min(1079, 1079 - sy))
        ty = max(0, min(2399, sx))
        return tx, ty

    step_delay = duration_ms / steps / 1000.0
    cmds: list[str] = []

    def _ev(ev_type: int, code: int, value: int) -> None:
        cmds.append(f"sendevent {device} {ev_type} {code} {value}")

    for i in range(steps + 1):
        frac = i / steps
        radius = int(start_radius + (end_radius - start_radius) * frac)

        f1x, f1y = _to_touch(center_x - radius, center_y)
        f2x, f2y = _to_touch(center_x + radius, center_y)

        # Slot 0 — finger 1
        _ev(3, 47, 0); _ev(3, 57, 1)            # ABS_MT_SLOT=0, TRACKING_ID=1
        _ev(3, 53, f1x); _ev(3, 54, f1y)        # ABS_MT_POSITION_X/Y
        _ev(3, 58, 50)                           # ABS_MT_PRESSURE

        # Slot 1 — finger 2
        _ev(3, 47, 1); _ev(3, 57, 2)            # ABS_MT_SLOT=1, TRACKING_ID=2
        _ev(3, 53, f2x); _ev(3, 54, f2y)
        _ev(3, 58, 50)

        _ev(0, 0, 0)                             # SYN_REPORT

        if i < steps:
            cmds.append(f"sleep {step_delay:.3f}")

    # Lift both fingers
    _ev(3, 47, 0); _ev(3, 57, -1)               # Slot 0 lifted
    _ev(3, 47, 1); _ev(3, 57, -1)               # Slot 1 lifted
    _ev(0, 0, 0)                                 # SYN_REPORT

    shell_cmd = "; ".join(cmds)
    cmd = ["adb"]
    if ADB_DEVICE_ID:
        cmd += ["-s", ADB_DEVICE_ID]
    cmd += ["shell", shell_cmd]
    result = subprocess.run(cmd, capture_output=True,
                            timeout=ADB_TIMEOUT + duration_ms / 1000 + 2)
    time.sleep(0.3)
    if result.returncode != 0:
        raise RuntimeError(f"pinch_zoom sendevent error: {result.stderr.decode().strip()}")
    _acted("pinch_zoom", 1.0)


def input_text(text: str, max_chars: int | None = None, clear_first: bool = False) -> None:
    """
    Type text into the currently focused input field, per-character.

    Uses per-character keyevent instead of batch 'input text' for compatibility
    with Unity game text fields — Unity only handles KeyEvent.ACTION_DOWN/UP
    and ignores the ACTION_MULTIPLE batch-insert sent by 'adb shell input text'.

    Anti-cheat (user 2026-08-18): the game fingerprints automated input, so type at
    a JITTERED human interval — never a fixed cadence.  `max_chars` types only the
    first N characters: for a search box, a 3–4 char prefix filters the list enough
    to find the target without typing the whole word (less input = lower risk).
    `clear_first` wipes any leftover text (move-to-end + deletes) before typing —
    without it a second search appends onto the first and matches nothing.
    """
    # Recorded like every other input: a trace that omits some primitives reads as if the
    # bot skipped steps, and the missing ones are exactly the hard-to-explain moments
    # (user 2026-08-22: "I am not sure why the steps before it was not recorded").
    try:
        from actions import action_trace
        if action_trace.active():
            action_trace.record_tap(-1, -1, "input_text")
    except Exception:
        pass
    # Android named keycodes for printable characters
    _KEYCODE: dict[str, str] = {
        'a': 'KEYCODE_A', 'b': 'KEYCODE_B', 'c': 'KEYCODE_C', 'd': 'KEYCODE_D',
        'e': 'KEYCODE_E', 'f': 'KEYCODE_F', 'g': 'KEYCODE_G', 'h': 'KEYCODE_H',
        'i': 'KEYCODE_I', 'j': 'KEYCODE_J', 'k': 'KEYCODE_K', 'l': 'KEYCODE_L',
        'm': 'KEYCODE_M', 'n': 'KEYCODE_N', 'o': 'KEYCODE_O', 'p': 'KEYCODE_P',
        'q': 'KEYCODE_Q', 'r': 'KEYCODE_R', 's': 'KEYCODE_S', 't': 'KEYCODE_T',
        'u': 'KEYCODE_U', 'v': 'KEYCODE_V', 'w': 'KEYCODE_W', 'x': 'KEYCODE_X',
        'y': 'KEYCODE_Y', 'z': 'KEYCODE_Z',
        ' ': 'KEYCODE_SPACE',
        '-': 'KEYCODE_MINUS',
        "'": 'KEYCODE_APOSTROPHE',
        '.': 'KEYCODE_PERIOD',
    }

    if clear_first:
        # Wipe leftover text: jump to end, then delete a generous number of chars.
        _adb(["shell", "input", "keyevent", "KEYCODE_MOVE_END"])
        for _ in range(20):
            _adb(["shell", "input", "keyevent", "KEYCODE_DEL"])
            time.sleep(random.uniform(0.03, 0.09))

    to_type = text.lower()[:max_chars] if max_chars else text.lower()
    # Send each character as an individual key event (lowercase — search is case-insensitive)
    for char in to_type:
        code = _KEYCODE.get(char)
        if code is not None:
            _adb(["shell", "input", "keyevent", str(code)])
        else:
            # Fallback for unmapped characters (digits, accented letters, etc.)
            #
            # QUOTED, BECAUSE `adb shell` RUNS A SHELL ON THE DEVICE. The argument list here
            # avoids the LOCAL shell, which is easy to mistake for safety — but adb hands the
            # command to a shell on the phone, and that one expands globs against ITS cwd,
            # which is `/`.
            #
            # Live 2026-09-04: a recovery sailed to an unresolved home port named '?'. `?` is
            # not in _KEYCODE, so it came here, and `input text ?` matched the single-character
            # entries of `/` — where Android keeps the `/d` symlink. The search box received
            # the letter **d** and the port list filtered to `Diu`. A wrong destination typed
            # from a wildcard, with nothing in the log to explain it (user spotted the stray
            # 'd' in the box).
            #
            # `?`, `*` and `[` are the ones that expand; quoting covers all of them at once.
            escaped = shlex.quote(char.replace(" ", "%s"))
            _adb(["shell", "input", "text", escaped])
        time.sleep(random.uniform(0.12, 0.35))   # jittered human interval (anti-cheat)
    time.sleep(random.uniform(0.3, 0.6))
    _acted('input_text', 0.8)
