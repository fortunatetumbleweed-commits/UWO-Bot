"""Region-cluster perception: one pass that says, per semantic UI region, whether it
holds a control cluster + its bbox + a cheap change-signature.

Rotation-robust: regions are NORMALIZED fractions and we cluster the ACTUAL detected
elements inside them, so the 118px camera-notch shift is absorbed (no absolute crops).
Serves BOTH localization (read the TITLE cluster wherever it is) and cheap structural
state-detection (dialog = CENTRE popup; panel toggle = RIGHT flip; sea vs port =
LOWER_LEFT steering wheel present or not). See docs/ui_region_cluster_perception_design.md.

Phase 1 is deterministic (reuses OmniParser elements + a couple of structural checks);
a trained region model can replace the internals later without changing this API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image

# Normalized region boxes (x0, y0, x1, y1). The port/building name is TITLE (top-left);
# LOWER_LEFT is the sea steering wheel (empty in port).
REGIONS = {
    "title":       (0.00, 0.02, 0.40, 0.12),
    "top_hud":     (0.55, 0.00, 1.00, 0.08),   # currency / clock (ignored for change)
    "chrome_tr":   (0.90, 0.00, 1.00, 0.09),   # ☰ / ⌂ top-right
    "chrome_tl":   (0.00, 0.00, 0.10, 0.09),   # ← back arrow top-left
    "left_menu":   (0.00, 0.14, 0.16, 0.85),
    "right_panel": (0.80, 0.10, 1.00, 0.95),   # list / cart / destinations / mini-map
    "bottom":      (0.14, 0.87, 0.96, 1.00),   # button row
    "lower_left":  (0.00, 0.74, 0.20, 1.00),   # steering wheel (sea) / empty (port)
    "centre":      (0.24, 0.13, 0.74, 0.86),   # main content / blocking popup
}
# min element count to call a region "populated" (title/chrome are small single clusters)
_MIN_ELEMENTS = {"title": 1, "chrome_tr": 1, "chrome_tl": 1}
_DEFAULT_MIN = 2
# NOTE (calibration, 2026-08-18): `populated` (element-count) is NOT yet a clean state
# signal — OmniParser scatters elements into most regions, so nearly everything reads
# populated. Needs area-coverage / density thresholds per region (design doc §9). The
# VALIDATED capability today is title_text() — the rotation-robust port/screen-name read.
# LOWER_LEFT steering (sea) vs empty (port) is NOT separable by grayscale std alone
# (port 19 / sea 28 / market 86 — sea≈port, market false-triggers); it needs a dedicated
# wheel detector (circle/template) or just the existing family CNN for sea-vs-port.


@dataclass
class RegionState:
    populated: bool
    n: int                          # element count in the region
    bbox: Optional[tuple] = None    # tight bbox of the cluster (member elements)
    dominant: str = ""              # label of the largest-area element (title read target)
    text: list = field(default_factory=list)
    signature: str = ""             # cheap content hash for change-detection


def _lbl(e) -> str:
    return (getattr(e, "content", "") or getattr(e, "label", "") or "").strip()


def _centre_in(e, box, w, h) -> bool:
    x0, y0, x1, y1 = box
    return x0 * w <= getattr(e, "cx", -1) <= x1 * w and y0 * h <= getattr(e, "cy", -1) <= y1 * h


def _grayscale_std(frame: Image.Image, box) -> float:
    w, h = frame.size
    x0, y0, x1, y1 = box
    crop = frame.convert("L").crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    a = np.asarray(crop, dtype=np.float32)
    return float(a.std()) if a.size else 0.0


def perceive_regions(frame: Image.Image, elements=None) -> dict:
    """Return {region_name: RegionState} for the current frame."""
    if elements is None:
        try:
            from vision.omniparser import parse_fast_cached
            elements = parse_fast_cached(frame)
        except Exception:
            elements = []
    w, h = frame.size
    out: dict = {}
    for name, box in REGIONS.items():
        els = [e for e in elements if _centre_in(e, box, w, h)]
        labels = [t for t in (_lbl(e) for e in els) if t]
        n = len(els)
        bbox = None
        dominant = ""
        if els:
            bbox = (min(e.x1 for e in els), min(e.y1 for e in els),
                    max(e.x2 for e in els), max(e.y2 for e in els))
            big = max(els, key=lambda e: getattr(e, "width", 0) * getattr(e, "height", 0))
            dominant = _lbl(big)
        populated = n >= _MIN_ELEMENTS.get(name, _DEFAULT_MIN)
        sig = f"{n}:" + "|".join(sorted(labels)[:5])
        out[name] = RegionState(populated=populated, n=n, bbox=bbox,
                                dominant=dominant, text=labels, signature=sig)

    # LOWER_LEFT steering wheel (sea) vs empty (port): OmniParser won't detect the wheel,
    # and grayscale std alone doesn't separate it cleanly — expose the std in the
    # signature for calibration, but don't decide populated on it (needs a real wheel
    # detector or the family CNN). See the calibration note above.
    out["lower_left"].signature += f"|std={_grayscale_std(frame, REGIONS['lower_left']):.0f}"
    return out


def title_text(frame: Image.Image, elements=None) -> Optional[str]:
    """The TITLE cluster's dominant text (port/building/screen name candidate), or None.
    Rotation-robust replacement for the absolute-crop port-name read."""
    t = perceive_regions(frame, elements).get("title")
    return (t.dominant or (t.text[0] if t.text else None)) if (t and t.populated) else None
