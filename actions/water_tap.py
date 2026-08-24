"""Water-tap localization on the world map.

When the player taps any point on navigable water in the world map, the
game displays the latitude/longitude of that tap point next to the Move
button.  This gives the bot an *absolute* anchor — a direct (pixel ↔
real-world coord) mapping — without depending on port labels being
visible.

This module provides:
  • `read_latlon_from_frame(frame)` — OCR the lat/lon readout on a frame
    where a water-tap has already been performed.  No tap.
  • `tap_water_and_read_latlon(px, py)` — tap (px, py), wait for the
    Move panel to appear, OCR the readout, return (lat, lon).
  • `localize_camera_via_water_tap(safe_pixels)` — try a list of
    candidate water-tap pixels until one yields a readout, return the
    pixel that worked and the (lat, lon) it produced.

The readout is `(lat, lon)` in plain decimal degrees, e.g.
`40.19, -9.91` for Lisbon.  Lon is positive east of Greenwich, lat
positive north of the equator.  Both can be negative.

Design notes:
  • The crop region around the readout is generous and tunable
    (`READOUT_CROP`).  Run any caller with `debug_dir` set and inspect
    the saved crop PNG to verify it actually frames the text.
  • Tap-on-land has no readout — `read_latlon_from_frame` returns None
    and the caller can retry from another candidate pixel.
  • The "You cannot move to that location" ribbon (transient, ~1-2s) is
    not used as a signal — by the time we tick again it's usually gone.
    Absence of a readout is the reliable negative.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple

import numpy as np
from PIL import Image

from capture.adb_capture import capture_screen
from actions.adb_actions import tap as _adb_tap
from vision.ocr import _get_reader

from loguru import logger


# Two readout locations exist for the same lat/lon value:
#
#   WORLD_MAP_READOUT_CROP  — small text near the Move button on the
#       world map, shown after tapping water.  This is the *planning*
#       readout: the bot taps a candidate pixel and reads the coord
#       BEFORE committing to a voyage.  Text is small (~28 px tall);
#       OCR is more failure-prone.
#
#   SAILING_HUD_READOUT_CROP — large pill near the bottom-center of the
#       sailing HUD, shown while the ship is actively sailing to a
#       destination.  Text is ~50 px tall; OCR is nearly always clean.
#       Useful as a *verification* readout: after the bot taps Move on
#       the world map, the same value should appear here, much larger.
#
# Both are on 2400×1080 landscape frames.  Adjust by saving a debug
# crop (pass debug_dir=…) and inspecting the PNG.
WORLD_MAP_READOUT_CROP   = (900, 860, 1500, 960)
SAILING_HUD_READOUT_CROP = (1030, 940, 1390, 1020)


# Pattern: two signed decimal numbers separated by a comma (or comma-
# space).  Accepts both "40.19, -9.91" and "40,19  -9,91" (some locales
# render decimal-comma).  The minus may also OCR as an en-dash or hyphen.
_NUM = r"[\-–—]?\s*\d{1,3}[.,]\d{1,3}"
_LATLON_RE = re.compile(
    rf"({_NUM})\s*[,\s]\s*({_NUM})"
)

# Fallback pattern for the common OCR failure where the lon's leading
# minus sign is misread as `.` or `,` (collapsed into the comma separator
# between lat and lon).  Examples seen on live readouts:
#   real "34.88, -51.51" → OCR "34.88,.51.51"
#   real "35.87, -53.27" → OCR "35.87,,53.27"
# When the matched character between the lat-comma and the lon digits
# is `.` or `,`, treat the lon as negative.
_LATLON_OCR_MANGLED_RE = re.compile(
    r"(?P<lat>[\-–—]?\s*\d{1,3}[.,]\d{1,3})"
    r"\s*,\s*"
    r"(?P<sign>[.,])"
    r"(?P<lon_int>\d{1,3})"
    r"\.(?P<lon_frac>\d{1,3})"
)


# A handful of candidate tap pixels inside the map safe rect, chosen so
# that *at least one* is statistically very likely to be on water for
# any reasonable view of the world map.  Spread across the safe rect to
# avoid local clusters where many candidates fall on the same island.
# Map safe rect on 2400×1080: x ∈ [250, 2150], y ∈ [200, 900].
WATER_TAP_CANDIDATES: tuple[Tuple[int, int], ...] = (
    (1200,  550),   # dead-center
    (1500,  450),   # right of center, upper
    ( 900,  650),   # left of center, lower
    (1700,  650),   # far right, lower
    ( 600,  450),   # far left, upper
    (1200,  300),   # high-center
    (1200,  800),   # low-center
    (1900,  500),   # far right, mid
)


def _parse_latlon_from_text(text: str) -> Optional[Tuple[float, float]]:
    """Pull the (lat, lon) pair out of a free-form OCR string.

    Returns None when no two-number pattern is found, or when the parsed
    numbers are outside the world-map's plausible range.  Lat ∈ [-90, 90],
    Lon ∈ [-180, 180].  Anything outside is a misread.
    """
    if not text:
        return None
    # Normalise common OCR substitutions.
    cleaned = (
        text.replace("–", "-")
            .replace("—", "-")
            .replace("(", " ")
            .replace(")", " ")
    )
    m = _LATLON_RE.search(cleaned)
    if m:
        raw_lat, raw_lon = m.group(1), m.group(2)
        try:
            # Some locales render decimal-comma.  Accept "40,19" as 40.19.
            lat = float(raw_lat.replace(",", "."))
            lon = float(raw_lon.replace(",", "."))
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    # Fallback: minus sign mangled into a `.` or `,` by OCR.
    m2 = _LATLON_OCR_MANGLED_RE.search(cleaned)
    if m2:
        try:
            lat = float(m2.group("lat").replace(",", "."))
            lon = -float(f"{m2.group('lon_int')}.{m2.group('lon_frac')}")
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    return None


def _ocr_latlon_in_crop(
    frame: Image.Image,
    crop_box: tuple[int, int, int, int],
    debug_dir: Optional[Path],
    debug_tag: str,
) -> Optional[Tuple[float, float]]:
    """Shared OCR+parse for any lat/lon readout crop."""
    crop = frame.crop(crop_box)
    arr = np.array(crop)
    raw = _get_reader().readtext(arr, detail=1)
    tokens = [(text, float(conf)) for _, text, conf in raw if conf >= 0.20]
    joined = " ".join(t for t, _ in tokens)
    parsed = _parse_latlon_from_text(joined)

    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%H%M%S")
        crop.save(debug_dir / f"latlon_{debug_tag}_{ts}.png")
        (debug_dir / f"latlon_{debug_tag}_{ts}.txt").write_text(
            f"tokens: {tokens!r}\njoined: {joined!r}\nparsed: {parsed!r}\n"
        )

    if parsed is None:
        logger.debug(
            f"[water-tap:{debug_tag}] no lat/lon parsed — tokens={tokens!r}"
        )
    else:
        logger.info(f"[water-tap:{debug_tag}] read lat/lon = {parsed}")
    return parsed


def read_latlon_from_frame(
    frame: Image.Image,
    debug_dir: Optional[Path] = None,
) -> Optional[Tuple[float, float]]:
    """OCR the lat/lon readout on a world-map frame where a water-tap
    has already been performed.

    Returns None when no readout is visible (caller tapped on land, or
    the Move panel hasn't appeared yet — try again or pick another pixel).

    Passing `debug_dir` writes the cropped readout region and the raw
    OCR tokens to disk for tuning WORLD_MAP_READOUT_CROP.
    """
    return _ocr_latlon_in_crop(
        frame, WORLD_MAP_READOUT_CROP, debug_dir, "worldmap",
    )


def read_destination_latlon_from_sailing_hud(
    frame: Image.Image,
    debug_dir: Optional[Path] = None,
) -> Optional[Tuple[float, float]]:
    """OCR the destination lat/lon shown in the sailing HUD while the
    ship is actively sailing to a coordinate.

    Useful as a verification channel: text is much larger than the
    world-map readout (~50 px vs ~28 px), so OCR is nearly always
    clean.  Returns None when the HUD is not currently showing a
    destination coord (e.g. the ship is moored or sailing to a port,
    not a coord).
    """
    return _ocr_latlon_in_crop(
        frame, SAILING_HUD_READOUT_CROP, debug_dir, "sailinghud",
    )


def _on_world_map(frame=None) -> bool:
    """True only when the WORLD MAP is on screen."""
    try:
        from actions.sail_actions import _is_on_world_map
        from capture.adb_capture import capture_screen as _cap
        return bool(_is_on_world_map(frame if frame is not None else _cap()))
    except Exception as exc:
        logger.debug(f"[water-tap] world-map check failed: {exc}")
        return False


def tap_water_and_read_latlon(
    px: int, py: int,
    settle_s: float = 0.9,
    debug_dir: Optional[Path] = None,
    require_world_map: bool = True,
) -> Optional[Tuple[float, float]]:
    """Tap (px, py) and read the lat/lon shown by the Move panel.

    Returns None when the tap landed on land/port-icon (no readout
    appears) or OCR failed to parse the text.  Caller should try
    another pixel from WATER_TAP_CANDIDATES.

    GATED ON LOCATION. This looks like a read, but it is a TAP at a fixed screen point,
    and CLAUDE.md is explicit that such primitives must assert where they are first. Left
    ungated it fired eight scattered taps into the SEA VIEW on 2026-08-21 — the caller
    believed it was localising a world-map camera — and one of them landed in the sea
    destination list, which is the most likely reason the fleet set course for a port
    nobody asked for. A localisation routine must never be able to steer the ship.
    """
    if require_world_map and not _on_world_map():
        logger.warning(f"[water-tap] NOT on the world map — refusing to tap ({px},{py}); "
                       "these coordinates mean nothing off the map and can hit live "
                       "controls (see the 2026-08-21 sea-view taps)")
        return None
    logger.info(f"[water-tap] tapping ({px}, {py}) to read lat/lon")
    _adb_tap(px, py)
    time.sleep(settle_s)
    frame = capture_screen()
    return read_latlon_from_frame(frame, debug_dir=debug_dir)


def localize_camera_via_water_tap(
    candidates: Iterable[Tuple[int, int]] = WATER_TAP_CANDIDATES,
    debug_dir: Optional[Path] = None,
) -> Optional[Tuple[Tuple[int, int], Tuple[float, float]]]:
    """Try a list of candidate water-tap pixels until one yields a
    readout.  Returns ((px, py), (lat, lon)) — the pixel that worked
    and the coords it produced — or None if every candidate hit land.

    The first hit wins; we don't fan out.  Order WATER_TAP_CANDIDATES
    by prior likelihood of being on water (center first, edges last).
    """
    for px, py in candidates:
        latlon = tap_water_and_read_latlon(px, py, debug_dir=debug_dir)
        if latlon is not None:
            return (px, py), latlon
    logger.warning(
        "[water-tap] every candidate pixel hit land/icon — "
        "no localization possible from this view"
    )
    return None
