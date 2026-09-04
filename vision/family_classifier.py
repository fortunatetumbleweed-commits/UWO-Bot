"""Phase 4a family classifier — whole-image inference.

Single 50 ms call that returns the bot's nav-state family:

  port_overworld  sea  world_map  chromed  transient

Wraps the model trained by `tools/train_family_classifier.py`.  Lazy
loads once per process; per-frame cache keyed by id(frame) so multiple
callers per perceive tick share inference cost.

Returns FamilyVerdict(family, confidence) — confidence < 0.7 should
fall through to the rule-based cascade.

Graceful when the model is absent: returns FamilyVerdict("unknown", 0.0)
so callers can be wired in before training completes.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger
from vision.frame_cache import FrameCache


_MODEL_PATH = Path("data/models/family_classifier.pt")


@dataclass(frozen=True)
class FamilyVerdict:
    family:     str        # one of CLASSES, or "unknown"
    confidence: float      # softmax prob of the top class, 0..1


# ── Lazy model + state ─────────────────────────────────────────────────────

_model = None
_classes: Optional[list[str]] = None
_input_size: Optional[int] = None
_device: Optional[str] = None
_transform = None
_load_failed = False     # cache the "no model" verdict so we don't keep retrying


def _ensure_loaded() -> bool:
    """Lazy-load the model.  Returns True iff the model is ready.
    False means we should return FamilyVerdict("unknown", 0.0) and let
    the caller fall through."""
    global _model, _classes, _input_size, _device, _transform, _load_failed
    if _load_failed:
        return False
    if _model is not None:
        return True
    if not _MODEL_PATH.exists():
        logger.debug(
            f"[family_classifier] model not found at {_MODEL_PATH} — "
            "run tools/train_family_classifier.py first.  Returning "
            "unknown for all calls."
        )
        _load_failed = True
        return False
    try:
        import torch
        from torch import nn
        from torchvision import transforms
        from torchvision.models import mobilenet_v3_small

        # weights_only=False because the checkpoint dict bundles the
        # class list + meta alongside the state_dict.  We control the
        # file (saved by our own training script), so the unpickle
        # safety concern doesn't apply here.
        ckpt = torch.load(_MODEL_PATH, map_location="cpu", weights_only=False)
        _classes = ckpt["classes"]
        _input_size = ckpt["input_size"]
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _device = device

        # Reconstruct the architecture and load weights
        m = mobilenet_v3_small(weights=None)
        m.classifier[-1] = nn.Linear(
            m.classifier[-1].in_features, len(_classes),
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
            f"[family_classifier] loaded ({m.__class__.__name__}, "
            f"{len(_classes)} classes, device={device}, "
            f"input={_input_size}×{_input_size})"
        )
        return True
    except Exception as exc:
        logger.warning(f"[family_classifier] load failed: {exc}")
        _load_failed = True
        return False


# Keyed on the frame via `vision.frame_cache`, which exists because this exact bug was found
# twice: omniparser 2026-08-21 (197 id collisions in 200 images — a world-map frame came back
# carrying the port overworld's buildings) and here 2026-08-26 (109 in 120 — a survey reported
# `chromed` and `transient` screens as `sea`). Same defect, silent both times.
_RESULT_CACHE = FrameCache("family_classifier", max_entries=8)


def _reset_for_test() -> None:
    """Test hook — drop the cached model so the next call reloads."""
    global _model, _classes, _input_size, _device, _transform, _load_failed
    _model = None
    _classes = None
    _input_size = None
    _device = None
    _transform = None
    _load_failed = False
    _RESULT_CACHE.clear()


# ── Inference ──────────────────────────────────────────────────────────────

def classify_family(frame) -> FamilyVerdict:
    """Classify a frame's nav-state family.

    `frame` is a PIL.Image.  Result is cached so multiple callers within one
    perceive tick share inference cost — PIL Images aren't hashable, so the
    LRU is keyed on the int id AND guarded by a weak reference, because an
    id alone is reused after collection and hands one image another's verdict.

    On no-model / load-failure, returns FamilyVerdict("unknown", 0.0).
    """
    if not _ensure_loaded():
        return FamilyVerdict("unknown", 0.0)

    return _RESULT_CACHE.memoize(frame, lambda: _run_inference(frame))
    if len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
        _RESULT_CACHE.popitem(last=False)
    return verdict


def _run_inference(frame) -> FamilyVerdict:
    """The actual model forward pass.  Separate from classify_family
    so tests can mock either layer independently."""
    import torch

    if _model is None or _transform is None or _classes is None:
        return FamilyVerdict("unknown", 0.0)
    img = frame.convert("RGB")
    tensor = _transform(img).unsqueeze(0).to(_device)
    with torch.no_grad():
        logits = _model(tensor)
        probs = torch.softmax(logits, dim=1)[0]
        top_prob, top_idx = float(probs.max()), int(probs.argmax())
    family = _classes[top_idx]
    return FamilyVerdict(family=family, confidence=top_prob)
