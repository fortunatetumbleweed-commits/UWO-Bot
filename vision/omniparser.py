# vision/omniparser.py
# OmniParser v2 integration — local UI element detection.
#
# OmniParser (Microsoft, 2024) uses two components:
#   1. icon_detect/model.pt  — YOLOv8 model, detects bounding boxes of all
#                              interactive icons on screen
#   2. icon_caption/         — Florence-2 fine-tune, generates a text
#                              description of each detected icon patch
#
# Text labels are found separately via EasyOCR (already in project).
#
# Two operating modes:
#   parse_fast(frame)  — YOLO + EasyOCR only, no Florence-2
#                        ~0.3-0.5s, minimal memory, safe for per-frame use.
#                        Icons without text get label="icon"; icons overlapping
#                        an EasyOCR text region are promoted to element_type="button"
#                        with the text as the label. Preferred mode.
#
#   parse(frame)       — YOLO + Florence-2 + EasyOCR (full pipeline)
#                        ~2-5s, ~12GB MPS. Only use for offline flow analysis;
#                        causes OOM on Mac MPS when called per-frame.
#
# Role in the vision pipeline (L1.5):
#   Runs on every frame where element positions are needed.
#   Answers: "what interactive elements are present and exactly where?"
#   Never reasons about game context — that is Claude's job (L4).
#
# Model download (one-time, ~1 GB):
#   python -m vision.omniparser --download
#
# Test on current screen:
#   python -m vision.omniparser

from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger
from PIL import Image

_HF_REPO = "microsoft/OmniParser-v2.0"
_WEIGHTS_DIR = Path("vision/models/omniparser")
_DETECT_WEIGHTS = _WEIGHTS_DIR / "icon_detect" / "model.pt"
_CAPTION_DIR = _WEIGHTS_DIR / "icon_caption"


@dataclass
class DetectedElement:
    """A single UI element detected by OmniParser."""
    label: str           # text label (OCR) or icon description (caption model)
    element_type: str    # "text" | "icon" | "button"
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    confidence: float = 0.0

    @property
    def cx(self) -> int:
        return (self.x1 + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y1 + self.y2) // 2

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def to_dict(self) -> dict:
        return asdict(self)

    def __repr__(self) -> str:
        return (
            f"DetectedElement({self.label!r} [{self.element_type}] "
            f"@({self.cx},{self.cy}) {self.width}×{self.height})"
        )


def download_weights() -> bool:
    """
    Download OmniParser v2 weights from HuggingFace into vision/models/omniparser/.
    Safe to call multiple times — skips files that already exist.
    Returns True on success.
    """
    try:
        from huggingface_hub import hf_hub_download
        _WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
        (_WEIGHTS_DIR / "icon_detect").mkdir(exist_ok=True)
        (_WEIGHTS_DIR / "icon_caption").mkdir(exist_ok=True)

        files_to_download = [
            # YOLO detection model
            ("icon_detect/model.pt",               _DETECT_WEIGHTS),
            # Florence-2 caption model
            ("icon_caption/config.json",            _CAPTION_DIR / "config.json"),
            ("icon_caption/generation_config.json", _CAPTION_DIR / "generation_config.json"),
            ("icon_caption/model.safetensors",      _CAPTION_DIR / "model.safetensors"),
        ]

        for repo_path, local_path in files_to_download:
            if local_path.exists() and local_path.stat().st_size > 0:
                logger.debug(f"Already downloaded: {local_path}")
                continue
            logger.info(f"Downloading {repo_path} …")
            hf_hub_download(
                repo_id=_HF_REPO,
                filename=repo_path,
                local_dir=str(_WEIGHTS_DIR),
            )
            logger.info(f"  → {local_path}")

        logger.info("OmniParser weights ready.")
        return True

    except Exception as exc:
        logger.error(f"OmniParser download failed: {exc}")
        return False


