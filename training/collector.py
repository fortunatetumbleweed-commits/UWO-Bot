# training/collector.py
# Saves labeled examples produced when Claude is called as a teacher fallback.
#
# Every example records:
#   - the image crop or frame that was sent to Claude
#   - what the local model predicted (if anything) and its confidence
#   - what Claude returned as the ground-truth label
#   - why Claude was called ("trigger")
#
# Over time these accumulate into a per-category training dataset.
# When a category has enough examples, a local model can be retrained on
# Claude's labels (distillation) and the Claude call rate drops toward zero.
#
# Storage layout:
#   data/training/<category>/
#     examples.jsonl          one record per line, newest at end
#     crops/                  saved image crops (PNG)
#
# Usage:
#   from training.collector import save_example, load_stats, load_examples

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

_TRAINING_DIR = Path(__file__).parent.parent / "data" / "training"


# ── write ──────────────────────────────────────────────────────────────────────

def save_example(
    category: str,
    image: Image.Image,
    claude_label: dict[str, Any],
    *,
    local_prediction: str | None = None,
    local_confidence: float | None = None,
    trigger: str = "unknown",
) -> None:
    """
    Persist one teacher-labeled training example.

    Parameters
    ----------
    category         : logical task group — "market_tile", "scene_type", …
    image            : the crop or frame Claude was shown
    claude_label     : structured output from Claude (keys are task-specific)
    local_prediction : what the local model said before Claude was called (or None)
    local_confidence : confidence score of the local model (or None)
    trigger          : why Claude was called, e.g.:
                         "no_name"   — OCR produced no item name
                         "no_price"  — OCR produced no price
                         "low_conf"  — classifier confidence below threshold
                         "unknown"   — catch-all
    """
    cat_dir   = _TRAINING_DIR / category
    crops_dir = cat_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    ts     = datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y%m%d_%H%M%S_%f")

    img_name = f"{ts_str}.png"
    image.save(crops_dir / img_name)

    record: dict[str, Any] = {
        "timestamp":        ts.isoformat(),
        "image":            img_name,
        "trigger":          trigger,
        "local_prediction": local_prediction,
        "local_confidence": local_confidence,
        "claude_label":     claude_label,
    }

    with open(cat_dir / "examples.jsonl", "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── read ───────────────────────────────────────────────────────────────────────

def load_examples(
    category: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return up to `limit` examples for a category (newest first)."""
    path = _TRAINING_DIR / category / "examples.jsonl"
    if not path.exists():
        return []
    lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    # Newest first
    lines = list(reversed(lines))
    lines = lines[offset: offset + limit]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_stats() -> list[dict]:
    """
    Return summary stats for every category that has training data.

    Each entry:
      category        : str
      total           : int   — total examples ever saved
      this_week       : int   — examples saved in last 7 days
      triggers        : dict  — {trigger: count}
      disagreements   : int   — local_prediction != claude primary label
      label_dist      : dict  — distribution of claude labels (top-level key)
    """
    from datetime import timedelta
    if not _TRAINING_DIR.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    stats  = []

    for cat_dir in sorted(_TRAINING_DIR.iterdir()):
        if not cat_dir.is_dir():
            continue
        path = cat_dir / "examples.jsonl"
        if not path.exists():
            continue

        category     = cat_dir.name
        total        = 0
        this_week    = 0
        triggers: dict[str, int]     = {}
        label_dist: dict[str, int]   = {}
        disagreements = 0

        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            total += 1

            ts_str = rec.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts > cutoff:
                    this_week += 1
            except ValueError:
                pass

            trigger = rec.get("trigger", "unknown")
            triggers[trigger] = triggers.get(trigger, 0) + 1

            # Primary label: first string value in claude_label dict
            label = rec.get("claude_label", {})
            primary = _primary_label(label)
            if primary:
                label_dist[primary] = label_dist.get(primary, 0) + 1

            local = rec.get("local_prediction")
            if local is not None and primary is not None and local != primary:
                disagreements += 1

        stats.append({
            "category":     category,
            "total":        total,
            "this_week":    this_week,
            "triggers":     triggers,
            "label_dist":   label_dist,
            "disagreements": disagreements,
        })

    return stats


# ── Name correction cache ─────────────────────────────────────────────────────
# Maps known OCR misreads to their correct names.
# Built automatically from Claude fallback responses; applied in the OCR pipeline
# so the same Claude call is never needed again for the same misread.
#
# File: data/training/market_tile/name_corrections.json
# Format: {"Tequlla": "Tequila", "Topacco": "Tobacco", ...}

_CORRECTIONS_FILE = _TRAINING_DIR / "market_tile" / "name_corrections.json"


def load_name_corrections() -> dict[str, str]:
    """Return {ocr_misread: correct_name} for all known corrections."""
    if not _CORRECTIONS_FILE.exists():
        return {}
    try:
        return json.loads(_CORRECTIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_name_correction(ocr_name: str, correct_name: str) -> None:
    """
    Record that `ocr_name` (what EasyOCR read) should be `correct_name`.
    No-op if they are the same or if the correction already exists.
    """
    if not ocr_name or not correct_name or ocr_name == correct_name:
        return
    corrections = load_name_corrections()
    if corrections.get(ocr_name) == correct_name:
        return  # already known
    corrections[ocr_name] = correct_name
    _CORRECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CORRECTIONS_FILE.write_text(
        json.dumps(corrections, indent=2, ensure_ascii=False)
    )
    from loguru import logger
    logger.info(f"  [corrections] Learned: {ocr_name!r} → {correct_name!r} "
                f"({len(corrections)} total)")


def apply_name_correction(name: str) -> str:
    """Return the corrected name if a correction exists, else the original."""
    if not name:
        return name
    corrections = load_name_corrections()
    return corrections.get(name, name)


def _primary_label(claude_label: dict) -> str | None:
    """Extract the most informative single string from a claude_label dict."""
    # Prefer "name" for market_tile, "scene_type" for classifiers
    for key in ("name", "scene_type", "label", "type"):
        v = claude_label.get(key)
        if isinstance(v, str) and v:
            return v
    # Fallback: first string value
    for v in claude_label.values():
        if isinstance(v, str) and v:
            return v
    return None
