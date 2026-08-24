"""Deterministic action-outcome classifier via a coarse frame-area diff.

AI perception (OmniParser/Moondream/CNN) is non-deterministic — the same screen can
yield different results — so "did my action work?" must NOT be answered by re-diffing
noisy model labels. Instead compare whether an action caused a TANGIBLE LAYOUT CHANGE in
a screen region: a clock tick / weather / NPC bubble = tiny, no layout change; a real
game action always changes a region (enter/exit building, dialog appear/dismiss, right
panel show/hide, a button like Go-to-Port appearing).

This is both the action-VERIFICATION gate (did it work?) and a LEARNING signal (what did
it do?). Cheap and ML-free — runs on downscaled frames, ahead of any perceive/recovery.
See docs/action_verification_and_recovery_design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

# Downscale target — coarse is the point (we want layout-scale change, not pixels).
_DW, _DH = 240, 108

# Region boxes as (x0, y0, x1, y1) fractions. The TOP-HUD strip (currency + clock) is
# excluded so a ticking clock never reads as a change.
_TOP_HUD = (0.00, 0.00, 1.00, 0.09)
_CENTRE  = (0.22, 0.12, 0.72, 0.86)   # main content / modal zone
_RIGHT   = (0.78, 0.10, 1.00, 0.95)   # building list / cart / destination panel
_BOTTOM  = (0.15, 0.88, 0.95, 1.00)   # button row (Depart / Purchase / Go-to-Port …)

# Mean abs-delta thresholds on the 0–255 downscaled diff.
_TINY = 4.0     # below this a region is unchanged
_SIG  = 14.0    # above this a region changed for real


@dataclass
class ActionOutcome:
    kind: str                       # unchanged | central_dialog | panel_or_button | state_change
    deltas: dict = field(default_factory=dict)   # region → mean abs delta

    def __bool__(self):             # truthy iff SOMETHING changed
        return self.kind != "unchanged"


def _region_mean(diff: np.ndarray, box) -> float:
    h, w = diff.shape[:2]
    x0, y0, x1, y1 = box
    sub = diff[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
    return float(sub.mean()) if sub.size else 0.0


def _outer_mean(diff: np.ndarray) -> float:
    """Mean delta OUTSIDE the centre and the ignored top-HUD — i.e. the left menu, the
    right panel and the bottom row. Distinguishes a centred dialog (outer quiet) from a
    whole-screen state change (outer loud)."""
    h, w = diff.shape[:2]
    mask = np.ones((h, w), bool)
    for box in (_TOP_HUD, _CENTRE):
        x0, y0, x1, y1 = box
        mask[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = False
    return float(diff[mask].mean()) if mask.any() else 0.0


def classify_action_outcome(prev: Image.Image, cur: Image.Image) -> ActionOutcome:
    """Classify the tangible change between the pre-action and post-action frames."""
    a = np.asarray(prev.convert("RGB").resize((_DW, _DH)), dtype=np.int16)
    b = np.asarray(cur.convert("RGB").resize((_DW, _DH)), dtype=np.int16)
    diff = np.abs(a - b).mean(axis=2)          # per-pixel mean channel delta, 0–255

    d = {
        "centre": _region_mean(diff, _CENTRE),
        "outer":  _outer_mean(diff),
        "right":  _region_mean(diff, _RIGHT),
        "bottom": _region_mean(diff, _BOTTOM),
    }
    centre, outer, right, bottom = d["centre"], d["outer"], d["right"], d["bottom"]

    if centre < _TINY and outer < _TINY:
        kind = "unchanged"                                      # tap didn't register
    elif centre > _SIG and outer < _SIG:
        kind = "central_dialog"                                 # overlay over the middle
    elif centre > _SIG and outer > _SIG:
        kind = "state_change"                                   # whole screen moved
    elif (right > _SIG or bottom > _SIG) and centre < _SIG:
        kind = "panel_or_button"                                # side panel / button reacted
    else:
        kind = "state_change"                                   # ambiguous large change
    return ActionOutcome(kind=kind, deltas={k: round(v, 1) for k, v in d.items()})
