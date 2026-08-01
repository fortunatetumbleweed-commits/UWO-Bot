# classifier/train.py
# Train a MobileNetV3-small screen type classifier on labeled screenshots.
#
# Usage:
#   python -m classifier.train
#   python -m classifier.train --epochs 20 --lr 1e-3
#
# Output:
#   models/screen_classifier/
#     model.pt          — best checkpoint (by val accuracy)
#     class_names.json  — index → class id mapping
#     report.json       — per-class accuracy + confusion matrix

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import torch
import torch.nn as nn
from PIL import Image
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from torchvision import models, transforms

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── paths ─────────────────────────────────────────────────────────────────────

DATA_DIR      = Path(__file__).parent.parent / "data"
SESSIONS_DIR  = DATA_DIR / "sessions"
LABELS_FILE   = DATA_DIR / "labels.jsonl"
MODELS_DIR    = Path(__file__).parent.parent / "models" / "screen_classifier"
SCREEN_TYPES_FILE = DATA_DIR / "knowledge" / "screen_types.json"

MODELS_DIR.mkdir(parents=True, exist_ok=True)

# ── hyper-parameters ──────────────────────────────────────────────────────────

IMAGE_SIZE   = 224       # MobileNetV3 standard input
VAL_SPLIT    = 0.2       # fraction of each class held out for validation
SEED         = 42


# ── data loading ──────────────────────────────────────────────────────────────

