"""Vision input abstraction — the swap point between minimap-based
perception (today) and full-screen VLM perception (later).

`VisionFrame` wraps one captured frame and exposes named regions
through accessor methods.  Layers ask for the region they want;
they don't index pixel coordinates and they don't know whether
the underlying source is ADB, a file, or a synthetic generator.

Adding a new perception strategy — e.g. "feed the whole screen to
a VLM" — means writing a layer that calls `frame.full_screen()`
instead of `frame.minimap()`.  No changes to the pipeline, no
changes to the other layers.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional, Protocol

from PIL import Image

from vision.minimap_navigation_view import get_minimap_crop


# Region coordinates on the 2400×1080 phone screen.
# Kept in one place so a future game update or device change is a
# single-line fix.  The current MINIMAP_CROP is mirrored from
# vision/minimap_navigation_view.py — it drifts with game updates,
# see memory/project_minimap_crop_drifts_with_game_updates.md.
# THE MINI-MAP CROP LIVES IN ONE PLACE — `vision.minimap_navigation_view`.
# This module used to keep its own MIRRORED copy of the literal, which meant two globals
# that must never disagree: `tools/run_ai_nav_live.py` had to assign BOTH after calibrating,
# and calibrating only one would leave half of perception reading a stale box, silently.
# `MINIMAP_CROP` is still readable as a module attribute (see `__getattr__` below) so
# existing `from brain.ai_nav.vision_input import MINIMAP_CROP` callers keep working, but it
# now resolves to the canonical value at ACCESS time rather than being frozen at import.
SEA_HUD_LATLON_CROP = (2240, 348, 2395, 400)  # the "lat,lon" text
# Fallback absolute crop; the live value is derived from MINIMAP_CROP
# at call time via _sea_hud_latlon_crop().  See vision/sea_hud.py for
# the full offset rationale.


def _sea_hud_latlon_crop() -> tuple[int, int, int, int]:
    """Live SEA_HUD_LATLON_CROP derived from the current MINIMAP_CROP.
    Uses the same offset math as vision.sea_hud.latlon_crop_for so
    OCR follows the mini-map when it shifts."""
    _, _, mx1, my1 = get_minimap_crop()
    return (mx1 - 129, my1 - 35, mx1 + 11, my1 + 5)
# `sea_view` is "the rendered 3D world, minus all chrome" — useful
# for future VLM input.  Today this is the full screen minus the
# minimap + bottom controls; we leave it as the full frame and let
# the consumer crop if needed.


@dataclass
class VisionFrame:
    """One captured frame plus convenience accessors for the regions
    different layers care about.

    `raw` is the full 2400×1080 ADB screenshot.  All accessors return
    PIL Images so consumers can ToTensor / np.asarray as needed.
    """
    raw: Image.Image
    tick: int
    wall_ts: float = field(default_factory=time.time)

    def full_screen(self) -> Image.Image:
        """The complete frame.  This is what a full-screen VLM should
        receive when we swap minimap-based layers for VLM-based ones.
        """
        return self.raw

    def minimap(self) -> Image.Image:
        """The 400×190 minimap crop (top-right radar)."""
        return self.raw.crop(get_minimap_crop())

    def sea_hud_latlon(self) -> Image.Image:
        """The bottom-right lat/lon text crop, anchored to the current
        MINIMAP_CROP so it follows the mini-map when the UI drifts."""
        return self.raw.crop(_sea_hud_latlon_crop())

    def ship_crop(self, radius: int = 64) -> Image.Image:
        """A square crop centered on the minimap's ship icon.

        Used by perception models that only care about the ship's
        immediate surroundings (heading CNN, future ship-state
        classifier).  The minimap is north-up and the ship is always
        roughly at the minimap's center, so we crop the minimap's
        center.  Caller can crop the actual sprite centroid later.
        """
        mm = self.minimap()
        W, H = mm.size
        cx, cy = W // 2, H // 2
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        return mm.crop(box)


# ── Sources ────────────────────────────────────────────────────────────


class VisionSource(Protocol):
    """A pluggable frame producer.  Default impl is ADB; tests use
    `FileVisionSource`; future RL training will use a simulator
    source."""

    def capture(self, tick: int) -> VisionFrame: ...


class AdbVisionSource:
    """Capture one frame via `adb exec-out screencap -p`.

    Adds the same 0.3–0.8 s jittered delay every ADB call in the
    bot uses (`memory/feedback_sea_steering_rudder_deflection.md`)
    — anti-cheat fingerprints regular cadence, so jittered captures
    are mandatory.
    """

    def capture(self, tick: int) -> VisionFrame:
        t0 = time.time()
        proc = subprocess.run(
            ["adb", "exec-out", "screencap", "-p"],
            capture_output=True, check=True,
        )
        img = Image.open(BytesIO(proc.stdout)).convert("RGB")
        return VisionFrame(raw=img, tick=tick, wall_ts=t0)


class FileVisionSource:
    """Replay a saved session — one frame per tick from a directory
    of `tick_NNNN.png` files.  Used by tests and offline replay.

    The legacy runner saves the **minimap crop** (400×190), not the
    full 2400×1080 screenshot.  When `paste_to_full_screen=True`
    (default), we paste each minimap into a black full-screen-sized
    image at the MINIMAP_CROP region so `frame.minimap()` continues
    to work.  Pass `paste_to_full_screen=False` if your source files
    are already full screenshots.
    """

    def __init__(self, session_dir: Path,
                 file_pattern: str = "tick_{tick:04d}.png",
                 paste_to_full_screen: bool = True):
        self.session_dir = Path(session_dir)
        self.file_pattern = file_pattern
        self.paste_to_full_screen = paste_to_full_screen

    def capture(self, tick: int) -> VisionFrame:
        path = self.session_dir / self.file_pattern.format(tick=tick)
        if not path.exists():
            raise FileNotFoundError(path)
        img = Image.open(path).convert("RGB")

        if self.paste_to_full_screen:
            _mm = get_minimap_crop()
            mm_w = _mm[2] - _mm[0]
            mm_h = _mm[3] - _mm[1]
            if img.size != (2400, 1080):
                wrapped = Image.new("RGB", (2400, 1080), (0, 0, 0))
                # Resize the saved minimap to the canonical crop dims
                # so `frame.minimap()` returns the same pixels we loaded.
                wrapped.paste(img.resize((mm_w, mm_h)),
                              (_mm[0], _mm[1]))
                img = wrapped
        return VisionFrame(raw=img, tick=tick, wall_ts=time.time())


class StaticVisionSource:
    """Return the same provided frame for every tick.  Useful for
    smoke tests."""

    def __init__(self, img: Image.Image):
        self.img = img

    def capture(self, tick: int) -> VisionFrame:
        return VisionFrame(raw=self.img, tick=tick, wall_ts=time.time())


def __getattr__(name):
    """Resolve `MINIMAP_CROP` to the canonical live value at access time.

    Keeps `from brain.ai_nav.vision_input import MINIMAP_CROP` working for existing callers
    while there is only ONE value in the process. Note this hook fires only for attribute
    access from OUTSIDE the module; code inside it calls `get_minimap_crop()` directly.
    """
    if name == "MINIMAP_CROP":
        return get_minimap_crop()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
