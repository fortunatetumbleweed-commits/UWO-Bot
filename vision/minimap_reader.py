"""Mini-map multi-label inference.

Crops the top-right radar from a sea frame and returns which sprites
are visible.  Wraps the model trained by `tools/train_minimap_detector.py`.

Usage:
    from vision.minimap_reader import read_minimap

    verdict = read_minimap(pil_frame)
    if "minimap:port_anchor" in verdict.tags:
        # port is on the radar — head toward its quadrant

Lazy-loads the model once per process; per-frame cache keyed by
id(frame) so multiple callers in one perceive tick share the forward
pass.  Returns a verdict with empty tags + zero probabilities when the
model isn't trained yet, so callers can be wired up before training
completes.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger


_MODEL_PATH = Path("data/models/minimap_detector.pt")
_DEFAULT_THRESHOLD = 0.5


@dataclass(frozen=True)
class MinimapVerdict:
    tags:  frozenset[str]            # tags above threshold
    probs: dict[str, float] = field(default_factory=dict)


# ── Lazy state ─────────────────────────────────────────────────────────────

_model = None
_tag_list: Optional[list[str]] = None
_input_size: Optional[int] = None
_minimap_crop: Optional[tuple[int, int, int, int]] = None
_device: Optional[str] = None
_transform = None
_load_failed = False


def _ensure_loaded() -> bool:
    global _model, _tag_list, _input_size, _minimap_crop
    global _device, _transform, _load_failed
    if _load_failed:
        return False
    if _model is not None:
        return True
    if not _MODEL_PATH.exists():
        logger.debug(
            f"[minimap_reader] model not found at {_MODEL_PATH} — "
            "run tools/train_minimap_detector.py first.  Returning empty "
            "verdicts for all calls."
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
        _minimap_crop = tuple(ckpt["minimap_crop"])
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
            f"[minimap_reader] loaded ({m.__class__.__name__}, "
            f"{len(_tag_list)} tags, device={device}, "
            f"input={_input_size}×{_input_size}, crop={_minimap_crop})"
        )
        return True
    except Exception as exc:
        logger.warning(f"[minimap_reader] load failed: {exc}")
        _load_failed = True
        return False


_RESULT_CACHE: "OrderedDict[int, MinimapVerdict]" = OrderedDict()
_RESULT_CACHE_MAX = 8


def _reset_for_test() -> None:
    """Test hook — drop cached model + results so the next call reloads."""
    global _model, _tag_list, _input_size, _minimap_crop
    global _device, _transform, _load_failed
    _model = None
    _tag_list = None
    _input_size = None
    _minimap_crop = None
    _device = None
    _transform = None
    _load_failed = False
    _RESULT_CACHE.clear()


# ── Inference ──────────────────────────────────────────────────────────────

def _crop_minimap(frame):
    """Crop the top-right radar.  Scale the canonical crop if the frame
    isn't 2400×1080 (rare; the project's phone is fixed at that res)."""
    if _minimap_crop is None:
        return frame
    w, h = frame.size
    cx0, cy0, cx1, cy1 = _minimap_crop
    if (w, h) != (2400, 1080):
        sx = w / 2400.0
        sy = h / 1080.0
        cx0, cy0 = int(cx0 * sx), int(cy0 * sy)
        cx1, cy1 = int(cx1 * sx), int(cy1 * sy)
    return frame.crop((cx0, cy0, cx1, cy1))


def read_minimap(frame, threshold: float = _DEFAULT_THRESHOLD) -> MinimapVerdict:
    """Run the mini-map detector on a sea frame.

    `frame` is a PIL.Image (full 2400×1080 sea frame — the crop happens
    inside).  Returns a MinimapVerdict with `tags` (set of tags above
    threshold) and `probs` (per-tag probability for inspection).

    Returns an empty verdict (no tags, empty probs) when the model
    isn't trained yet — callers can fall through to Claude or to the
    rule-based perception cascade.
    """
    if not _ensure_loaded():
        return MinimapVerdict(tags=frozenset(), probs={})
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


def _run_inference(frame, threshold: float) -> MinimapVerdict:
    import torch

    if _model is None or _transform is None or _tag_list is None:
        return MinimapVerdict(tags=frozenset(), probs={})
    crop = _crop_minimap(frame.convert("RGB"))
    tensor = _transform(crop).unsqueeze(0).to(_device)
    with torch.no_grad():
        logits = _model(tensor)
        probs = torch.sigmoid(logits)[0].cpu().tolist()
    probs_dict = {tag: float(p) for tag, p in zip(_tag_list, probs)}
    tags = frozenset(tag for tag, p in probs_dict.items() if p >= threshold)
    return MinimapVerdict(tags=tags, probs=probs_dict)
