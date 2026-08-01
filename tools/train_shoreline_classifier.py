#!/usr/bin/env python
"""Train the sea-view shoreline + beam classifier (ship-relative v2).

Reads the full 3D sea frame and outputs four independent binary signals
that drive manual-steering decisions:

  shore:land_ahead       — bow path blocked by land (must turn)
  shore:land_port        — land off the PORT (left) side of the ship
  shore:land_starboard   — land off the STARBOARD (right) side
  beam_present           — discovery beam (B1..B6 collapsed) visible

A frame can have any combination — open sea = none, river = port +
starboard, frontal wall = ahead alone, river curve = all three, bay
wrapping from port = ahead + port, etc.  Replaces the v1 single-select
S1..S7 softmax that conflated screen-position with ship-relative.

Architecture: MobileNetV3-small (ImageNet pretrained) + a single 4-
output sigmoid head.  Joint BCEWithLogitsLoss with per-tag pos_weight
to handle class imbalance.  Random 80/20 split.

Training corpus: latest row per (session, file) with screen_type=="sea"
and a labeled_by indicating the sea-view + shoreline-migration passes
have run on it.

Usage:
    python tools/train_shoreline_classifier.py

Outputs:
    data/models/shoreline_classifier.pt       weights + meta
    data/models/shoreline_classifier_log.txt  training log + per-tag P/R/F1
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import torch
import torch.nn as nn
import torch.optim as optim
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
from torchvision.models import mobilenet_v3_small, MobileNet_V3_Small_Weights


# ── Tag vocabulary ─────────────────────────────────────────────────────────

TAGS: list[str] = [
    "shore:land_ahead",
    "shore:land_port",
    "shore:land_starboard",
    "beam_present",
]
TAG_TO_IDX = {t: i for i, t in enumerate(TAGS)}
NUM_TAGS = len(TAGS)


# ── Paths ──────────────────────────────────────────────────────────────────

LABELS_PATH = Path("data/labels.jsonl")
SESSIONS_ROOT = Path("data/sessions")
MODEL_OUTPUT = Path("data/models/shoreline_classifier.pt")
LOG_OUTPUT = Path("data/models/shoreline_classifier_log.txt")


# Labellers whose latest row should be trusted for the new shoreline
# binaries.  Includes the migration pass and any future human edits.
_SHORE_LABELERS = {
    "human",
    "claude",                          # sea-view labeller (may carry binaries forward)
    "claude-minimap",
    "claude-restore-minimap",
    "claude-consistency-fix",
    "claude-shoreline-migrate",        # legacy-S* → shore:* migration
    "claude-shoreline-strip",          # removed legacy yolo:S* tags from latest row
}


# ── Data loading ───────────────────────────────────────────────────────────

def _row_target(r: dict) -> torch.Tensor:
    """Build the 4-D target vector from a row's tags."""
    tags = set(r.get("tags") or [])
    target = torch.zeros(NUM_TAGS, dtype=torch.float32)
    if "shore:land_ahead" in tags:
        target[TAG_TO_IDX["shore:land_ahead"]] = 1.0
    if "shore:land_port" in tags:
        target[TAG_TO_IDX["shore:land_port"]] = 1.0
    if "shore:land_starboard" in tags:
        target[TAG_TO_IDX["shore:land_starboard"]] = 1.0
    if any(t.startswith("yolo:B") for t in tags):
        target[TAG_TO_IDX["beam_present"]] = 1.0
    return target


def _load_records():
    """Return (frame_path, target_vector) for every usable sea frame."""
    latest: dict = {}
    with LABELS_PATH.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (r.get("session_id"), r.get("file"))
            if key[0] and key[1]:
                latest[key] = r

    records = []
    skipped = Counter()
    for (sid, fname), r in latest.items():
        if r.get("screen_type") != "sea":
            skipped["non_sea"] += 1
            continue
        if (r.get("labeled_by") or "") not in _SHORE_LABELERS:
            skipped["unlabeled_by_shore_pass"] += 1
            continue
        # An empty shore:* set on a row in the whitelist means "open
        # sea" — S1 frames stripped of the legacy yolo:S1_no_land tag
        # are still valid zero-positive training examples.  No further
        # filtering needed.
        tags = set(r.get("tags") or [])
        path = SESSIONS_ROOT / sid / "frames" / fname
        if not path.exists():
            skipped["missing_frame"] += 1
            continue
        records.append((path, _row_target(r)))
    return records, skipped


