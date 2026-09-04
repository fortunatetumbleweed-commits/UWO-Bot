#!/usr/bin/env python
"""Train the Phase 4a family classifier.

Whole-image image classification for the bot's nav-state family.  The
old chrome → fingerprint → OCR cascade kept producing false positives
on screens where a non-title element happened to be tagged with a
building-name label.  A single small CNN looking at the whole frame
distinguishes the families purely on background gestalt — port world
vs sea horizon vs world map vs interior chrome.

Family vocabulary (6 classes):
  port_overworld   port_overworld + port_arrival_overlay + port_loading + port_map
  sea              sea + sailing_idle
  world_map        world_map
  chromed          building_interior + sub_menu + village + main_menu
  transient        dialog_* + announcement + result_screen + loading + others
  idle_lock        the standby lock — its own family because its EXIT is a swipe

Training corpus: data/labels.jsonl (1015 labelled frames).  Stratified
80/20 train/val split.  MobileNetV3-small pretrained on ImageNet,
classifier head replaced for 5 classes.

Usage:
    python tools/train_family_classifier.py

Outputs:
    data/models/family_classifier.pt    state dict + class names + meta
    data/models/family_classifier_log.txt  training log + per-class metrics
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

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


# ── Family mapping ─────────────────────────────────────────────────────────

SCREEN_TYPE_TO_FAMILY: dict[str, str] = {
    "port_overworld":       "port_overworld",
    "port_arrival_overlay": "port_overworld",
    "port_loading":         "port_overworld",
    "port_map":             "port_overworld",

    "sea":                  "sea",
    "sailing_idle":         "sea",

    "world_map":            "world_map",

    "building_interior":    "chromed",
    "sub_menu":             "chromed",
    "village":              "chromed",
    "main_menu":            "chromed",

    "dialog_gameplay":      "transient",
    "dialog_event":         "transient",
    "dialog_system":        "transient",
    "dialog_android":       "transient",
    "dialog_reward":        "transient",
    "dialog_game_notice":   "transient",
    "dialog_shop":          "transient",
    "dialog_transaction":   "transient",
    "dialog_overlay":       "transient",
    "announcement":         "transient",
    "result_screen":        "transient",
    "loading":              "transient",
    "negotiation":          "transient",
    "story_npc_conversation": "transient",
    "building_npc_overlay": "transient",
    "task_progress":        "transient",

    # THE IDLE LOCK IS ITS OWN FAMILY, added 2026-08-27 (user's suggestion). It did not
    # exist when this model was trained, so it fell into `transient` by default — and that
    # is exactly why it was mistaken for a full-screen notice: same gestalt, no chrome, no
    # buttons. But its exit is a SWIPE where a notice is TAPPED, and the bot tapped a screen
    # that only answers to a gesture (live 2026-08-27, "Barcelona / Slide up to unlock"
    # classified transient@0.80). A family whose members need different ACTIONS is not one
    # family.
    "idle_lock":            "idle_lock",
    # 'other' is intentionally NOT mapped — those go to the negative class
    # and aren't trained against any family.
}

CLASSES = sorted(set(SCREEN_TYPE_TO_FAMILY.values()))   # 6 in alphabetical order
NUM_CLASSES = len(CLASSES)
CLASS_TO_IDX = {c: i for i, c in enumerate(CLASSES)}


# ── Data loading ───────────────────────────────────────────────────────────

LABELS_PATH = Path("data/labels.jsonl")
SESSIONS_ROOT = Path("data/sessions")
MODEL_OUTPUT = Path("data/models/family_classifier.pt")
LOG_OUTPUT = Path("data/models/family_classifier_log.txt")


def _load_records():
    """Return list of (frame_path, family_class_idx) for all usable
    labelled frames."""
    records = []
    skipped = Counter()
    with LABELS_PATH.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                skipped["json_error"] += 1
                continue
            scr = r.get("screen_type")
            family = SCREEN_TYPE_TO_FAMILY.get(scr)
            if family is None:
                skipped["unmapped_screen_type"] += 1
                continue
            session = r.get("session_id", "")
            fname = r.get("file", "")
            path = SESSIONS_ROOT / session / "frames" / fname
            if not path.exists():
                # Action traces keep their frames beside the trace, not under frames/.
                alt = SESSIONS_ROOT / session / fname
                if alt.exists():
                    path = alt
                else:
                    skipped["missing_frame"] += 1
                    continue
            records.append((path, CLASS_TO_IDX[family]))
    return records, skipped


class FrameDataset(Dataset):
    def __init__(self, records, transform):
        self.records = records
        self.transform = transform

    def __len__(self):
        return len(self.records)

    def __getitem__(self, i):
        path, label = self.records[i]
        img = Image.open(path).convert("RGB")
        return self.transform(img), label


def _split_stratified(records, val_fraction=0.2, seed=42):
    """Stratified train/val split.  Each class contributes
    val_fraction of its frames to the val set."""
    rng = random.Random(seed)
    by_class = defaultdict(list)
    for r in records:
        by_class[r[1]].append(r)
    train, val = [], []
    for cls, items in by_class.items():
        rng.shuffle(items)
        n_val = max(1, int(len(items) * val_fraction))
        val.extend(items[:n_val])
        train.extend(items[n_val:])
    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


# ── Model ──────────────────────────────────────────────────────────────────

def build_model() -> nn.Module:
    """MobileNetV3-small with the final classifier swapped for our 5
    classes.  ImageNet pretraining gives us decent feature extractors
    for free — only the head needs to be retrained on the game frames."""
    model = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.IMAGENET1K_V1)
    # Replace the last linear layer (1000 → NUM_CLASSES).  Keeping the
    # rest of the classifier (Linear → Hardswish → Dropout) intact.
    in_features = model.classifier[-1].in_features
    model.classifier[-1] = nn.Linear(in_features, NUM_CLASSES)
    return model


# ── Training loop ──────────────────────────────────────────────────────────

INPUT_SIZE = 224     # MobileNetV3 standard
BATCH_SIZE = 32
EPOCHS = 12
LEARNING_RATE = 3e-4


def _build_transforms():
    """Resize + normalise.  Minimal augmentation — UWO frames are
    always landscape 2400×1080 from a fixed camera, no rotation/flip
    in the data distribution.  Slight brightness jitter covers the
    day/night cycle the bot encounters."""
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
    total_loss, correct, total = 0.0, 0, 0
    per_class_correct = Counter()
    per_class_total = Counter()
    with torch.set_grad_enabled(train):
        for images, labels in loader:
            images, labels = images.to(device), labels.to(device)
            logits = model(images)
            loss = criterion(logits, labels)
            if train:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            total_loss += loss.item() * images.size(0)
            preds = logits.argmax(dim=1)
            correct += (preds == labels).sum().item()
            total += images.size(0)
            for p, l in zip(preds.tolist(), labels.tolist()):
                per_class_total[l] += 1
                if p == l:
                    per_class_correct[l] += 1
    return {
        "loss": total_loss / max(total, 1),
        "acc":  correct / max(total, 1),
        "per_class_correct": per_class_correct,
        "per_class_total":   per_class_total,
    }


def _device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main() -> int:
    print(f"=== Phase 4a family classifier training ===")
    print(f"Classes ({NUM_CLASSES}): {CLASSES}")
    device = _device()
    print(f"Device: {device}")
    print()

    records, skipped = _load_records()
    if not records:
        print(f"No usable records found.  Skipped: {dict(skipped)}")
        return 1
    print(f"Loaded {len(records)} labelled frames")
    print(f"Skipped: {dict(skipped)}")

    # Per-class counts
    class_counts = Counter(idx for _, idx in records)
    print()
    print(f"{'Class':20s} {'Count':>6s}")
    for cls in CLASSES:
        print(f"  {cls:18s} {class_counts[CLASS_TO_IDX[cls]]:>6d}")

    train_recs, val_recs = _split_stratified(records, val_fraction=0.2)
    print()
    print(f"Train: {len(train_recs)}  Val: {len(val_recs)}")

    train_tf, val_tf = _build_transforms()
    train_ds = FrameDataset(train_recs, train_tf)
    val_ds = FrameDataset(val_recs, val_tf)
    train_loader = DataLoader(
        train_ds, batch_size=BATCH_SIZE, shuffle=True,
        num_workers=2, pin_memory=(device != "cpu"),
    )
    val_loader = DataLoader(
        val_ds, batch_size=BATCH_SIZE, shuffle=False,
        num_workers=2, pin_memory=(device != "cpu"),
    )

    model = build_model().to(device)
    optimizer = optim.AdamW(model.parameters(), lr=LEARNING_RATE)
    criterion = nn.CrossEntropyLoss()

    log_lines = []
    best_val_acc = 0.0
    best_state = None

    print()
    print(f"{'Epoch':>5s}  {'Train Loss':>10s}  {'Train Acc':>9s}  "
          f"{'Val Loss':>9s}  {'Val Acc':>8s}  {'Time':>5s}")
    for epoch in range(1, EPOCHS + 1):
        t0 = time.time()
        train_stats = _epoch(model, train_loader, criterion, optimizer, device, True)
        val_stats = _epoch(model, val_loader, criterion, optimizer, device, False)
        dt = time.time() - t0
        line = (
            f"  {epoch:>3d}  {train_stats['loss']:>10.4f}  {train_stats['acc']:>9.3f}  "
            f"{val_stats['loss']:>9.4f}  {val_stats['acc']:>8.3f}  {dt:>4.1f}s"
        )
        print(line)
        log_lines.append(line)
        if val_stats["acc"] > best_val_acc:
            best_val_acc = val_stats["acc"]
            best_state = {
                k: v.detach().cpu().clone() for k, v in model.state_dict().items()
            }
            best_val_stats = val_stats

    print()
    print(f"Best val acc: {best_val_acc:.3f}")
    log_lines.append(f"\nBest val acc: {best_val_acc:.3f}")
    print()
    print(f"Per-class val accuracy:")
    log_lines.append("\nPer-class val accuracy:")
    for cls in CLASSES:
        idx = CLASS_TO_IDX[cls]
        n = best_val_stats["per_class_total"][idx]
        c = best_val_stats["per_class_correct"][idx]
        acc = c / n if n else 0.0
        line = f"  {cls:18s}  {c:>3d} / {n:>3d}  ({acc:.3f})"
        print(line)
        log_lines.append(line)

    MODEL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "state_dict":  best_state,
        "classes":     CLASSES,
        "screen_type_to_family": SCREEN_TYPE_TO_FAMILY,
        "input_size":  INPUT_SIZE,
        "best_val_acc": best_val_acc,
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
