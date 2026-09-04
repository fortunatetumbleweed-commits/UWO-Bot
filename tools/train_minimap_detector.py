#!/usr/bin/env python
"""Train the mini-map multi-label detector.

The top-right radar (mini-map) is the bot's most reliable navigation
substrate — stable across regions / weather / time of day, and unlike
the full sea-view it can be cropped to a small fixed region.  This
script trains a single MobileNetV3-small backbone with a 10-output
sigmoid head that reads the mini-map crop and reports which of the
following sprites are visible:

    minimap:land                     — landmass (crisp-edge whitish blob)
    minimap:port_anchor              — anchor sprite (known port nearby)
    minimap:village_building         — building sprite (village nearby)
    minimap:settlement_name_text     — port / village name text in radar
    minimap:undiscovered_marker      — ??? sprite (not-yet-discovered port)
    minimap:fleet_merchant           — white diamond NPC merchant
    minimap:fleet_pirate_regular     — yellow side-facing pirate head
    minimap:fleet_pirate_special     — yellow front-facing mustachioed boss
    minimap:player_neutral           — neutral player asterisk
    minimap:player_guild             — same-guild player asterisk

Training corpus: data/labels.jsonl rows with screen_type=="sea" and a
labeled_by that has been through mini-map labelling (human or any of
the claude-* mini-map / consistency passes).  Multi-label targets — a
single frame can have several tags simultaneously.

Random 80/20 split (multi-label stratification isn't worth the
complexity at this corpus size).

Usage:
    python tools/train_minimap_detector.py

Outputs:
    data/models/minimap_detector.pt        state dict + tag list + meta
    data/models/minimap_detector_log.txt   training log + per-tag P/R/F1
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# Project root on sys.path
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
    "minimap:land",
    "minimap:port_anchor",
    "minimap:village_building",
    "minimap:settlement_name_text",
    "minimap:undiscovered_marker",
    "minimap:fleet_merchant",
    "minimap:fleet_pirate_regular",
    "minimap:fleet_pirate_special",
    "minimap:player_neutral",
    "minimap:player_guild",
]
TAG_TO_IDX = {t: i for i, t in enumerate(TAGS)}
NUM_TAGS = len(TAGS)


# ── Paths ──────────────────────────────────────────────────────────────────

LABELS_PATH = Path("data/labels.jsonl")
SESSIONS_ROOT = Path("data/sessions")
MODEL_OUTPUT = Path("data/models/minimap_detector.pt")
LOG_OUTPUT = Path("data/models/minimap_detector_log.txt")

# Top-right radar region on a 2400×1080 frame.  See
# memory/project_minimap_as_navigation_radar.md.
# DO NOT point this at the live navigation crop. This is the region the DETECTOR MODEL is
# trained and labelled on: it is written into the checkpoint as `ckpt["minimap_crop"]` and
# read back at inference by `vision/minimap_reader.py`, so it is a contract with the trained
# weights, not a description of where the mini-map currently sits on screen. Changing it to
# follow UI drift would silently feed the model a region it was never trained on.
# The live UI crop is `vision.minimap_navigation_view.get_minimap_crop()`.
MINIMAP_CROP = (2055, 140, 2400, 360)   # (left, top, right, bottom)


# ── Data loading ───────────────────────────────────────────────────────────

# `labeled_by` values that imply mini-map tagging was considered for
# this frame.  Other values (notably "claude-screen-type") leave the
# mini-map question unanswered and must be excluded so we don't train
# on noisy negatives.
_MINIMAP_LABELERS = {
    "human",
    "claude-minimap",
    "claude-restore-minimap",
    "claude-consistency-fix",
    "claude",                      # sea-view labeller — preserves minimap tags via merge
    "claude-shoreline-migrate",    # appended rows preserve minimap tags
    "claude-shoreline-strip",      # ditto — only removes legacy yolo:S*
}


def _load_records():
    """Return (frame_path, target_vector) for every usable sea frame.

    Picks the LATEST row per (session, file), drops frames whose latest
    labeller hasn't considered the mini-map.
    """
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
        if (r.get("labeled_by") or "") not in _MINIMAP_LABELERS:
            skipped["unlabeled_by_minimap_pass"] += 1
            continue
        path = SESSIONS_ROOT / sid / "frames" / fname
        if not path.exists():
            skipped["missing_frame"] += 1
            continue
        tags = set(r.get("tags") or [])
        target = torch.zeros(NUM_TAGS, dtype=torch.float32)
        for t in tags:
            i = TAG_TO_IDX.get(t)
            if i is not None:
                target[i] = 1.0
        records.append((path, target))
    return records, skipped


class MinimapDataset(Dataset):
    """Loads a frame, crops the mini-map region, applies transforms."""

    def __init__(self, records, transform):
        self.records = records
        self.transform = transform

    def __len__(self):
        return len(self.records)

    def __getitem__(self, i):
        path, target = self.records[i]
        img = Image.open(path).convert("RGB")
        # Hardware-fixed phone resolution per CLAUDE.md is 2400×1080.
        # Robustly crop on the right edge in case of off-by-one.
        w, h = img.size
        # Use the canonical crop scaled if the frame size differs (rare).
        if (w, h) != (2400, 1080):
            sx = w / 2400.0
            sy = h / 1080.0
            left   = int(MINIMAP_CROP[0] * sx)
            top    = int(MINIMAP_CROP[1] * sy)
            right  = int(MINIMAP_CROP[2] * sx)
            bottom = int(MINIMAP_CROP[3] * sy)
        else:
            left, top, right, bottom = MINIMAP_CROP
        crop = img.crop((left, top, right, bottom))
        return self.transform(crop), target


def _split_random(records, val_fraction=0.2, seed=42):
    """Random split.  Multi-label stratification isn't worth the
    complexity at ~240 examples — random gives a reasonable mix and the
    training is robust to it."""
    rng = random.Random(seed)
    items = list(records)
    rng.shuffle(items)
    n_val = max(1, int(len(items) * val_fraction))
    return items[n_val:], items[:n_val]


# ── Model ──────────────────────────────────────────────────────────────────

def build_model() -> nn.Module:
    """MobileNetV3-small with the final linear swapped for NUM_TAGS
    sigmoid outputs.  Same backbone as the family classifier so we
    inherit ImageNet pretraining and a small parameter count."""
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
    """Crop is already done by the dataset.  Apply ImageNet-style resize
    and normalisation here.  Light brightness / contrast jitter covers
    day/night radar tinting; no rotation/flip — the radar's orientation
    is meaningful (the ship icon points in the bearing direction)."""
    train_tf = transforms.Compose([
        transforms.Resize((INPUT_SIZE, INPUT_SIZE)),
        transforms.ColorJitter(brightness=0.15, contrast=0.15),
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
    total_loss = 0.0
    total = 0
    # per-tag confusion counts, thresholded at 0.5
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
            t = targets
            tp += ((preds == 1) & (t == 1)).sum(dim=0).cpu()
            fp += ((preds == 1) & (t == 0)).sum(dim=0).cpu()
            fn += ((preds == 0) & (t == 1)).sum(dim=0).cpu()
            tn += ((preds == 0) & (t == 0)).sum(dim=0).cpu()
    return {
        "loss": total_loss / max(total, 1),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def _per_tag_f1(stats: dict) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    tp, fp, fn = stats["tp"], stats["fp"], stats["fn"]
    prec = tp / torch.clamp(tp + fp, min=1.0)
    rec  = tp / torch.clamp(tp + fn, min=1.0)
    f1   = 2 * prec * rec / torch.clamp(prec + rec, min=1e-6)
    return prec, rec, f1


def _macro_f1(f1: torch.Tensor, tag_support: torch.Tensor) -> float:
    """Macro F1 over tags that actually appear in the val set — ignore
    tags with zero positives (otherwise their F1 is undefined / zero
    and tanks the metric)."""
    mask = tag_support > 0
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
    print("=== Mini-map multi-label detector training ===")
    print(f"Tags ({NUM_TAGS}): {TAGS}")
    device = _device()
    print(f"Device: {device}")
    print()

    records, skipped = _load_records()
    if not records:
        print(f"No usable records.  Skipped: {dict(skipped)}")
        return 1
    print(f"Loaded {len(records)} usable sea frames")
    print(f"Skipped: {dict(skipped)}")

    # Per-tag positive counts (support).
    pos_counts = torch.zeros(NUM_TAGS)
    for _, t in records:
        pos_counts += t
    print()
    print(f"{'Tag':40s} {'Pos':>6s}  {'Pos%':>6s}")
    for i, tag in enumerate(TAGS):
        n = int(pos_counts[i])
        pct = 100.0 * n / len(records)
        print(f"  {tag:38s}  {n:>6d}  {pct:>5.1f}%")

    train_recs, val_recs = _split_random(records, val_fraction=0.2)
    print()
    print(f"Train: {len(train_recs)}  Val: {len(val_recs)}")

    train_tf, val_tf = _build_transforms()
    train_loader = DataLoader(
        MinimapDataset(train_recs, train_tf),
        batch_size=BATCH_SIZE, shuffle=True,
        num_workers=2, pin_memory=(device != "cpu"),
    )
    val_loader = DataLoader(
        MinimapDataset(val_recs, val_tf),
        batch_size=BATCH_SIZE, shuffle=False,
        num_workers=2, pin_memory=(device != "cpu"),
    )

    model = build_model().to(device)
    optimizer = optim.AdamW(model.parameters(), lr=LEARNING_RATE)

    # pos_weight = (#negatives / #positives) per tag — counters class
    # imbalance for the rarer sprites (player_guild ≈ 2%).
    pos_weight = torch.zeros(NUM_TAGS)
    for i in range(NUM_TAGS):
        pos = float(pos_counts[i])
        neg = len(records) - pos
        pos_weight[i] = (neg / pos) if pos > 0 else 1.0
    pos_weight = torch.clamp(pos_weight, max=20.0).to(device)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    log_lines = []
    best_val_f1 = -1.0
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
        if val_macro > best_val_f1:
            best_val_f1 = val_macro
            best_state = {
                k: v.detach().cpu().clone() for k, v in model.state_dict().items()
            }
            best_val_stats = val_stats

    print()
    print(f"Best val macro-F1: {best_val_f1:.3f}")
    log_lines.append(f"\nBest val macro-F1: {best_val_f1:.3f}")
    print()
    print(f"Per-tag val metrics (at best epoch):")
    log_lines.append("\nPer-tag val metrics:")
    header = f"  {'tag':38s}  {'P':>5s}  {'R':>5s}  {'F1':>5s}  {'#pos':>5s}"
    print(header)
    log_lines.append(header)
    prec, rec, f1 = _per_tag_f1(best_val_stats)
    support = best_val_stats["tp"] + best_val_stats["fn"]
    for i, tag in enumerate(TAGS):
        line = (
            f"  {tag:38s}  {float(prec[i]):>5.2f}  {float(rec[i]):>5.2f}  "
            f"{float(f1[i]):>5.2f}  {int(support[i]):>5d}"
        )
        print(line)
        log_lines.append(line)

    MODEL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "state_dict":  best_state,
        "tags":        TAGS,
        "input_size":  INPUT_SIZE,
        "minimap_crop": list(MINIMAP_CROP),
        "best_val_macro_f1": best_val_f1,
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
