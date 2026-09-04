# actions/orientation.py
# Orientation guard.
#
# The game is landscape-locked but follows the sensor, so with Android
# auto-rotate ON it sits in ROTATION_90 or ROTATION_270 depending on how the
# phone is physically held.  The game bakes a different notch/safe-area X-offset
# per screen at the instant of each world-switch (sea/port/building/menu
# transition), from the orientation active then — so ALL hardcoded UI
# coordinates are calibrated for exactly one orientation (CANONICAL_ROTATION).
# A world-switch in the wrong orientation silently mis-taps every absolute coord
# (this was the trading-flow breakage).  See uwo_bot AUDIT.md (trading_revisit).
#
# This guard locks auto-rotate off (so a physically-fixed phone can't drift) and
# asserts the display is at CANONICAL_ROTATION before the bot taps anything.

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Optional

from config.settings import ADB_DEVICE_ID, ADB_TIMEOUT, CANONICAL_ROTATION

logger = logging.getLogger(__name__)

_ROTATION_RE = re.compile(r"mDisplayRotation=ROTATION_(\d+)")


class OrientationError(RuntimeError):
    """Raised when the display isn't at the canonical calibration rotation."""


def _adb_out(args: list[str]) -> str:
    """Run an ADB command and return its stdout as text ('' on failure)."""
    cmd = ["adb"]
    if ADB_DEVICE_ID:
        cmd += ["-s", ADB_DEVICE_ID]
    cmd += args
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=ADB_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — device may be unplugged mid-run
        logger.warning(f"[orientation] adb {args[:2]} failed: {e}")
        return ""
    return result.stdout.decode(errors="replace")


def _parse_rotation(dumpsys_output: str) -> Optional[int]:
    """Extract the game-surface rotation in degrees (0/90/180/270) from
    `dumpsys window displays` output.  Returns None if not present."""
    m = _ROTATION_RE.search(dumpsys_output)
    return int(m.group(1)) if m else None


def get_display_rotation() -> Optional[int]:
    """Current game-surface rotation in degrees, or None if unreadable."""
    return _parse_rotation(_adb_out(["shell", "dumpsys", "window", "displays"]))


def is_autorotate_on() -> bool:
    """True if Android auto-rotate (accelerometer_rotation) is enabled."""
    return _adb_out(
        ["shell", "settings", "get", "system", "accelerometer_rotation"]
    ).strip() == "1"


def unlock_autorotate() -> None:
    """Hand rotation back to the sensor, so a physical position can take effect."""
    _adb_out(["shell", "settings", "put", "system",
              "accelerometer_rotation", "1"])


def lock_autorotate() -> None:
    """Disable Android auto-rotate so the game can't flip orientation."""
    _adb_out(["shell", "settings", "put", "system",
              "accelerometer_rotation", "0"])


def ensure_canonical_orientation(raise_on_mismatch: bool = True) -> bool:
    """Lock auto-rotate off and assert the display is at CANONICAL_ROTATION.

    Locks auto-rotate first (idempotent) so a physically-fixed phone cannot
    drift mid-session, then checks the rotation.  On mismatch the phone must be
    PHYSICALLY rotated to the canonical landscape — adb cannot force a specific
    landscape for a sensor-locked app — so we escalate rather than tap into the
    wrong notch offset: raise OrientationError (default) or log and return False.

    Returns True when the display is at CANONICAL_ROTATION.
    """
    # LOCK ONLY ONCE CANONICAL. Locking first is what made this guard unfixable: a run that
    # started while the display sat at ROTATION_90 disabled the sensor, freezing it there,
    # and then asked for a PHYSICAL rotation that could no longer take effect — the game
    # follows the sensor, and with auto-rotate off there is no sensor reading to follow at
    # the next world switch. Live 2026-08-25: `user_rotation=3` plus a full
    # overworld->world-map->overworld round trip left it at 90; enabling the sensor moved it
    # to 270 immediately, the phone having been in the right position the whole time.
    rot = get_display_rotation()
    if rot != CANONICAL_ROTATION and not is_autorotate_on():
        logger.warning(f"[orientation] display is ROTATION_{rot} with auto-rotate OFF — "
                       "handing rotation back to the sensor so the phone's real position "
                       "can take effect")
        unlock_autorotate()
        time.sleep(1.5)
        rot = get_display_rotation()

    if rot == CANONICAL_ROTATION:
        # Now it is safe to freeze: locking here pins the CORRECT orientation.
        if is_autorotate_on():
            logger.info("[orientation] at the canonical rotation — locking auto-rotate off "
                        "so the game cannot flip mid-session")
            lock_autorotate()
        return True

    msg = (f"[orientation] display is ROTATION_{rot}, expected "
           f"ROTATION_{CANONICAL_ROTATION}.  All hardcoded UI coords are "
           f"calibrated for ROTATION_{CANONICAL_ROTATION}; tapping now would "
           f"mis-hit by the per-screen notch offset.  Auto-rotate has been left "
           f"ON so the sensor can follow: physically rotate the phone to the "
           f"canonical landscape, then rerun.")
    if raise_on_mismatch:
        raise OrientationError(msg)
    logger.error(msg)
    return False