class OmniParser:
    """
    OmniParser v2 — detects all interactive UI elements in a screenshot.

    Sub-models are loaded lazily and independently:
      - YOLO (ultralytics): fast icon bounding-box detector (~300 MB, safe for per-frame).
      - Florence-2 fine-tune: icon captioner (~12 GB MPS, only for offline batch analysis).

    Preferred usage:  parser.parse_fast(frame)
    Full pipeline:    parser.parse(frame)       [only when Florence-2 is acceptable]
    """

    def __init__(self) -> None:
        self._yolo = None
        self._caption_processor = None
        self._caption_model = None
        self._florence_ready = False
        self._device: str = "cpu"

    # ── Model loading ──────────────────────────────────────────────────────────

    def _load_yolo(self) -> bool:
        """Load just the YOLO icon detector. Fast, low memory."""
        if self._yolo is not None:
            return True

        if not _DETECT_WEIGHTS.exists():
            logger.warning(
                "OmniParser YOLO weights not found. "
                "Run:  python -m vision.omniparser --download"
            )
            return False

        try:
            import torch
            from ultralytics import YOLO

            if torch.backends.mps.is_available():
                self._device = "mps"
            elif torch.cuda.is_available():
                self._device = "cuda"
            else:
                self._device = "cpu"

            logger.info(f"Loading OmniParser YOLO on {self._device}…")
            t0 = time.monotonic()
            self._yolo = YOLO(str(_DETECT_WEIGHTS))
            logger.info(f"OmniParser YOLO ready in {time.monotonic() - t0:.1f}s")
            return True

        except ImportError as exc:
            logger.warning(f"OmniParser YOLO dependency missing: {exc}\nRun: pip install ultralytics")
            return False
        except Exception as exc:
            logger.warning(f"OmniParser YOLO failed to load: {exc}")
            return False

    def _load_florence(self) -> bool:
        """
        Load Florence-2 caption model. Heavy (~12GB MPS).
        Only call this for offline batch analysis — will OOM on per-frame use.
        """
        if self._florence_ready:
            return True

        if not _DETECT_WEIGHTS.exists():
            return False

        try:
            import torch
            from transformers import AutoProcessor, AutoModelForCausalLM

            _FLORENCE_BASE = "microsoft/Florence-2-base-ft"
            logger.info("Loading Florence-2 caption model (this uses ~12GB MPS)…")
            t0 = time.monotonic()

            self._caption_processor = AutoProcessor.from_pretrained(
                _FLORENCE_BASE, trust_remote_code=True,
            )
            self._caption_model = AutoModelForCausalLM.from_pretrained(
                str(_CAPTION_DIR), trust_remote_code=True,
            ).to(self._device)
            self._caption_model.eval()

            logger.info(f"Florence-2 ready in {time.monotonic() - t0:.1f}s")
            self._florence_ready = True
            return True

        except ImportError as exc:
            logger.warning(f"Florence-2 dependency missing: {exc}")
            return False
        except Exception as exc:
            logger.warning(f"Florence-2 failed to load: {exc}")
            return False

    def yolo_available(self) -> bool:
        """True if YOLO icon detector can be loaded."""
        return self._load_yolo()

    def available(self) -> bool:
        """True if full pipeline (YOLO + Florence-2) can be loaded."""
        return self._load_yolo() and self._load_florence()

    # ── Public parse methods ───────────────────────────────────────────────────

    def parse_fast(self, frame: Image.Image) -> List[DetectedElement]:
        """
        Fast mode: YOLO icon detection + EasyOCR text detection.
        No Florence-2 — icon patches are NOT captioned.

        Icons without overlapping text keep label="icon".
        Icons whose bounding box contains an EasyOCR text region are promoted
        to element_type="button" with the text as their label.

        ~0.3–0.5s per frame. Safe for per-frame use in the bot main loop.
        """
        if not self._load_yolo():
            return []

        try:
            icons = self._detect_icons_yolo_only(frame)
            texts = self._detect_text(frame)
            elements = _merge_icons_and_text(icons, texts, frame_dims=(frame.width, frame.height))
            elements.sort(key=lambda e: (e.y1, e.x1))
            return elements
        except Exception as exc:
            logger.warning(f"OmniParser.parse_fast() error: {exc}")
            return []

    def parse(self, frame: Image.Image, include_text: bool = True) -> List[DetectedElement]:
        """
        Full pipeline: YOLO + Florence-2 captioning + EasyOCR.
        Only use for offline analysis (flow capture, scene inventory).
        ~2–5s per frame; Florence-2 causes OOM on Mac MPS under sustained load.
        """
        if not self._load_yolo():
            return []

        try:
            icons = self._detect_icons_with_caption(frame)
            if include_text:
                texts = self._detect_text(frame)
                elements = _merge_icons_and_text(icons, texts, frame_dims=(frame.width, frame.height))
            else:
                elements = icons
            elements.sort(key=lambda e: (e.y1, e.x1))
            return elements
        except Exception as exc:
            logger.warning(f"OmniParser.parse() error: {exc}")
            return []

    # ── Icon detection (YOLO only, no captions) ────────────────────────────────

    def _detect_icons_yolo_only(self, frame: Image.Image) -> List[DetectedElement]:
        """Run YOLO, return bounding boxes with label='icon'. No Florence-2."""
        results = self._yolo(frame, verbose=False)
        elements = []
        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                conf = float(box.conf[0])
                if conf >= 0.3:
                    elements.append(DetectedElement(
                        label="icon",
                        element_type="icon",
                        x1=x1, y1=y1, x2=x2, y2=y2,
                        confidence=conf,
                    ))
        return elements

    # ── Icon detection (YOLO + Florence-2 captions) ────────────────────────────

    def _detect_icons_with_caption(self, frame: Image.Image) -> List[DetectedElement]:
        """
        Run YOLO to detect icon bounding boxes, then caption all patches in
        a single batched Florence-2 inference pass.
        """
        results = self._yolo(frame, verbose=False)

        boxes_conf: List[tuple] = []
        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                conf = float(box.conf[0])
                if conf >= 0.3:
                    boxes_conf.append((x1, y1, x2, y2, conf))

        if not boxes_conf:
            return []

        # Crop all patches and caption them in one batched call
        patches = []
        for x1, y1, x2, y2, _ in boxes_conf:
            patch = frame.crop((x1, y1, x2, y2))
            if patch.width < 32 or patch.height < 32:
                patch = patch.resize((64, 64), Image.LANCZOS)
            patches.append(patch)

        if not self._load_florence():
            # Florence unavailable — return YOLO boxes unlabelled
            return [
                DetectedElement(
                    label="icon",
                    element_type="icon",
                    x1=x1, y1=y1, x2=x2, y2=y2,
                    confidence=conf,
                )
                for (x1, y1, x2, y2, conf) in boxes_conf
            ]

        labels = self._caption_batch(patches)

        return [
            DetectedElement(
                label=label or "icon",
                element_type="icon",
                x1=x1, y1=y1, x2=x2, y2=y2,
                confidence=conf,
            )
            for (x1, y1, x2, y2, conf), label in zip(boxes_conf, labels)
        ]

    def _caption_batch(self, patches: List[Image.Image]) -> List[str]:
        """Caption a list of icon patches in a single batched Florence-2 call."""
        import torch

        if not patches or not self._florence_ready:
            return []

        task = "<CAPTION>"
        inputs = self._caption_processor(
            text=[task] * len(patches),
            images=patches,
            return_tensors="pt",
            padding=True,
        ).to(self._device)

        with torch.no_grad():
            ids = self._caption_model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=20,
                num_beams=1,
            )

        results = self._caption_processor.batch_decode(ids, skip_special_tokens=True)
        return [r.replace(task, "").strip() for r in results]

    # ── Text detection via EasyOCR ─────────────────────────────────────────────

    def _detect_text(self, frame: Image.Image) -> List[DetectedElement]:
        """OCR the frame for text labels using EasyOCR."""
        try:
            import numpy as np
            from vision.ocr import _get_reader
            raw = _get_reader().readtext(np.array(frame), detail=1)
            elements = []
            for bbox, text, conf in raw:
                if conf < 0.4 or len(text.strip()) < 2:
                    continue
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                elements.append(DetectedElement(
                    label=text.strip(),
                    element_type="text",
                    x1=int(min(xs)), y1=int(min(ys)),
                    x2=int(max(xs)), y2=int(max(ys)),
                    confidence=conf,
                ))
            return elements
        except Exception as exc:
            logger.debug(f"Text OCR failed: {exc}")
            return []


