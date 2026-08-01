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

import random
import subprocess
import time

from config.settings import ADB_DEVICE_ID, ADB_TIMEOUT, TAP_DELAY_MIN, TAP_DELAY_MAX

# How long a finger physically presses the screen (milliseconds).
# Real taps measured on Android: ~80–220ms.
PRESS_DURATION_MIN_MS: int = 80
PRESS_DURATION_MAX_MS: int = 220

# Maximum finger drift during a tap (pixels). Finger is never perfectly still.
TAP_DRIFT_MAX: int = 4


def _adb(args: list[str]) -> None:
    """Run an ADB shell command, raise on failure."""
    cmd = ["adb"]
    if ADB_DEVICE_ID:
        cmd += ["-s", ADB_DEVICE_ID]
    cmd += args
    result = subprocess.run(cmd, capture_output=True, timeout=ADB_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"ADB error: {result.stderr.decode().strip()}")


def _human_delay() -> None:
    """Wait a human-scale delay between actions (5–10 s by default)."""
    time.sleep(random.uniform(TAP_DELAY_MIN, TAP_DELAY_MAX))


def tap(x: int, y: int) -> None:
    """
    Simulate a finger tap at (x, y).

    Uses `input swipe` with a tiny random drift and realistic press duration
    instead of `input tap`, so the touch-down/hold/touch-up event sequence
    looks like a real finger rather than an instantaneous ADB tap.
    """
    # Slight coordinate jitter — no human lands on exactly the same pixel
    x += random.randint(-2, 2)
    y += random.randint(-2, 2)

    # Finger drifts slightly during press
    x2 = x + random.randint(-TAP_DRIFT_MAX, TAP_DRIFT_MAX)
    y2 = y + random.randint(-TAP_DRIFT_MAX, TAP_DRIFT_MAX)

    duration_ms = random.randint(PRESS_DURATION_MIN_MS, PRESS_DURATION_MAX_MS)
    _adb(["shell", "input", "swipe",
          str(x), str(y), str(x2), str(y2), str(duration_ms)])
    _human_delay()


def long_press(x: int, y: int, duration_ms: int = 800) -> None:
    """
    Press and hold at (x, y) for duration_ms milliseconds, then release.

    Unity text fields require a long press to trigger showSoftInput() —
    a quick tap is not recognised as a keyboard-show request.
    No anti-cheat delay after (caller controls timing).
    """
    _adb(["shell", "input", "swipe",
          str(x), str(y), str(x), str(y), str(duration_ms)])
    time.sleep(0.3)


def tap_fast(x: int, y: int, delay_ms: int = 150) -> None:
    """
    Fast tap for UI elements (panel tabs, buttons, soft keyboard keys).

    Uses `input tap` with a short inter-action delay.  Jitter (±3 px) is
    applied so repeated taps on the same target never land on exactly the
    same pixel.  The delay itself is randomised ±15 % around the requested
    value so the inter-tap cadence is never perfectly uniform.
    """
    x += random.randint(-3, 3)
    y += random.randint(-3, 3)
    _adb(["shell", "input", "tap", str(x), str(y)])
    actual_ms = int(delay_ms * random.uniform(0.85, 1.15))
    time.sleep(actual_ms / 1000)


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    """Swipe from (x1, y1) to (x2, y2) over *duration_ms* milliseconds.

    ±4 px jitter on the start position and ±6 px drift on the end position
    so no two swipes are identical.  Duration is randomised ±15 %.
    """
    x1 += random.randint(-4, 4)
    y1 += random.randint(-4, 4)
    x2 += random.randint(-6, 6)
    y2 += random.randint(-6, 6)
    actual_ms = int(duration_ms * random.uniform(0.85, 1.15))
    _adb(["shell", "input", "swipe",
          str(x1), str(y1), str(x2), str(y2), str(actual_ms)])
    _human_delay()


def swipe_fast(x1: int, y1: int, x2: int, y2: int,
               duration_ms: int = 300, settle_ms: int = 500) -> None:
    """
    Swipe without the anti-cheat human delay.
    Use for scrolling game UI lists (port list panel, market list, etc.)
    where the target is game UI not game-world input.

    ±4 px jitter on start, ±6 px drift on end, duration ±15 %.
    settle_ms is also randomised ±10 % so the inter-scroll cadence varies.
    """
    x1 += random.randint(-4, 4)
    y1 += random.randint(-4, 4)
    x2 += random.randint(-6, 6)
    y2 += random.randint(-6, 6)
    actual_dur = int(duration_ms * random.uniform(0.85, 1.15))
    actual_set = int(settle_ms   * random.uniform(0.90, 1.10))
    _adb(["shell", "input", "swipe",
          str(x1), str(y1), str(x2), str(y2), str(actual_dur)])
    time.sleep(actual_set / 1000)


def press_back() -> None:
    """Send the Android BACK key."""
    _adb(["shell", "input", "keyevent", "4"])
    _human_delay()


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


def input_text(text: str) -> None:
    """
    Type text into the currently focused input field.
    Clears the field first (select-all + delete), then types the new value.

    Uses per-character keyevent instead of batch 'input text' for compatibility
    with Unity game text fields — Unity only handles KeyEvent.ACTION_DOWN/UP
    and ignores the ACTION_MULTIPLE batch-insert sent by 'adb shell input text'.
    """
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

    # Send each character as an individual key event (lowercase — search is case-insensitive)
    for char in text.lower():
        code = _KEYCODE.get(char)
        if code is not None:
            _adb(["shell", "input", "keyevent", str(code)])
        else:
            # Fallback for unmapped characters (digits, accented letters, etc.)
            escaped = char.replace(" ", "%s")
            _adb(["shell", "input", "text", escaped])
        time.sleep(0.05)
    time.sleep(0.2)
