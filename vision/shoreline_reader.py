"""Sea-view shoreline + beam inference (ship-relative v2).

Reads a full sea frame and returns four independent binary signals
that drive manual steering:

  land_ahead     — bow path blocked by land (must turn)
  land_port      — land off the port (left) side of the ship
  land_starboard — land off the starboard (right) side
  beam_present   — discovery beam (B1..B6) visible

Plus per-tag probabilities for inspection.

Lazy load + per-frame cache; same pattern as vision/family_classifier
and vision/minimap_reader.  Returns an empty/zero verdict when the
model isn't trained yet so callers can be wired up in advance.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger


_MODEL_PATH = Path("data/models/shoreline_classifier.pt")
_DEFAULT_THRESHOLD = 0.5


@dataclass(frozen=True)
class ShorelineVerdict:
    land_ahead:     bool
    land_port:      bool
    land_starboard: bool
    beam_present:   bool
    probs: dict[str, float] = field(default_factory=dict)


# ── Lazy state ─────────────────────────────────────────────────────────────

_model = None
_tag_list: Optional[list[str]] = None
_input_size: Optional[int] = None
_device: Optional[str] = None
_transform = None
_load_failed = False


def _ensure_loaded() -> bool:
    global _model, _tag_list, _input_size, _device, _transform, _load_failed
    if _load_failed:
        return False
    if _model is not None:
        return True
    if not _MODEL_PATH.exists():
        logger.debug(
            f"[shoreline_reader] model not found at {_MODEL_PATH} — "
            "run tools/train_shoreline_classifier.py first.  Returning "
            "all-false verdicts for all calls."
        )
        _load_failed = True
        return False
    try:
        import torch
        from torch import nn
        from torchvision import transforms
        from torchvision.models import mobilenet_v3_small

        ckpt = torch.load(_MODEL_PATH, map_location="cpu", weights_only=False)
        _tag_list = list(ckpt["tags"])
        _input_size = int(ckpt["input_size"])
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _device = device

        m = mobilenet_v3_small(weights=None)
        m.classifier[-1] = nn.Linear(
            m.classifier[-1].in_features, len(_tag_list),
        )
        m.load_state_dict(ckpt["state_dict"])
        m.eval()
        m.to(device)
        _model = m

        _transform = transforms.Compose([
            transforms.Resize((_input_size, _input_size)),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225],
            ),
        ])
        logger.info(
            f"[shoreline_reader] loaded ({m.__class__.__name__}, "
            f"{len(_tag_list)} tags, device={device}, "
            f"input={_input_size}×{_input_size})"
        )
        return True
    except Exception as exc:
        logger.warning(f"[shoreline_reader] load failed: {exc}")
        _load_failed = True
        return False


_RESULT_CACHE: "OrderedDict[int, ShorelineVerdict]" = OrderedDict()
_RESULT_CACHE_MAX = 8


def _reset_for_test() -> None:
    global _model, _tag_list, _input_size, _device, _transform, _load_failed
    _model = None
    _tag_list = None
    _input_size = None
    _device = None
    _transform = None
    _load_failed = False
    _RESULT_CACHE.clear()


# ── Inference ──────────────────────────────────────────────────────────────

_EMPTY = ShorelineVerdict(
    land_ahead=False, land_port=False, land_starboard=False,
    beam_present=False, probs={},
)


def read_shoreline(frame, threshold: float = _DEFAULT_THRESHOLD) -> ShorelineVerdict:
    """Run the shoreline + beam model on a sea frame.

    `frame` is a PIL.Image (full 2400×1080 sea frame).  Returns a
    ShorelineVerdict.  Returns the all-false verdict when the model
    isn't trained yet.
    """
    if not _ensure_loaded():
        return _EMPTY
    key = (id(frame), threshold)
    cached = _RESULT_CACHE.get(key)
    if cached is not None:
        _RESULT_CACHE.move_to_end(key)
        return cached
    verdict = _run_inference(frame, threshold)
    _RESULT_CACHE[key] = verdict
    if len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
        _RESULT_CACHE.popitem(last=False)
    return verdict


def _run_inference(frame, threshold: float) -> ShorelineVerdict:
    import torch

    if _model is None or _transform is None or _tag_list is None:
        return _EMPTY
    img = frame.convert("RGB")
    tensor = _transform(img).unsqueeze(0).to(_device)
    with torch.no_grad():
        logits = _model(tensor)
        probs = torch.sigmoid(logits)[0].cpu().tolist()
    probs_dict = {tag: float(p) for tag, p in zip(_tag_list, probs)}
    def _get(tag: str) -> bool:
        return probs_dict.get(tag, 0.0) >= threshold
    return ShorelineVerdict(
        land_ahead=_get("shore:land_ahead"),
        land_port=_get("shore:land_port"),
        land_starboard=_get("shore:land_starboard"),
        beam_present=_get("beam_present"),
        probs=probs_dict,
    )
