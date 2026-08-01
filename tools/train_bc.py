"""Behavioral cloning trainer for the learned navigation controller.

Phase 2 of `docs/learned_navigation_controller_plan.md`.  Trains
the NavController to predict the action labels in the offline
dataset built by `tools/build_rl_dataset.py`.  Cross-entropy loss.
No reward, no value estimation — just supervised classification.

Once BC works end-to-end (data → model → checkpoint that runs at
inference time), we'll graduate to IQL (Phase 2.5) which adds the
reward signal.

Usage
─────
  python -m tools.train_bc \\
      --dataset data/rl/dataset_v1.npz \\
      --out models/nav_controller_bc_v1.pt \\
      --epochs 30 --batch-size 64

  # Smoke test (1 epoch, small subset):
  python -m tools.train_bc \\
      --dataset data/rl/dataset_v1.npz \\
      --out /tmp/nav_smoke.pt --epochs 1 --subset 200
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, Sampler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.ai_nav.learned.model import (
    NavController, count_params, IMG_HEIGHT, IMG_WIDTH, N_AUX, N_ACTIONS,
)


# ImageNet mean/std normalization — standard practice even when not
# using a pretrained model; helps the optimizer.
IMAGENET_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
IMAGENET_STD  = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)


def _load_session_labels(frame_paths: np.ndarray) -> list[dict]:
    """For each row in frame_paths, return its labels.jsonl entry
    (empty dict when no entry exists).  Single-pass loader with
    per-session caching."""
    import json
    cache: dict[Path, dict[int, dict]] = {}
    out: list[dict] = []
    for p in frame_paths:
        path = ROOT / str(p)
        sess_dir = path.parent
        if sess_dir not in cache:
            labels: dict[int, dict] = {}
            lpath = sess_dir / "labels.jsonl"
            if lpath.exists():
                for line in lpath.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        labels[int(rec["tick"])] = rec
                    except Exception:
                        pass
            cache[sess_dir] = labels
        tick = int(path.stem.split("_")[-1])
        out.append(cache[sess_dir].get(tick) or {})
    return out


def _load_training_overrides(frame_paths: np.ndarray) -> np.ndarray:
    """Pull `training_include` (True/False/None) per row.  See
    `_load_session_labels` for the per-session label loader."""
    out = np.empty(len(frame_paths), dtype=object)
    out[:] = None
    for i, rec in enumerate(_load_session_labels(frame_paths)):
        if "training_include" in rec:
            out[i] = bool(rec["training_include"])
    return out


def _load_corrected_headings(frame_paths: np.ndarray) -> np.ndarray:
    """Pull `corrected_heading` (degrees, or NaN) per row.  Set via
    the tick_viewer `H` key (adopt motion bearing as heading)."""
    out = np.full(len(frame_paths), np.nan, dtype=np.float32)
    for i, rec in enumerate(_load_session_labels(frame_paths)):
        if "corrected_heading" in rec:
            out[i] = float(rec["corrected_heading"])
    return out


class BCDataset(Dataset):
    """Loads (image, aux, action_idx) tuples from the v2 NPZ built by
    tools/build_rl_dataset.py.  Images are loaded on demand to avoid
    blowing RAM.

    Aux layout (7 floats, matches `brain/ai_nav/learned/model.py::N_AUX`):
        heading_sin, heading_cos,
        dlat_5tick, dlon_5tick,
        is_channel, is_junction, is_dead_end
    """

    def __init__(self, npz_path: Path, indices: np.ndarray | None = None):
        from PIL import Image  # noqa: F401  (assert torchvision available too)
        npz = np.load(npz_path, allow_pickle=True)
        if indices is None:
            indices = np.arange(len(npz["action_idx"]))
        self.frame_paths   = npz["frame_paths"][indices]
        heading_sin = npz["heading_sin"][indices].copy()
        heading_cos = npz["heading_cos"][indices].copy()
        # Human-entered `corrected_heading` (via tick_viewer `H` key) wins
        # over the original heading.  Lets us keep ticks with bad
        # template-match heading but correct lat/lon motion in training,
        # using the motion-derived bearing as ground truth.
        corrected = _load_corrected_headings(self.frame_paths)
        n_corrected = int((~np.isnan(corrected)).sum())
        for i in np.where(~np.isnan(corrected))[0]:
            rad = float(np.deg2rad(corrected[i]))
            heading_sin[i] = np.sin(rad)
            heading_cos[i] = np.cos(rad)
        if n_corrected:
            print(f"  human heading corrections: {n_corrected} ticks")
        self.heading_sin   = heading_sin
        self.heading_cos   = heading_cos
        self.dlat_5tick    = npz["dlat_5tick"][indices]
        self.dlon_5tick    = npz["dlon_5tick"][indices]
        self.is_channel    = npz["is_channel"][indices]
        self.is_junction   = npz["is_junction"][indices]
        self.is_dead_end   = npz["is_dead_end"][indices]
        self.action_idx    = npz["action_idx"][indices]
        # ROI + valid mask are not used by __getitem__ but are stored
        # so the StratifiedROISampler can read them off the dataset.
        self.roi           = npz["roi"][indices] if "roi" in npz.files else None
        auto_valid         = (npz["valid"][indices] if "valid" in npz.files
                              else np.ones(len(indices), dtype=bool))
        # Per-session labels.jsonl may contain `training_include` overrides
        # entered via the review-mode tick_viewer.  Human verdict wins
        # over the auto valid rule; missing override → auto wins.
        overrides = _load_training_overrides(self.frame_paths)
        n_override_keep = int(((overrides == True) & ~auto_valid).sum())
        n_override_drop = int(((overrides == False) & auto_valid).sum())
        # Combine: human-set entries win; None entries fall through to auto.
        effective_valid = np.array([
            o if o is not None else bool(a)
            for o, a in zip(overrides, auto_valid)
        ], dtype=bool)
        self.valid = effective_valid
        self.auto_valid = auto_valid
        self.human_override = overrides
        if n_override_keep or n_override_drop:
            print(f"  human overrides: +{n_override_keep} include, "
                  f"-{n_override_drop} exclude")
        self.root = ROOT
        npz.close()

    def __len__(self):
        return len(self.action_idx)

    def __getitem__(self, idx):
        from PIL import Image
        img_path = self.root / str(self.frame_paths[idx])
        img = Image.open(img_path).convert("RGB").resize(
            (IMG_WIDTH, IMG_HEIGHT), Image.BILINEAR
        )
        arr = np.asarray(img, dtype=np.float32) / 255.0
        tensor = torch.from_numpy(arr).permute(2, 0, 1)   # H, W, C → C, H, W
        tensor = (tensor - IMAGENET_MEAN) / IMAGENET_STD

        aux = torch.tensor([
            self.heading_sin[idx], self.heading_cos[idx],
            self.dlat_5tick[idx], self.dlon_5tick[idx],
            self.is_channel[idx], self.is_junction[idx], self.is_dead_end[idx],
        ], dtype=torch.float32)

        return tensor, aux, int(self.action_idx[idx])


# Default per-batch ROI proportions.  Tunable via --roi-mix.  The
# rare ROIs each contribute ~10–12% while channel takes the rest;
# without stratification the natural distribution would be ~95%
# channel and the model would ignore the rare classes entirely.
DEFAULT_ROI_MIX = {
    "channel":       0.40,
    "cairo_outflow": 0.10,
    "nubia_village": 0.10,
    "the_bend":      0.10,
    "y_tip":         0.10,
    "barri_village": 0.10,
    "lake_end":      0.10,
}


class StratifiedROISampler(Sampler):
    """Yields indices so each consumed mini-batch hits the configured
    per-ROI proportions.

    Implementation: for each ROI, maintain an infinite shuffled cycle
    of its valid indices.  Per epoch, emit `target_proportion * total`
    indices from each ROI (rare ROIs get oversampled, channel is
    capped).  Random-shuffle the final list so the trainer's batch
    boundaries land on a representative mix.
    """

    def __init__(self, dataset: BCDataset, roi_mix: dict[str, float] | None = None,
                 epoch_size: int | None = None, only_valid: bool = True):
        self.dataset = dataset
        self.roi_mix = roi_mix or DEFAULT_ROI_MIX
        self.only_valid = only_valid
        if dataset.roi is None:
            raise ValueError("Dataset has no `roi` column — rebuild with v2 schema")
        # Indices per ROI (optionally filtered to valid).
        self.indices_by_roi: dict[str, np.ndarray] = {}
        for r in sorted(set(dataset.roi.tolist())):
            mask = (dataset.roi == r)
            if only_valid:
                mask &= dataset.valid.astype(bool)
            idx = np.where(mask)[0]
            if len(idx) > 0:
                self.indices_by_roi[r] = idx
        if not self.indices_by_roi:
            raise RuntimeError("No valid ticks in any ROI — check valid mask")
        # Epoch size defaults to the total valid count.
        if epoch_size is None:
            epoch_size = sum(len(v) for v in self.indices_by_roi.values())
        self.epoch_size = epoch_size
        # Normalize mix over the ROIs that actually have data.
        present = {k: v for k, v in self.roi_mix.items() if k in self.indices_by_roi}
        total = sum(present.values()) or 1.0
        self.normalized = {k: v / total for k, v in present.items()}

    def __len__(self):
        return self.epoch_size

    def __iter__(self):
        rng = np.random.default_rng()
        picks = []
        for roi, prop in self.normalized.items():
            n = max(1, int(round(self.epoch_size * prop)))
            pool = self.indices_by_roi[roi]
            # Sample with replacement so the rare ROIs can be hit n>len(pool) times.
            picks.append(rng.choice(pool, size=n, replace=True))
        merged = np.concatenate(picks)
        rng.shuffle(merged)
        yield from merged.tolist()


def split_indices(n: int, val_frac: float, seed: int = 42):
    """Random train/val split.

    Picks `val_frac` of all rows for validation.  Sessions can
    straddle the split — that's OK for BC sanity-checking; for IQL
    we may want stricter per-episode splitting.
    """
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n)
    cut = int(n * (1 - val_frac))
    return perm[:cut], perm[cut:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--num-workers", type=int, default=2)
    ap.add_argument("--device", default="auto",
                   choices=("auto", "cpu", "mps", "cuda"))
    ap.add_argument("--subset", type=int, default=None,
                   help="Use only the first N rows of the dataset "
                        "(for smoke tests).")
    ap.add_argument("--no-stratify", action="store_true",
                    help="Disable ROI-stratified sampling; sample uniformly.")
    ap.add_argument("--include-invalid", action="store_true",
                    help="Include ticks marked valid=False in training.")
    args = ap.parse_args()

    # Device selection
    if args.device == "auto":
        if torch.cuda.is_available():
            device = torch.device("cuda")
        elif torch.backends.mps.is_available():
            device = torch.device("mps")
        else:
            device = torch.device("cpu")
    else:
        device = torch.device(args.device)
    print(f"device: {device}")

    # Load split
    npz = np.load(args.dataset, allow_pickle=True)
    n = len(npz["action_idx"])
    if args.subset:
        n = min(n, args.subset)
    npz.close()
    train_idx, val_idx = split_indices(n, args.val_frac)
    print(f"dataset: {n} rows, train={len(train_idx)}, val={len(val_idx)}")

    train_ds = BCDataset(args.dataset, indices=train_idx)
    val_ds   = BCDataset(args.dataset, indices=val_idx)

    # ROI-stratified sampling for training (default).  Validation
    # stays sequential — we want consistent eval across epochs.
    if args.no_stratify or train_ds.roi is None:
        train_loader = DataLoader(
            train_ds, batch_size=args.batch_size, shuffle=True,
            num_workers=args.num_workers, pin_memory=False,
        )
        print("sampler: uniform (--no-stratify or no ROI column)")
    else:
        sampler = StratifiedROISampler(
            train_ds, roi_mix=DEFAULT_ROI_MIX,
            only_valid=not args.include_invalid,
        )
        n_valid = sum(len(v) for v in sampler.indices_by_roi.values())
        print(f"sampler: ROI-stratified ({n_valid} valid ticks, "
              f"epoch_size={sampler.epoch_size})")
        for roi, prop in sampler.normalized.items():
            n_avail = len(sampler.indices_by_roi[roi])
            print(f"  {roi:<18}: target={prop:.2%}  pool={n_avail}")
        train_loader = DataLoader(
            train_ds, batch_size=args.batch_size, sampler=sampler,
            num_workers=args.num_workers, pin_memory=False,
        )

    val_loader = DataLoader(
        val_ds, batch_size=args.batch_size, shuffle=False,
        num_workers=args.num_workers, pin_memory=False,
    )

    model = NavController().to(device)
    print(f"model: {count_params(model):,} params")
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    loss_fn = nn.CrossEntropyLoss()

    best_val_loss = float("inf")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    for epoch in range(args.epochs):
        model.train()
        t0 = time.time()
        train_loss = 0.0
        train_correct = 0
        train_total = 0
        for img, aux, action in train_loader:
            img = img.to(device, non_blocking=True)
            aux = aux.to(device, non_blocking=True)
            action = action.to(device, non_blocking=True)

            optimizer.zero_grad()
            logits, _ = model(img, aux)
            loss = loss_fn(logits, action)
            loss.backward()
            optimizer.step()

            train_loss += loss.item() * img.size(0)
            train_correct += (logits.argmax(1) == action).sum().item()
            train_total += img.size(0)

        train_loss /= train_total
        train_acc = train_correct / train_total

        # Validation
        model.eval()
        val_loss = 0.0
        val_correct = 0
        val_total = 0
        with torch.no_grad():
            for img, aux, action in val_loader:
                img = img.to(device); aux = aux.to(device)
                action = action.to(device)
                logits, _ = model(img, aux)
                loss = loss_fn(logits, action)
                val_loss += loss.item() * img.size(0)
                val_correct += (logits.argmax(1) == action).sum().item()
                val_total += img.size(0)
        val_loss /= val_total
        val_acc = val_correct / val_total

        elapsed = time.time() - t0
        print(f"epoch {epoch+1:>3}/{args.epochs} "
              f"[{elapsed:.1f}s]  "
              f"train: loss={train_loss:.4f} acc={train_acc:.3f}  "
              f"val: loss={val_loss:.4f} acc={val_acc:.3f}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save({
                "model_state": model.state_dict(),
                "input_source": "minimap",            # current dataset
                "input_size": (IMG_HEIGHT, IMG_WIDTH),
                "n_actions": N_ACTIONS,
                "n_aux": N_AUX,
                "epoch": epoch + 1,
                "val_loss": val_loss,
                "val_acc": val_acc,
            }, args.out)

    print(f"\n→ best checkpoint saved to {args.out}")
    print(f"  val_loss={best_val_loss:.4f}")


if __name__ == "__main__":
    main()
