# capture/adb_capture.py
# Captures the Android screen via ADB and returns PIL Images.
# Run standalone to verify connectivity: python -m capture.adb_capture

from __future__ import annotations

import struct
import subprocess
import io
from PIL import Image

from config.settings import ADB_DEVICE_ID, ADB_TIMEOUT


def _adb(args: list[str]) -> bytes:
    """Run an ADB command and return raw stdout bytes."""
    cmd = ["adb"]
    if ADB_DEVICE_ID:
        cmd += ["-s", ADB_DEVICE_ID]
    cmd += args
    result = subprocess.run(cmd, capture_output=True, timeout=ADB_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"ADB error: {result.stderr.decode().strip()}")
    return result.stdout


def _capture_screen_raw() -> Image.Image | None:
    """Capture via `screencap` (no -p) — skips phone-side PNG encoding,
    cuts transfer time roughly in half on USB.  Returns None on any parse
    failure so the caller can fall back to PNG.

    Header layout (Android ≥ 9): width(u32), height(u32), pixel_format(u32),
    color_dataspace(u32) — 16 bytes.  Pre-9 devices use a 12-byte header
    without the dataspace field.
    """
    raw = _adb(["exec-out", "screencap"])
    for hdr_len in (16, 12):
        if len(raw) < hdr_len + 4:
            continue
        w, h = struct.unpack('<II', raw[0:8])
        if not (1000 < w < 5000 and 500 < h < 3000):
            continue
        expected_rgba = w * h * 4
        if len(raw) - hdr_len != expected_rgba:
            continue
        return Image.frombuffer(
            'RGBA', (w, h), raw[hdr_len:], 'raw', 'RGBA', 0, 1
        ).convert('RGB')
    return None


def capture_screen() -> Image.Image:
    """Take a screenshot via ADB and return it as a PIL Image.

    Tries the raw RGBA path first (no phone-side PNG encode).  Falls
    back to `screencap -p` if the raw header doesn't parse (older
    Android, format change, etc.).
    """
    try:
        img = _capture_screen_raw()
        if img is not None:
            return img
    except Exception:
        pass
    raw = _adb(["exec-out", "screencap", "-p"])
    return Image.open(io.BytesIO(raw)).convert("RGB")


if __name__ == "__main__":
    img = capture_screen()
    img.save("debug_capture.png")
    print(f"Captured: {img.size}")
