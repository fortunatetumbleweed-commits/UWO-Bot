"""Train the ship-heading CNN.

Loads sprites from data/heading_sprites/ (built by
tools/extract_ship_sprites.py), applies online rotation augmentation
to get uniform angle coverage, and trains the tiny CNN defined in
brain/ai_nav/learned/heading_cnn.py.

Usage
─────
  python -m tools.train_heading_cnn                 # default 30 epochs
  python -m tools.train_heading_cnn --epochs 60 --batch 64
  python -m tools.train_heading_cnn --eval-only data/heading_cnn/best.pt
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.ai_nav.learned.heading_cnn import (HeadingCNN, angular_loss,
                                              decode_heading,
                                              symmetry_loss, pca_align_loss)
from brain.ai_nav.learned.heading_synthesis import synthesize


SPRITES_ROOT = REPO / "data" / "heading_sprites"
BACKGROUNDS_ROOT = REPO / "data" / "heading_backgrounds"
DEFAULT_OUT  = REPO / "data" / "heading_cnn"


def _load_backgrounds() -> list[np.ndarray]:
    bgs = []
    if not BACKGROUNDS_ROOT.exists():
        return bgs
    for p in sorted(BACKGROUNDS_ROOT.glob("bg_*.png")):
        bgs.append(np.asarray(Image.open(p).convert("RGB")))
    return bgs


def _device():
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


class SpriteHeadingDataset(Dataset):
    """One entry = (synthesized_crop, target_heading_deg, visibility).

    Three modes:
      - augment=True, no fixed_angle, no fixed_strategy → full
        rotation + random occlusion synthesis (training)
      - augment=False, fixed_angle set → deterministic rotation,
        no occlusion (clean eval)
      - augment=False, fixed_angle=0 → original crop, no rotation,
        no occlusion (sanity check)
    """
    def __init__(self, manifest_rows: list[dict],
                 backgrounds: list[np.ndarray],
                 augment: bool = True,
                 fixed_angle: float | None = None,
                 fixed_strategy: str | None = None,
                 fixed_bg_idx: int | None = None,
                 sprites_root: Path | None = None):
        self.rows = manifest_rows
        self.backgrounds = backgrounds
        self.augment = augment
        self.fixed_angle = fixed_angle
        self.fixed_strategy = fixed_strategy
        self.fixed_bg_idx = fixed_bg_idx
        # Passed explicitly so DataLoader multiprocessing workers pick
        # up the right path (globals aren't reliably inherited).
        self.sprites_root = sprites_root or SPRITES_ROOT

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int):
        rec = self.rows[idx]
        # Hard cases live in a separate dir (no "sprite_" prefix); load
        # from there when the row is marked hard_case=True.  Synthesis
        # is forced to strategy="none" so no synthetic occlusion piles
        # on top of the real pirate-boss / text overlay already in the
        # crop.
        is_hard = rec.get("hard_case", False)
        if is_hard:
            hard_root = (self.sprites_root.parent.parent
                         / "heading_hard_cases_labeled")
            sprite_path = hard_root / f"{rec['sprite_id']}.png"
        else:
            sprite_path = self.sprites_root / f"sprite_{rec['sprite_id']}.png"
        rgba = np.asarray(Image.open(sprite_path).convert("RGBA"))
        source_hdg = float(rec["heading_deg"])

        delta = (self.fixed_angle if self.fixed_angle is not None
                 else (random.uniform(0.0, 360.0) if self.augment
                       else 0.0))
        if is_hard:
            strategy = "none"
        else:
            strategy = (self.fixed_strategy if self.fixed_strategy is not None
                        else (None if self.augment else "none"))

        # Background: sampled at random during training (so each epoch
        # is a different ship+bg pairing).  Eval uses a fixed bg for
        # determinism.
        if not self.backgrounds:
            bg = np.full((rgba.shape[0], rgba.shape[1], 3), 30,
                         dtype=np.uint8)  # dark fallback
        elif self.fixed_bg_idx is not None:
            bg = self.backgrounds[self.fixed_bg_idx % len(self.backgrounds)]
        else:
            bg = self.backgrounds[random.randrange(len(self.backgrounds))]

        ex = synthesize(rgba, source_hdg, bg,
                        rotation_deg=delta, strategy=strategy)
        arr = np.asarray(ex.image).astype(np.float32) / 255.0
        tensor = torch.from_numpy(arr).permute(2, 0, 1)

        # Confidence target: visibility floored at 0.2 so the head
        # learns "some signal" even on heavy occlusion.
        conf_target = max(0.2, ex.visibility_frac)
        # v15 targets: 80×80 float mask + bow_end scalar.  If synthesis
        # didn't produce them (older SynthExample), fall back to zeros —
        # v14 training won't consume them anyway.
        if ex.ship_mask is not None:
            mask_t = torch.from_numpy(
                ex.ship_mask.astype(np.float32)).unsqueeze(0)
        else:
            mask_t = torch.zeros(1, 80, 80, dtype=torch.float32)
        bow_t = torch.tensor(
            float(ex.bow_end) if ex.bow_end is not None else 1.0,
            dtype=torch.float32)
        return (tensor,
                torch.tensor(ex.heading_deg, dtype=torch.float32),
                torch.tensor(conf_target, dtype=torch.float32),
                mask_t, bow_t)


def _ang_err(pred_deg: torch.Tensor, target_deg: torch.Tensor) -> torch.Tensor:
    """Smallest absolute angular distance in degrees."""
    d = (pred_deg - target_deg + 540.0) % 360.0 - 180.0
    return d.abs()


def split_train_eval(manifest: list[dict], seed: int = 0
                     ) -> tuple[list[dict], list[dict]]:
    """Hold out 10% by sprite_id hash — deterministic across runs."""
    rng = random.Random(seed)
    sids = sorted({r["sprite_id"] for r in manifest})
    rng.shuffle(sids)
    cut = max(1, len(sids) // 10)
    eval_sids = set(sids[:cut])
    train, ev = [], []
    for r in manifest:
        (ev if r["sprite_id"] in eval_sids else train).append(r)
    return train, ev


def compute_bucket_weights(rows: list[dict],
                           n_buckets: int = 12,
                           mode: str = "inverse",
                           ) -> list[float]:
    """Per-sample weight for WeightedRandomSampler.

    Two modes:
      - "inverse" (default, matches v6/v7): weight = 1 / count.  Fully
        equalizes samples per bucket, but at fine bucketing (72×5°) the
        1-sprite buckets get 300× the weight of dominant-bucket sprites,
        which over-samples specific-hull appearances and starves the
        dominant-direction diversity.
      - "sqrt": weight = 1 / sqrt(count).  Softer correction — rare
        buckets still get boosted but the extreme sprites don't dominate
        the epoch.  For 72×5°, extreme weight ratio drops from ~300× to
        ~17×.
    """
    import math as _m
    counts = [0] * n_buckets
    bucket_size = 360.0 / n_buckets
    for r in rows:
        b = int(r["heading_deg"] // bucket_size) % n_buckets
        counts[b] += 1
    weights = []
    for r in rows:
        b = int(r["heading_deg"] // bucket_size) % n_buckets
        c = counts[b]
        if c <= 0:
            base_w = 0.0
        elif mode == "sqrt":
            base_w = 1.0 / _m.sqrt(c)
        else:
            base_w = 1.0 / c
        # Hard cases (real production failures with hand labels) get a
        # 10× multiplier so the model sees each ~10 times per epoch —
        # dialed down from v13's 25× after evidence that aggressive
        # weighting regressed cases the model previously handled fine
        # (e.g. t100/t101 in voyage 2026-07-02T21-52-16).
        if r.get("hard_case"):
            base_w *= 10.0
        weights.append(base_w)
    return weights


def evaluate(model: HeadingCNN, eval_rows: list[dict], device,
             angles: list[float] | None = None,
             strategies: list[str] | None = None) -> dict:
    """Mean / P50 / P90 / P99 angular error across (sprite × angles ×
    occlusion-strategies).  Defaults to clean (no occlusion) only —
    that's the gold accuracy.  Pass `strategies=['none','front_half',
    'back_half','center_band']` to also report on partial-occlusion
    cases.
    """
    if angles is None:
        angles = list(range(0, 360, 30))
    if strategies is None:
        strategies = ["none"]
    model.eval()
    per_strat = {}
    with torch.no_grad():
        for strat in strategies:
            errs, confs = [], []
            for ang in angles:
                ds = SpriteHeadingDataset(eval_rows,
                                          backgrounds=model._eval_bgs,
                                          augment=False,
                                          fixed_angle=ang,
                                          fixed_strategy=strat,
                                          fixed_bg_idx=0,
                                          sprites_root=SPRITES_ROOT)
                loader = DataLoader(ds, batch_size=64, shuffle=False)
                for batch in loader:
                    batch_imgs = batch[0].to(device)
                    batch_tgt = batch[1].to(device)
                    outputs = model(batch_imgs)
                    pred, conf = outputs[0], outputs[1]
                    pred_deg = decode_heading(pred)
                    errs.extend(_ang_err(pred_deg, batch_tgt
                                         ).cpu().numpy().tolist())
                    confs.extend(conf.squeeze(1).cpu().numpy().tolist())
            errs_arr = np.array(errs)
            confs_arr = np.array(confs)
            per_strat[strat] = {
                "n": len(errs),
                "mean_err": float(errs_arr.mean()),
                "p50_err": float(np.percentile(errs_arr, 50)),
                "p90_err": float(np.percentile(errs_arr, 90)),
                "p99_err": float(np.percentile(errs_arr, 99)),
                "mean_conf": float(confs_arr.mean()),
                "pct_under_5deg":  float((errs_arr < 5).mean()),
                "pct_under_10deg": float((errs_arr < 10).mean()),
                "pct_under_30deg": float((errs_arr < 30).mean()),
            }
    # Flatten the primary strategy ("none") into top-level for
    # backwards-compat with the training loop's print line.
    out = dict(per_strat["none"])
    out["per_strategy"] = per_strat
    return out


def main():
    global SPRITES_ROOT, BACKGROUNDS_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--sprites-root", default=str(SPRITES_ROOT),
                    help="Directory containing sprite_*.png + manifest.jsonl. "
                         "Point at a versioned snapshot (e.g. "
                         "data/heading_training_snapshot_v5_2026-07-01/sprites) "
                         "instead of the live dir.")
    ap.add_argument("--backgrounds-root", default=str(BACKGROUNDS_ROOT),
                    help="Directory containing bg_*.png backgrounds.")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--eval-only", default=None,
                    help="Skip training; evaluate this .pt checkpoint")
    ap.add_argument("--no-balance", action="store_true",
                    help="Disable class-balanced sampling.  Default is "
                         "on: samples are drawn with weights inversely "
                         "proportional to their direction bucket, so "
                         "under-represented directions get their fair "
                         "share of gradient steps per epoch.")
    ap.add_argument("--n-buckets", type=int, default=12,
                    help="Number of direction buckets for class-balanced "
                         "sampling.  12 = 30° per bucket (v6 default); "
                         "72 = 5° per bucket (finer balance).  Ignored "
                         "when --no-balance is set.")
    ap.add_argument("--weight-mode", default="inverse",
                    choices=("inverse", "sqrt"),
                    help="Bucket-balancing weight function.  'inverse' "
                         "= weight ∝ 1/count (full equalization); "
                         "'sqrt' = weight ∝ 1/√count (softer, avoids "
                         "over-sampling single-sprite rare buckets).")
    # v15 flags
    ap.add_argument("--v15", action="store_true",
                    help="Train the v15 multi-head model (mask + heading "
                         "+ bow-end).  See docs/heading_shape_prior_design.md.")
    ap.add_argument("--w-seg", type=float, default=1.0,
                    help="v15 segmentation-loss weight (BCE on mask).")
    ap.add_argument("--w-sym", type=float, default=0.5,
                    help="v15 symmetry-loss weight.")
    ap.add_argument("--w-pca", type=float, default=0.3,
                    help="v15 PCA-alignment loss weight.")
    ap.add_argument("--w-bow", type=float, default=0.5,
                    help="v15 bow-end BCE loss weight.")
    ap.add_argument("--v15-warmup", type=int, default=0,
                    help="v15 curriculum: number of epochs to train "
                         "heading-only before enabling the aux losses "
                         "(seg / sym / pca / bow).  Gives heading a "
                         "foundation before the shape-prior tug-of-war "
                         "starts.  0 disables curriculum (all losses "
                         "active from epoch 0).")
    args = ap.parse_args()

    SPRITES_ROOT = Path(args.sprites_root)
    BACKGROUNDS_ROOT = Path(args.backgrounds_root)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = SPRITES_ROOT / "manifest.jsonl"
    manifest = [json.loads(l) for l in manifest_path.open()]
    print(f"loaded {len(manifest)} sprites")
    # Merge hand-labelled hard cases (real production frames with
    # pirate boss / text overlay + human-provided heading) if present.
    hard_manifest_path = (SPRITES_ROOT.parent.parent
                          / "heading_hard_cases_labeled" / "manifest.jsonl")
    if hard_manifest_path.exists():
        hard = [json.loads(l) for l in hard_manifest_path.open()]
        print(f"merging {len(hard)} hard-case sprites")
        manifest += hard
    # Drop UI-ribbon sprites that leaked through extraction.  Real ship
    # sprites are saturated dark/bright green (R<150, low B).  Pale mint
    # ribbons — region-notice banners — score high n_green (~1000) but
    # have R>180 AND B>180 (washed light-green), which no real ship has.
    kept = []
    dropped = 0
    for rec in manifest:
        # Hard cases live in a different dir and are always real
        # production frames — skip the pale-ribbon filter for them.
        if rec.get("hard_case"):
            kept.append(rec); continue
        arr = np.asarray(
            Image.open(SPRITES_ROOT / f"sprite_{rec['sprite_id']}.png"
                      ).convert("RGBA"))
        core = arr[..., 3] > 200
        if core.sum() < 5:
            kept.append(rec); continue
        r_mean = arr[core, 0].mean()
        b_mean = arr[core, 2].mean()
        if r_mean > 150 and b_mean > 150:
            dropped += 1
            continue
        kept.append(rec)
    print(f"filtered pale UI-ribbon sprites: dropped {dropped}, kept {len(kept)}")
    manifest = kept
    train_rows, eval_rows = split_train_eval(manifest, seed=args.seed)
    print(f"split: train {len(train_rows)}  eval {len(eval_rows)}")

    device = _device()
    print(f"device: {device}")

    backgrounds = _load_backgrounds()
    print(f"backgrounds: {len(backgrounds)}")

    model = HeadingCNN(multi_head=args.v15).to(device)
    if args.v15:
        print(f"v15 multi-head model  losses: seg={args.w_seg} sym={args.w_sym} pca={args.w_pca} bow={args.w_bow}")
    # Stash a fixed eval-background set on the model so `evaluate()`
    # is deterministic across epochs.
    model._eval_bgs = backgrounds[:1] if backgrounds else []
    n_params = sum(p.numel() for p in model.parameters())
    print(f"model params: {n_params:,}")

    if args.eval_only:
        ckpt = torch.load(args.eval_only, map_location=device)
        model.load_state_dict(ckpt["model"])
        stats = evaluate(model, eval_rows, device)
        print("\n=== EVAL ===")
        for k, v in stats.items():
            print(f"  {k}: {v:.3f}")
        return

    train_ds = SpriteHeadingDataset(train_rows, backgrounds=backgrounds,
                                    augment=True,
                                    sprites_root=SPRITES_ROOT)

    if args.no_balance:
        print("class-balanced sampling: OFF")
        train_loader = DataLoader(train_ds, batch_size=args.batch,
                                  shuffle=True, num_workers=2)
    else:
        n_buckets = args.n_buckets
        bucket_size = 360.0 / n_buckets
        weights = compute_bucket_weights(train_rows, n_buckets=n_buckets,
                                         mode=args.weight_mode)
        # Show what the balancing does to the effective distribution.
        buckets = [0] * n_buckets
        for r in train_rows:
            buckets[int(r["heading_deg"] // bucket_size) % n_buckets] += 1
        empty = sum(1 for c in buckets if c == 0)
        min_c = min(buckets)
        max_c = max(buckets)
        print(f"class-balanced sampling: ON (WeightedRandomSampler, "
              f"{n_buckets} buckets = {bucket_size:.1f}° each, "
              f"weight mode = {args.weight_mode})")
        print(f"  raw counts min/max : {min_c}/{max_c}  empty buckets: {empty}")
        print(f"  effective per epoch: each non-empty bucket ~equally sampled")
        sampler = WeightedRandomSampler(
            weights=weights,
            num_samples=len(train_ds),
            replacement=True,
        )
        train_loader = DataLoader(train_ds, batch_size=args.batch,
                                  sampler=sampler, num_workers=2)

    optim = torch.optim.AdamW(model.parameters(), lr=args.lr,
                              weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(optim, T_max=args.epochs)

    CONF_LOSS_WEIGHT = 0.5
    best_p90 = float("inf")
    history = []
    eval_strategies = ["none", "front_half", "back_half",
                       "center_band", "random_polygons"]
    # v15 loss weights — tuned from the design doc.
    W_SEG = args.w_seg
    W_SYM = args.w_sym
    W_PCA = args.w_pca
    W_BOW = args.w_bow

    for epoch in range(args.epochs):
        model.train()
        running = 0.0
        run_seg = run_sym = run_pca = run_bow = 0.0
        # Curriculum: hold aux losses off until warmup epochs pass, so
        # the heading head gets a strong foundation before shape-prior
        # losses tug the encoder in other directions.
        aux_active = args.v15 and (epoch >= args.v15_warmup)
        for batch in train_loader:
            batch_imgs = batch[0].to(device)
            batch_tgt = batch[1].to(device)
            batch_conf = batch[2].to(device)
            outputs = model(batch_imgs)
            pred, conf = outputs[0], outputs[1]
            loss_hdg = angular_loss(pred, batch_tgt)
            loss_conf = ((conf.squeeze(1) - batch_conf) ** 2).mean()
            loss = loss_hdg + CONF_LOSS_WEIGHT * loss_conf
            if aux_active:
                mask_logits, bow_logit = outputs[2], outputs[3]
                mask_gt = batch[3].to(device)
                bow_gt = batch[4].to(device)
                # Segmentation: BCE-with-logits (numerically stable).
                loss_seg = torch.nn.functional.binary_cross_entropy_with_logits(
                    mask_logits, mask_gt)
                # Symmetry + PCA-alignment use mask probability + the
                # *predicted* heading angle to co-supervise both heads.
                pred_deg = decode_heading(pred)
                mask_prob = torch.sigmoid(mask_logits)
                loss_sym = symmetry_loss(mask_prob, pred_deg)
                loss_pca = pca_align_loss(mask_prob, pred_deg)
                # Bow-end: BCE-with-logits against the derived label.
                loss_bow = torch.nn.functional.binary_cross_entropy_with_logits(
                    bow_logit.squeeze(1), bow_gt)
                loss = (loss + W_SEG * loss_seg + W_SYM * loss_sym
                        + W_PCA * loss_pca + W_BOW * loss_bow)
                run_seg += loss_seg.item() * batch_imgs.size(0)
                run_sym += loss_sym.item() * batch_imgs.size(0)
                run_pca += loss_pca.item() * batch_imgs.size(0)
                run_bow += loss_bow.item() * batch_imgs.size(0)
            optim.zero_grad()
            loss.backward()
            optim.step()
            running += loss.item() * batch_imgs.size(0)
        sched.step()
        train_loss = running / len(train_ds)
        if args.v15:
            n = len(train_ds)
            tag = "" if aux_active else "  (warmup — heading-only)"
            print(f"    v15 losses: seg={run_seg/n:.3f} "
                  f"sym={run_sym/n:.3f} pca={run_pca/n:.3f} "
                  f"bow={run_bow/n:.3f}{tag}")

        stats = evaluate(model, eval_rows, device,
                         strategies=eval_strategies)
        history.append({"epoch": epoch, "train_loss": train_loss, **stats})
        ps = stats["per_strategy"]
        print(f"ep{epoch:>3} loss={train_loss:.4f}  "
              f"clean:p90={ps['none']['p90_err']:>5.1f}°  "
              f"front:p90={ps['front_half']['p90_err']:>5.1f}°  "
              f"back:p90={ps['back_half']['p90_err']:>5.1f}°  "
              f"cband:p90={ps['center_band']['p90_err']:>5.1f}°  "
              f"poly:p90={ps['random_polygons']['p90_err']:>5.1f}°")

        if stats["p90_err"] < best_p90:
            best_p90 = stats["p90_err"]
            torch.save({"model": model.state_dict(),
                        "stats": stats,
                        "epoch": epoch}, out_dir / "best.pt")

    torch.save({"model": model.state_dict()}, out_dir / "last.pt")
    with (out_dir / "history.json").open("w") as f:
        json.dump(history, f, indent=2)
    print(f"\nbest checkpoint: {out_dir/'best.pt'}  (p90={best_p90:.2f}°)")


if __name__ == "__main__":
    main()