def load_samples() -> tuple[list[tuple[Path, str]], list[str]]:
    """
    Read labels.jsonl (last write per frame wins) and return:
      samples   — list of (image_path, class_id)
      class_ids — sorted list of known class ids (from screen_types.json)

    Frames whose label is not in the current screen_types.json are skipped.
    """
    with open(SCREEN_TYPES_FILE) as f:
        known_ids = [st["id"] for st in json.load(f)["screen_types"]]
    known_set = set(known_ids)

    # Last label per frame wins
    labels: dict[str, str] = {}
    with open(LABELS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                key = f"{rec['session_id']}/{rec['file']}"
                labels[key] = rec["screen_type"]
            except (json.JSONDecodeError, KeyError):
                continue

    samples: list[tuple[Path, str]] = []
    for key, class_id in labels.items():
        if class_id not in known_set:
            continue
        session_id, filename = key.split("/", 1)
        img_path = SESSIONS_DIR / session_id / "frames" / filename
        if img_path.exists():
            samples.append((img_path, class_id))

    return samples, known_ids


def stratified_split(
    samples: list[tuple[Path, str]],
    val_fraction: float,
    seed: int,
) -> tuple[list, list]:
    """Split samples into train/val preserving class proportions."""
    rng = random.Random(seed)
    by_class: dict[str, list] = defaultdict(list)
    for s in samples:
        by_class[s[1]].append(s)

    train, val = [], []
    for cls_samples in by_class.values():
        rng.shuffle(cls_samples)
        n_val = max(1, int(len(cls_samples) * val_fraction))
        val.extend(cls_samples[:n_val])
        train.extend(cls_samples[n_val:])
    return train, val


# ── dataset ───────────────────────────────────────────────────────────────────

TRAIN_TRANSFORMS = transforms.Compose([
    transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
    transforms.RandomHorizontalFlip(p=0.3),
    transforms.ColorJitter(brightness=0.2, contrast=0.2),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
])

VAL_TRANSFORMS = transforms.Compose([
    transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
])


class ScreenDataset(Dataset):
    def __init__(
        self,
        samples: list[tuple[Path, str]],
        class_to_idx: dict[str, int],
        transform,
    ):
        self.samples = samples
        self.class_to_idx = class_to_idx
        self.transform = transform

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        img_path, class_id = self.samples[idx]
        img = Image.open(img_path).convert("RGB")
        return self.transform(img), self.class_to_idx[class_id]


# ── model ─────────────────────────────────────────────────────────────────────

def build_model(num_classes: int) -> nn.Module:
    model = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
    # Replace the classifier head
    in_features = model.classifier[3].in_features
    model.classifier[3] = nn.Linear(in_features, num_classes)
    return model


# ── training ──────────────────────────────────────────────────────────────────

def train(
    epochs: int = 15,
    lr: float = 1e-3,
    batch_size: int = 16,
    unfreeze_after: int = 8,
) -> None:
    """
    Phase 1 (epochs 1..unfreeze_after): train classifier head only.
    Phase 2 (epochs unfreeze_after+1..end): unfreeze full network, lower LR.
    """
    device = (
        torch.device("mps") if torch.backends.mps.is_available()
        else torch.device("cuda") if torch.cuda.is_available()
        else torch.device("cpu")
    )
    print(f"Device: {device}")

    samples, class_ids = load_samples()
    if not samples:
        print("No labeled samples found — run the labeler first.")
        return

    class_to_idx = {c: i for i, c in enumerate(class_ids)}
    idx_to_class = {i: c for c, i in class_to_idx.items()}

    train_samples, val_samples = stratified_split(samples, VAL_SPLIT, SEED)

    counts = Counter(s[1] for s in train_samples)
    print(f"\nTotal samples: {len(samples)}  "
          f"(train {len(train_samples)}, val {len(val_samples)})")
    print(f"Classes: {len(class_ids)}")
    print()
    for cid in class_ids:
        n = counts.get(cid, 0)
        bar = "█" * n
        print(f"  {n:3d}  {cid}  {bar}")

    # Weighted sampler to handle class imbalance
    class_weights = {c: 1.0 / max(n, 1) for c, n in counts.items()}
    sample_weights = [class_weights[s[1]] for s in train_samples]
    sampler = WeightedRandomSampler(sample_weights, len(train_samples))

    train_ds = ScreenDataset(train_samples, class_to_idx, TRAIN_TRANSFORMS)
    val_ds   = ScreenDataset(val_samples,   class_to_idx, VAL_TRANSFORMS)
    train_dl = DataLoader(train_ds, batch_size=batch_size, sampler=sampler)
    val_dl   = DataLoader(val_ds,   batch_size=batch_size, shuffle=False)

    model = build_model(len(class_ids)).to(device)

    # Freeze backbone initially
    for p in model.features.parameters():
        p.requires_grad = False

    optimizer = torch.optim.AdamW(
        filter(lambda p: p.requires_grad, model.parameters()), lr=lr
    )
    criterion = nn.CrossEntropyLoss()

    best_val_acc = 0.0
    report: dict = {"epochs": []}

    print()
    for epoch in range(1, epochs + 1):

        # Phase 2: unfreeze backbone at configured epoch
        if epoch == unfreeze_after + 1:
            print(f"\n  → Unfreezing backbone (epoch {epoch})")
            for p in model.features.parameters():
                p.requires_grad = True
            optimizer = torch.optim.AdamW(model.parameters(), lr=lr * 0.1)

        # Train
        model.train()
        train_loss = 0.0
        for imgs, labels in train_dl:
            imgs, labels = imgs.to(device), labels.to(device)
            optimizer.zero_grad()
            loss = criterion(model(imgs), labels)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()
        train_loss /= len(train_dl)

        # Validate
        model.eval()
        correct = 0
        per_class_correct: Counter = Counter()
        per_class_total:   Counter = Counter()

        with torch.no_grad():
            for imgs, labels in val_dl:
                imgs, labels = imgs.to(device), labels.to(device)
                preds = model(imgs).argmax(dim=1)
                correct += (preds == labels).sum().item()
                for p, t in zip(preds.cpu().tolist(), labels.cpu().tolist()):
                    per_class_total[t] += 1
                    if p == t:
                        per_class_correct[t] += 1

        val_acc = correct / len(val_samples)
        print(f"  epoch {epoch:3d}/{epochs}  loss={train_loss:.4f}  "
              f"val_acc={val_acc:.3f}", end="")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(model.state_dict(), MODELS_DIR / "model.pt")
            print("  ✓ saved", end="")
        print()

        report["epochs"].append({
            "epoch": epoch,
            "train_loss": round(train_loss, 4),
            "val_acc": round(val_acc, 4),
        })

    # ── final per-class report ────────────────────────────────────────────────
    print(f"\nBest val accuracy: {best_val_acc:.3f}")
    print("\nPer-class accuracy (on best checkpoint):")

    # Reload best model for final eval
    model.load_state_dict(torch.load(MODELS_DIR / "model.pt", map_location=device))
    model.eval()

    per_class_correct = Counter()
    per_class_total   = Counter()
    confusion: dict[str, Counter] = defaultdict(Counter)

    with torch.no_grad():
        for imgs, labels in val_dl:
            imgs, labels = imgs.to(device), labels.to(device)
            preds = model(imgs).argmax(dim=1)
            for p, t in zip(preds.cpu().tolist(), labels.cpu().tolist()):
                per_class_total[t] += 1
                if p == t:
                    per_class_correct[t] += 1
                confusion[idx_to_class[t]][idx_to_class[p]] += 1

    per_class_acc = {}
    for i, cid in enumerate(class_ids):
        total = per_class_total[i]
        acc   = per_class_correct[i] / total if total > 0 else None
        per_class_acc[cid] = acc
        acc_str = f"{acc:.2f}" if acc is not None else " n/a"
        flag = "  ← low" if acc is not None and acc < 0.7 else ""
        print(f"  {acc_str}  {cid}  ({total} val samples){flag}")

    # Save class names and report
    with open(MODELS_DIR / "class_names.json", "w") as f:
        json.dump(idx_to_class, f, indent=2)

    report["best_val_acc"] = round(best_val_acc, 4)
    report["per_class_acc"] = {k: round(v, 4) if v is not None else None
                                for k, v in per_class_acc.items()}
    report["confusion"] = {k: dict(v) for k, v in confusion.items()}
    with open(MODELS_DIR / "report.json", "w") as f:
        json.dump(report, f, indent=2)

    print(f"\nSaved to {MODELS_DIR}/")
    print(f"  model.pt, class_names.json, report.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train UWO screen classifier")
    parser.add_argument("--epochs",         type=int,   default=15)
    parser.add_argument("--lr",             type=float, default=1e-3)
    parser.add_argument("--batch-size",     type=int,   default=16)
    parser.add_argument("--unfreeze-after", type=int,   default=8,
                        help="Epoch at which to unfreeze the backbone (default 8)")
    args = parser.parse_args()
    train(
        epochs=args.epochs,
        lr=args.lr,
        batch_size=args.batch_size,
        unfreeze_after=args.unfreeze_after,
    )
