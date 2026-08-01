"""Layer 2 — water/land segmentation.

Per-tick boolean mask: True = water, False = land/UI/sprite.
Pluggable: today's default is the V11 brightness-threshold mask
(documented offender — fails on UI overlays and dark coastal
water); tomorrow's is a fine-tuned MobileSAM / YOLOv8-seg trained
on labeled minimap crops; later, full-screen VLM segmentation.

Every implementation MUST:
  - return an `np.bool_` array, same H×W as `frame.minimap()`
  - tolerate frames where ship sprite, UI overlays, or text labels
    sit on top of water — do not classify them as land
  - never block longer than its declared latency budget
"""
from __future__ import annotations

from typing import Optional, Protocol

import numpy as np

from brain.ai_nav.vision_input import VisionFrame


class SegmentationLayer(Protocol):
    """Protocol for L2 implementations."""
    name: str
    latency_budget_ms: float

    def segment(self, frame: VisionFrame) -> np.ndarray:
        """Return water mask: True = water, False = land/other.
        Shape == frame.minimap().size flipped to (H, W)."""
        ...


# ── Default impl: legacy V11 brightness mask ───────────────────────────


class V11Segmenter:
    """Wraps `channel_mask_from_rgb` (V11 brightness threshold +
    ship-CC restriction).

    Known failure modes:
      - bright UI overlays (port markers, village icons, text labels,
        sonar rings) are classified as land — see
        `memory/project_translucent_overlay_corrupts_perception.md`
      - dark coastal water near banks fails the threshold and reads
        as land, shrinking the water region by ~10 px
      - sprite pixels (ship hull, sonar fan) punch holes in the mask
        — `memory/project_ship_sonar_punch_holes_in_water_mask.md`

    These are the bugs the learned segmenter is meant to fix.  Kept
    here so the pipeline runs end-to-end today.
    """
    name = "v11_brightness"
    latency_budget_ms = 50.0

    def segment(self, frame: VisionFrame) -> np.ndarray:
        from tools.centerline_extraction_prototype import channel_mask_from_rgb
        rgb = np.asarray(frame.minimap())
        _trimmed, mask = channel_mask_from_rgb(rgb)
        return mask.astype(np.bool_)


# ── Stub: learned segmenter (MobileSAM / YOLOv8-seg) ───────────────────


class LearnedSegmenter:
    """Fine-tuned MobileSAM or YOLOv8-seg, trained on 500–2000
    hand-labeled minimap crops.

    See `docs/ai_navigation_landscape.md` §5.2 for the training
    recipe and §3.8 for the model family.

    Not implemented yet.
    """
    name = "learned_segmenter"
    latency_budget_ms = 50.0

    def __init__(self, weights_path):
        raise NotImplementedError(
            "LearnedSegmenter not implemented yet — see docs/ai_navigation_landscape.md §8.2"
        )

    def segment(self, frame):
        raise NotImplementedError


# ── Stub: VLM full-screen segmenter (later) ────────────────────────────


class VLMSegmenter:
    """Florence-2 or similar with REGION_TO_SEGMENTATION task,
    reading the full screen.  This is the swap target when the
    user wants navigation to read the rendered 3D view, not the
    minimap.

    Calls `frame.full_screen()`.

    Not implemented yet.
    """
    name = "vlm_full_screen"
    latency_budget_ms = 500.0

    def segment(self, frame):
        raise NotImplementedError(
            "VLMSegmenter not implemented yet — see docs/ai_navigation_landscape.md §3.9"
        )