class ShorelineDataset(Dataset):
    def __init__(self, records, transform):
        self.records = records
        self.transform = transform

    def __len__(self):
        return len(self.records)

    def __getitem__(self, i):
        path, target = self.records[i]
        img = Image.open(path).convert("RGB")
        return self.transform(img), target


def _split_random(records, val_fraction=0.2, seed=42):
    """Random split.  Multi-label stratification adds complexity for
    little gain at this size."""
    rng = random.Random(seed)
    items = list(records)
    rng.shuffle(items)
    n_val = max(1, int(len(items) * val_fraction))
    return items[n_val:], items[:n_val]


# ── Model ──────────────────────────────────────────────────────────────────

def build_model() -> nn.Module:
    model = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.IMAGENET1K_V1)
    in_features = model.classifier[-1].in_features
    model.classifier[-1] = nn.Linear(in_features, NUM_TAGS)
    return model


# ── Training loop ──────────────────────────────────────────────────────────

INPUT_SIZE = 224
BATCH_SIZE = 32
EPOCHS = 25
LEARNING_RATE = 3e-4


def _build_transforms():
    """No horizontal flip — port/starboard handedness matters."""
    train_tf = transforms.Compose([
        transforms.Resize((INPUT_SIZE, INPUT_SIZE)),
        transforms.ColorJitter(brightness=0.2, contrast=0.2),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225],
        ),
    ])
    val_tf = transforms.Compose([
        transforms.Resize((INPUT_SIZE, INPUT_SIZE)),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225],
        ),
    ])
    return train_tf, val_tf


