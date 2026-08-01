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


_RESULT_CACHE: "OrderedDict[int, FamilyVerdict]" = OrderedDict()
_RESULT_CACHE_MAX = 8


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

    `frame` is a PIL.Image.  Result is cached per `id(frame)` so multiple
    callers within one perceive tick share inference cost — PIL Images
    aren't hashable so we use a small manual LRU keyed on the int id.

    On no-model / load-failure, returns FamilyVerdict("unknown", 0.0).
    """
    if not _ensure_loaded():
        return FamilyVerdict("unknown", 0.0)

    key = id(frame)
    cached = _RESULT_CACHE.get(key)
    if cached is not None:
        _RESULT_CACHE.move_to_end(key)
        return cached

    verdict = _run_inference(frame)
    _RESULT_CACHE[key] = verdict
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