# ── Element merging ────────────────────────────────────────────────────────────

# Containment threshold for promoting an icon to a button when an
# EasyOCR text region overlaps it.  0.7 means the text bbox must be
# almost-fully-inside the icon (small clipping at edges OK).  The
# previous threshold of 0.3 was too permissive — peripheral text was
# being sucked into large background panels.
_MERGE_CONTAINMENT_MIN = 0.7

# Panel-size cap as a fraction of the frame area.  When the frame dims
# are known, icons larger than this are treated as panels and excluded
# as merge targets so their interior text stays standalone.  Real
# buttons in UWO sit well below this (the widest building-list row is
# ~600×140 ≈ 3% of a 2400×1080 frame); 15% is a generous ceiling that
# still rejects the ship-list panel (~24%) and similar panel-sized
# YOLO bboxes.
_MERGE_PANEL_AREA_FRAC = 0.15


def _merge_icons_and_text(
    icons: List[DetectedElement],
    texts: List[DetectedElement],
    frame_dims: Optional[Tuple[int, int]] = None,
) -> List[DetectedElement]:
    """
    Combine YOLO icon detections with EasyOCR text detections.

    Rules:
    - If a text region is ≥ _MERGE_CONTAINMENT_MIN inside an icon bbox
      AND the icon is button-sized (< _MERGE_PANEL_AREA_FRAC of the
      frame), promote the icon to element_type="button" with the text
      as its label.
    - Icons over the panel cap (ship list, NPC overlay, market grid)
      are SKIPPED as merge targets.  Their interior text stays
      standalone — preventing the soup-string label problem where
      every text overlapping a big panel got concatenated together.
    - When an icon already has a real text label (i.e. was already
      promoted on a prior text in this pass), any additional text
      inside it stays standalone — no more `label + " " + text`
      append.  Two texts inside one icon means YOLO drew a
      panel-shaped bbox we should treat as such.
    - Text regions not promoted → kept as element_type="text".
    - Icons not matched to any text → kept as element_type="icon".

    *frame_dims*: (width, height) used to evaluate the panel-area cap.
    When None, the cap defaults to permissive (skip cap) and only the
    containment threshold applies — back-compat for callers that
    haven't been updated.
    """
    merged_icons = list(icons)
    unmatched_texts = []

    if frame_dims is not None:
        frame_w, frame_h = frame_dims
        panel_area_cap = _MERGE_PANEL_AREA_FRAC * frame_w * frame_h
    else:
        panel_area_cap = None

    def _is_button_sized(el: DetectedElement) -> bool:
        if panel_area_cap is None:
            return True
        return (el.width * el.height) <= panel_area_cap

    for text_el in texts:
        best_icon: Optional[DetectedElement] = None
        best_score = 0.0

        for icon_el in merged_icons:
            # Skip panel-sized icons — their interior text stays standalone
            if not _is_button_sized(icon_el):
                continue
            score = _containment_score(text_el, icon_el)
            if score > best_score:
                best_score = score
                best_icon = icon_el

        if (
            best_icon is not None
            and best_score >= _MERGE_CONTAINMENT_MIN
            and best_icon.element_type == "icon"
        ):
            # Promote: icon → button with this text as label
            best_icon.label = text_el.label
            best_icon.element_type = "button"
        else:
            # Either no qualifying icon, or icon already has a label
            # from a prior text — keep this text standalone.
            unmatched_texts.append(text_el)

    return merged_icons + unmatched_texts