def _epoch(model, loader, criterion, optimizer, device, train: bool) -> dict:
    model.train(train)
    total_loss, total = 0.0, 0
    tp = torch.zeros(NUM_TAGS)
    fp = torch.zeros(NUM_TAGS)
    fn = torch.zeros(NUM_TAGS)
    tn = torch.zeros(NUM_TAGS)
    with torch.set_grad_enabled(train):
        for images, targets in loader:
            images = images.to(device)
            targets = targets.to(device)
            logits = model(images)
            loss = criterion(logits, targets)
            if train:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            total_loss += loss.item() * images.size(0)
            total += images.size(0)
            preds = (torch.sigmoid(logits) >= 0.5).float()
            tp += ((preds == 1) & (targets == 1)).sum(dim=0).cpu()
            fp += ((preds == 1) & (targets == 0)).sum(dim=0).cpu()
            fn += ((preds == 0) & (targets == 1)).sum(dim=0).cpu()
            tn += ((preds == 0) & (targets == 0)).sum(dim=0).cpu()
    return {
        "loss": total_loss / max(total, 1),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def _per_tag_f1(stats: dict):
    tp, fp, fn = stats["tp"], stats["fp"], stats["fn"]
    prec = tp / torch.clamp(tp + fp, min=1.0)
    rec  = tp / torch.clamp(tp + fn, min=1.0)
    f1   = 2 * prec * rec / torch.clamp(prec + rec, min=1e-6)
    return prec, rec, f1


def _macro_f1(f1, support):
    mask = support > 0
    if mask.sum() == 0:
        return 0.0
    return float(f1[mask].mean())


def _device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main() -> int:
    print("=== Shoreline (ship-relative) + beam multi-label training ===")
    print(f"Tags ({NUM_TAGS}): {TAGS}")
    device = _device()
    print(f"Device: {device}")
    print()

    records, skipped = _load_records()
    if not records:
        print(f"No usable records.  Skipped: {dict(skipped)}")
        print()
        print("Did you run tools/migrate_shoreline_to_binary.py --apply ?")
        return 1
    print(f"Loaded {len(records)} usable sea frames")
    print(f"Skipped: {dict(skipped)}")

    pos = torch.zeros(NUM_TAGS)
    for _, t in records:
        pos += t
    print()
    print(f"{'Tag':30s} {'Pos':>5s}  {'Pos%':>6s}")
    for i, tag in enumerate(TAGS):
        n = int(pos[i])
        pct = 100.0 * n / len(records)
        print(f"  {tag:28s}  {n:>5d}  {pct:>5.1f}%")

    train_recs, val_recs = _split_random(records, val_fraction=0.2)
    print()
    print(f"Train: {len(train_recs)}  Val: {len(val_recs)}")

    train_tf, val_tf = _build_transforms()
    train_loader = DataLoader(
        ShorelineDataset(train_recs, train_tf),
        batch_size=BATCH_SIZE, shuffle=True,
        num_workers=2, pin_memory=(device != "cpu"),
    )
    val_loader = DataLoader(
        ShorelineDataset(val_recs, val_tf),
        batch_size=BATCH_SIZE, shuffle=False,
        num_workers=2, pin_memory=(device != "cpu"),
    )

    model = build_model().to(device)
    optimizer = optim.AdamW(model.parameters(), lr=LEARNING_RATE)

    pos_weight = torch.zeros(NUM_TAGS)
    for i in range(NUM_TAGS):
        p = float(pos[i])
        n = len(records) - p
        pos_weight[i] = (n / p) if p > 0 else 1.0
    pos_weight = torch.clamp(pos_weight, max=20.0).to(device)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    log_lines = []
    best_score = -1.0
    best_state = None
    best_val_stats = None

    print()
    print(f"{'Epoch':>5s}  {'Train Loss':>10s}  {'Val Loss':>9s}  "
          f"{'Val MacroF1':>11s}  {'Time':>5s}")
    for epoch in range(1, EPOCHS + 1):
        t0 = time.time()
        train_stats = _epoch(model, train_loader, criterion, optimizer, device, True)
        val_stats = _epoch(model, val_loader, criterion, optimizer, device, False)
        dt = time.time() - t0
        _, _, val_f1 = _per_tag_f1(val_stats)
        val_macro = _macro_f1(val_f1, val_stats["tp"] + val_stats["fn"])
        line = (
            f"  {epoch:>3d}  {train_stats['loss']:>10.4f}  "
            f"{val_stats['loss']:>9.4f}  {val_macro:>11.3f}  {dt:>4.1f}s"
        )
        print(line)
        log_lines.append(line)
        if val_macro > best_score:
            best_score = val_macro
            best_state = {
                k: v.detach().cpu().clone() for k, v in model.state_dict().items()
            }
            best_val_stats = val_stats

    print()
    print(f"Best val macro-F1: {best_score:.3f}")
    log_lines.append(f"\nBest val macro-F1: {best_score:.3f}")
    print()
    print(f"Per-tag val metrics (at best epoch):")
    log_lines.append("\nPer-tag val metrics:")
    header = f"  {'tag':30s}  {'P':>5s}  {'R':>5s}  {'F1':>5s}  {'#pos':>5s}"
    print(header)
    log_lines.append(header)
    prec, rec, f1 = _per_tag_f1(best_val_stats)
    support = best_val_stats["tp"] + best_val_stats["fn"]
    for i, tag in enumerate(TAGS):
        line = (
            f"  {tag:30s}  {float(prec[i]):>5.2f}  {float(rec[i]):>5.2f}  "
            f"{float(f1[i]):>5.2f}  {int(support[i]):>5d}"
        )
        print(line)
        log_lines.append(line)

    MODEL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "state_dict":  best_state,
        "tags":        TAGS,
        "input_size":  INPUT_SIZE,
        "best_val_macro_f1": best_score,
        "model_arch":  "mobilenet_v3_small",
        "saved_at":    time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, MODEL_OUTPUT)
    LOG_OUTPUT.write_text("\n".join(log_lines))
    print()
    print(f"✓ Saved model to {MODEL_OUTPUT}")
    print(f"✓ Saved log to {LOG_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
