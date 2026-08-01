# vision/template_matcher.py
# OpenCV template matching for detecting UI elements in a captured frame.
# Templates live in vision/assets/ as PNG files.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from config.settings import TEMPLATE_MATCH_THRESHOLD, ASSETS_DIR


@dataclass
class MatchResult:
    found: bool
    x: int          # centre x of matched region
    y: int          # centre y of matched region
    confidence: float


def _pil_to_bgr(image: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


def find_template(
    frame: Image.Image,
    template_path: str,
    threshold: float = TEMPLATE_MATCH_THRESHOLD,
) -> MatchResult:
    """
    Search for *template_path* inside *frame*.

    Returns a MatchResult with found=True and the centre coordinates when the
    best match exceeds *threshold*; otherwise found=False.
    """
    raise NotImplementedError


def find_all_templates(
    frame: Image.Image,
    template_path: str,
    threshold: float = TEMPLATE_MATCH_THRESHOLD,
) -> list[MatchResult]:
    """Return every non-overlapping match above the threshold."""
    raise NotImplementedError