def _containment_score(inner: DetectedElement, outer: DetectedElement) -> float:
    """
    How much of `inner` is contained within `outer`.
    Returns 0.0–1.0:  1.0 = inner is fully inside outer.
    """
    ix1 = max(inner.x1, outer.x1)
    iy1 = max(inner.y1, outer.y1)
    ix2 = min(inner.x2, outer.x2)
    iy2 = min(inner.y2, outer.y2)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    inter = (ix2 - ix1) * (iy2 - iy1)
    inner_area = inner.width * inner.height
    return inter / inner_area if inner_area > 0 else 0.0


def _iou(a: DetectedElement, b: DetectedElement) -> float:
    ix1, iy1 = max(a.x1, b.x1), max(a.y1, b.y1)
    ix2, iy2 = min(a.x2, b.x2), min(a.y2, b.y2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = (a.width * a.height) + (b.width * b.height) - inter
    return inter / union if union > 0 else 0.0


# ── Singleton ──────────────────────────────────────────────────────────────────

_instance: Optional[OmniParser] = None


def get_omniparser() -> OmniParser:
    global _instance
    if _instance is None:
        _instance = OmniParser()
    return _instance


# ── Per-frame parse_fast cache ────────────────────────────────────────────────
#
# Phase 5c Layer 4: parse_fast is the dominant per-frame vision cost
# (~0.3-0.5s) and several callers within a single perceive() tick need
# the same element list (chrome detection, _find_button, cue catalog).
# Cache by id(frame) so repeated calls within a tick re-use the result.
#
# The cache is bounded at MAX_CACHE_ENTRIES; on overflow it is cleared
# wholesale (simpler than LRU eviction and good enough for the access
# pattern: 1-3 frames live at once).

_FRAME_CACHE: dict[int, List[DetectedElement]] = {}
MAX_CACHE_ENTRIES: int = 4


def parse_fast_cached(frame: Image.Image) -> List[DetectedElement]:
    """parse_fast() with per-frame caching by id(frame).

    Within a single perceive() tick the same PIL frame is passed to many
    consumers (chrome detection, _find_button, etc).  Without caching,
    parse_fast runs ~0.3-0.5s per call and dominates the tick budget.
    With caching, only the first caller pays the cost.

    Cross-tick safety: the cache holds a REFERENCE to the frame alongside its elements.
    That is load-bearing, not incidental — `id()` is only unique while the object is
    alive, and CPython hands the same address to the next allocation once a frame is
    freed. Measured: 200 sequentially-created 2400x1080 PIL images occupied just THREE
    distinct ids, i.e. 197 collisions. Without the reference, a frame silently inherits a
    previous, unrelated frame's elements — which is how a world-map frame came back
    carrying the port overworld's buildings (live 2026-08-21), and any consumer keyed on
    those elements then acts on a screen that is not in front of it.
    """
    fid = id(frame)
    cached = _FRAME_CACHE.get(fid)
    if cached is not None:
        cached_frame, elements = cached
        if cached_frame is frame:
            return elements
        _FRAME_CACHE.pop(fid, None)          # stale id — the old frame is gone

    if len(_FRAME_CACHE) >= MAX_CACHE_ENTRIES:
        _FRAME_CACHE.clear()

    elements = get_omniparser().parse_fast(frame)
    _FRAME_CACHE[fid] = (frame, elements)    # the reference pins the id
    return elements


def clear_parse_fast_cache() -> None:
    """Clear the per-frame parse_fast cache.  Useful in tests or after a
    long-running pause where stale frame ids might be reused."""
    _FRAME_CACHE.clear()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from memory.logger import setup_logging
    setup_logging()

    if "--download" in sys.argv:
        logger.info("Downloading OmniParser v2 weights…")
        ok = download_weights()
        sys.exit(0 if ok else 1)

    mode = "fast"
    if "--full" in sys.argv:
        mode = "full"

    logger.info(f"Capturing screen and running OmniParser ({mode} mode)…")
    from capture.adb_capture import capture_screen
    frame = capture_screen()
    parser = get_omniparser()

    if not parser.yolo_available():
        logger.error(
            "OmniParser YOLO not available. "
            "Run:  python -m vision.omniparser --download"
        )
        sys.exit(1)

    t0 = time.monotonic()
    if mode == "full":
        elements = parser.parse(frame)
    else:
        elements = parser.parse_fast(frame)
    elapsed = time.monotonic() - t0

    logger.info(f"Detected {len(elements)} elements in {elapsed:.2f}s ({mode} mode):")
    for el in elements:
        logger.info(f"  {el}")

    # Debug visualisation
    from PIL import ImageDraw
    debug = frame.copy()
    draw = ImageDraw.Draw(debug)
    colours = {"icon": "red", "text": "cyan", "button": "yellow"}
    for el in elements:
        c = colours.get(el.element_type, "white")
        draw.rectangle([el.x1, el.y1, el.x2, el.y2], outline=c, width=2)
        draw.text((el.x1, max(0, el.y1 - 14)), el.label[:24], fill=c)
    out = Path("debug_omniparser.png")
    debug.save(out)
    logger.info(f"Debug image saved: {out}")
