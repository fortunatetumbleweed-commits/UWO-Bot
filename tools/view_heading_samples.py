"""Visual inspector for heading training samples.

Two modes:

  --mode source     Show source sprites (1171 extracted) with their
                    labeled heading drawn as an arrow.  Use to verify
                    the extraction + label pipeline.

  --mode augmented  Show the EXACT training inputs the model sees —
                    rotated crops with the augmented heading label.
                    Use to verify the online-rotation augmentation
                    in tools/train_heading_cnn.py.

  --mode ticks      Show specific raw minimap frames by session + tick
                    list.  Use to inspect real-world hard cases
                    (occlusion, NPC sprites overlapping the ship).

Output: a 6×6 grid PNG at data/heading_samples_review/<mode>.png with
the heading arrow + label text overlaid on each cell.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SPRITES_ROOT = REPO / "data" / "heading_sprites"
OUT_ROOT = REPO / "data" / "heading_samples_review"


def _draw_heading_arrow(img: Image.Image, cx: int, cy: int,
                        heading_deg: float, length: int = 25,
                        color: tuple = (255, 0, 0)):
    """Draw an arrow from (cx, cy) in the heading direction.

    Heading: 0=N (up), 90=E (right), CW positive — matches the bot's
    convention.
    """
    draw = ImageDraw.Draw(img)
    th = math.radians(heading_deg)
    # 0° = up = (-y), 90° = right = (+x)
    dx = length * math.sin(th)
    dy = -length * math.cos(th)
    x2, y2 = int(cx + dx), int(cy + dy)
    draw.line([(cx, cy), (x2, y2)], fill=color, width=2)
    # Arrowhead
    head_angle = math.radians(25)
    hl = 6
    for sign in (-1, 1):
        ah = th + sign * head_angle + math.pi  # back-pointing wing
        ax = x2 + hl * math.sin(ah)
        ay = y2 - hl * math.cos(ah)
        draw.line([(x2, y2), (int(ax), int(ay))], fill=color, width=2)


def _grid(samples: list[dict], out_path: Path,
          cell_w: int = 80, cell_h: int | None = None,
          rows: int = 6, cols: int = 6, pad: int = 8,
          label_lines: int = 1, line_height: int = 14):
    """Render a grid of (image, heading_deg, caption).

    Each sample: {"img": PIL.Image, "heading_deg": float, "caption": str
                  or list[str] (one element per line)}.

    cell_w / cell_h preserve aspect when set — pass cell_h=None to keep
    the cell square at cell_w.
    """
    if cell_h is None:
        cell_h = cell_w
    label_height = label_lines * line_height + 4
    cw = cell_w + pad
    ch = cell_h + label_height + pad
    out = Image.new("RGB", (cols * cw + pad, rows * ch + pad),
                    (24, 24, 24))
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 10)
    except (OSError, IOError):
        font = ImageFont.load_default()

    for i, s in enumerate(samples[: rows * cols]):
        r, c = i // cols, i % cols
        x = pad + c * cw
        y = pad + r * ch
        # Paste image — preserve aspect by fitting to cell_w × cell_h
        img = s["img"].copy()
        if img.size != (cell_w, cell_h):
            img = img.resize((cell_w, cell_h))
        arrow_len = min(cell_w, cell_h) // 4
        # Production / live heading: red arrow
        if s.get("heading_deg") is not None:
            _draw_heading_arrow(img, cell_w // 2, cell_h // 2,
                                s["heading_deg"], length=arrow_len,
                                color=(255, 64, 64))
        # CNN heading: cyan arrow, slightly offset origin so the two
        # don't overlap when the answers match
        if s.get("heading_deg_cnn") is not None:
            _draw_heading_arrow(img, cell_w // 2 + 2, cell_h // 2 + 2,
                                s["heading_deg_cnn"], length=arrow_len,
                                color=(0, 220, 255))
        # PCA heading: magenta arrow, further offset for clarity when
        # all three agree
        if s.get("heading_deg_pca") is not None:
            _draw_heading_arrow(img, cell_w // 2 - 2, cell_h // 2 + 4,
                                s["heading_deg_pca"], length=arrow_len,
                                color=(255, 0, 220))
        out.paste(img, (x, y))
        # Caption underneath — list of strings → one line each
        cap = s["caption"]
        if isinstance(cap, str):
            cap = [cap]
        for li, line in enumerate(cap[:label_lines]):
            draw.text((x + 2, y + cell_h + 2 + li * line_height),
                      line, fill=(220, 220, 220), font=font)

    # Legend in the top-right corner
    if any(s.get("heading_deg_cnn") is not None for s in samples):
        draw.text((out.width - 250, 4),
                  "red = production heading (what shipped)",
                  fill=(255, 80, 80), font=font)
        draw.text((out.width - 250, 4 + line_height),
                  "cyan = CNN raw prediction    magenta = PCA raw",
                  fill=(0, 220, 255), font=font)

    out.save(out_path)
    return out


def mode_source(n: int, seed: int) -> tuple[list[dict], Path]:
    manifest = [json.loads(l) for l in (SPRITES_ROOT / "manifest.jsonl").open()]
    random.Random(seed).shuffle(manifest)
    samples = []
    for rec in manifest[:n]:
        img = Image.open(SPRITES_ROOT / f"sprite_{rec['sprite_id']}.png").convert("RGB")
        samples.append({
            "img": img,
            "heading_deg": rec["heading_deg"],
            "caption": (f"id={rec['sprite_id']} h={rec['heading_deg']:.0f}° "
                        f"src={rec['source_session'].split('T')[0][-4:]} "
                        f"t{rec['source_tick']}"),
        })
    return samples, OUT_ROOT / "source.png"


def mode_augmented(n: int, seed: int) -> tuple[list[dict], Path]:
    from tools.train_heading_cnn import SpriteHeadingDataset
    manifest = [json.loads(l) for l in (SPRITES_ROOT / "manifest.jsonl").open()]
    random.seed(seed)
    ds = SpriteHeadingDataset(manifest, augment=True)
    samples = []
    for i in range(n):
        idx = random.randrange(len(ds))
        tensor, tgt = ds[idx]
        # Convert [3, H, W] float [0,1] back to PIL
        arr = (tensor.permute(1, 2, 0).numpy() * 255).clip(0, 255).astype("uint8")
        img = Image.fromarray(arr)
        rec = manifest[idx]
        samples.append({
            "img": img,
            "heading_deg": float(tgt.item()),
            "caption": (f"src_h={rec['heading_deg']:.0f}° "
                        f"→ tgt={tgt.item():.0f}°"),
        })
    return samples, OUT_ROOT / "augmented.png"


def _cnn_predict(rgb_full: np.ndarray, model,
                 reference_bg: np.ndarray | None = None
                 ) -> tuple[float, float]:
    """Returns (heading_deg, confidence) by running the CNN on the
    80×80 crop around the green-pixel centroid.  When a
    reference_bg is provided, applies the ship-only masking +
    composite preprocessing that matches the training distribution.
    """
    import torch
    from scipy.ndimage import label as cc_label, binary_dilation
    R, G, B = (rgb_full[..., c].astype(np.int16) for c in range(3))
    green = (G > 140) & (G - R > 30) & (G - B > 30)
    H, W, _ = rgb_full.shape
    if int(green.sum()) >= 10:
        lab, n = cc_label(green)
        sizes = np.bincount(lab.ravel())[1:]
        biggest = int(np.argmax(sizes)) + 1
        ys, xs = np.where(lab == biggest)
        cy, cx = float(ys.mean()), float(xs.mean())
    else:
        cy, cx = H / 2.0, W / 2.0
    half = 40
    cy = int(round(max(half, min(H - half, cy))))
    cx = int(round(max(half, min(W - half, cx))))
    crop = rgb_full[cy - half:cy + half, cx - half:cx + half]

    if reference_bg is not None:
        # Ship-only mask + composite (matches training)
        cR, cG, cB = (crop[..., c].astype(np.int16) for c in range(3))
        c_green = (cG > 140) & (cG - cR > 30) & (cG - cB > 30)
        c_yellow = ((cR > 180) & (cG > 150) & (cB < 130)
                    & (np.abs(cR - cG) < 40))
        ship_region = binary_dilation(c_green, iterations=2)
        ship_only = ship_region & ~c_yellow
        composite = reference_bg.copy()
        composite[ship_only] = crop[ship_only]
        crop = composite

    arr = crop.astype(np.float32) / 255.0
    tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
    with torch.no_grad():
        sin_cos, conf = model(tensor)
    from brain.ai_nav.learned.heading_cnn import decode_heading
    return float(decode_heading(sin_cos).item()), float(conf.item())


def mode_ticks(spec: str, cnn_ckpt: Path | None = None
               ) -> tuple[list[dict], Path]:
    """spec format: 'session_name:t1,t2,t3,...'  e.g.
       'ai_nav_2026-06-30T07-59-21:130,131,139'
    """
    sess, tick_csv = spec.split(":", 1)
    ticks = [int(x.strip()) for x in tick_csv.split(",")]
    sess_dir = REPO / "data" / "sessions" / sess
    if not sess_dir.exists():
        raise SystemExit(f"session not found: {sess_dir}")

    model = None
    if cnn_ckpt is not None and cnn_ckpt.exists():
        import torch
        from brain.ai_nav.learned.heading_cnn import HeadingCNN
        model = HeadingCNN().eval()
        ckpt = torch.load(cnn_ckpt, map_location="cpu", weights_only=False)
        model.load_state_dict(ckpt["model"])
    # Load a reference background for inference-time ship-only masking
    # (matches the training distribution).
    ref_bg_path = REPO / "data" / "heading_backgrounds" / "bg_000000.png"
    reference_bg = (np.asarray(Image.open(ref_bg_path).convert("RGB"))
                    if ref_bg_path.exists() else None)
    # The minimap frames are saved as `tick_NNNN.png` (400×190).
    samples = []
    # Pull the live heading and ai_nav state from trace.jsonl for caption.
    trace = {}
    tp = sess_dir / "trace.jsonl"
    if tp.exists():
        with tp.open() as f:
            for line in f:
                try: r = json.loads(line)
                except: continue
                if r.get("tick") in set(ticks):
                    trace[r["tick"]] = r
    # Also mine from our labels.jsonl if available — gives us both
    # detector outputs, not just live.
    labels = {}
    lp = REPO / "data" / "heading_labels" / sess / "labels.jsonl"
    if lp.exists():
        for line in lp.open():
            r = json.loads(line)
            if r.get("tick") in set(ticks):
                labels[r["tick"]] = r
    for t in ticks:
        fp = sess_dir / f"tick_{t:04d}.png"
        if not fp.exists():
            continue
        img = Image.open(fp).convert("RGB")
        tr = trace.get(t, {})
        lb = labels.get(t, {})

        live = tr.get("heading_deg")
        live_s = f"{live:.0f}°" if isinstance(live, (int, float)) else "?"
        live_src = (tr.get("heading_source") or "").replace(
            "template_match", "tpl")

        tpl_v = lb.get("hdg_template")
        pca_v = lb.get("hdg_pca")
        tpl_s = f"{tpl_v:.0f}°" if tpl_v is not None else "?"
        pca_s = f"{pca_v:.0f}°" if pca_v is not None else "?"
        agree = lb.get("agreement_class", "?")

        # Mark which raw detector the LIVE pipeline ended up with.
        # Live pipeline uses template-match as the base, so:
        #   - if live ≈ tpl → "live=tpl"   (raw template won)
        #   - if live ≈ tpl+180 → "live=tpl_anti"  (antipode flipped)
        #   - if live ≈ pca → "live=pca-side"  (informational; rare)
        marker = "live=?"
        if isinstance(live, (int, float)) and tpl_v is not None:
            d_tpl  = abs(((live - tpl_v + 540) % 360) - 180)
            d_anti = abs(((live - (tpl_v + 180) + 540) % 360) - 180)
            d_pca  = (abs(((live - pca_v + 540) % 360) - 180)
                      if pca_v is not None else 999)
            best = min(d_tpl, d_anti, d_pca)
            if best == d_tpl:
                marker = "live=tpl"
            elif best == d_anti:
                marker = "live=tpl_anti"
            else:
                marker = "live=pca-side"

        # CNN inference (optional)
        cnn_hdg = cnn_conf = None
        if model is not None:
            cnn_hdg, cnn_conf = _cnn_predict(np.asarray(img), model,
                                             reference_bg=reference_bg)

        # Live PCA inference — same detector the pipeline uses now
        pca_live_hdg = None
        try:
            from brain.ai_nav.layers.heading import PCAHeading
            from brain.ai_nav.vision_input import VisionFrame
            from PIL import Image as _PIL
            # Fake VisionFrame that wraps the saved minimap
            class _F:
                def __init__(self, mm): self._mm = mm
                def minimap(self): return self._mm
                def full_screen(self): return self._mm
            pca_model = PCAHeading()
            p = pca_model.estimate(_F(img), prior=None)
            if p.confidence > 0:
                pca_live_hdg = p.bearing_deg
        except Exception:
            pass

        used_for_training = "YES" if agree == "gold" else "NO"
        line1 = f"t{t}  prod={live_s}  cnn=" + (
            f"{cnn_hdg:.0f}°(c{cnn_conf:.2f})" if cnn_hdg is not None
            else "off")
        if pca_live_hdg is not None:
            line1 += f"  pca={pca_live_hdg:.0f}°"
        line2 = (f"tpl={tpl_s}  pca={pca_s}  mb=" +
                 (f"{lb['motion_bearing']:.0f}°"
                  if lb.get("motion_bearing") is not None else "?"))
        line3 = (f"mining={agree}  train={used_for_training}  "
                 f"src={live_src}")

        h_draw_prod = float(live) if isinstance(live, (int, float)) else None
        samples.append({
            "img": img,
            "heading_deg": h_draw_prod,
            "heading_deg_cnn": cnn_hdg,
            "heading_deg_pca": pca_live_hdg,
            "caption": [line1, line2, line3],
        })
    return samples, OUT_ROOT / "ticks.png"


def mode_cnn_crops(spec: str, cnn_ckpt: Path) -> tuple[list[dict], Path]:
    """Show the EXACT 80×80 crop the CNN receives for each tick,
    scaled up.  Use to verify the crop-centering logic isn't locking
    onto the wrong green blob (e.g. NPC ship).
    """
    sess, tick_csv = spec.split(":", 1)
    ticks = [int(x.strip()) for x in tick_csv.split(",")]
    sess_dir = REPO / "data" / "sessions" / sess
    if not sess_dir.exists():
        raise SystemExit(f"session not found: {sess_dir}")

    import torch
    from brain.ai_nav.learned.heading_cnn import HeadingCNN, decode_heading
    from scipy.ndimage import label as cc_label

    model = HeadingCNN().eval()
    ckpt = torch.load(cnn_ckpt, map_location="cpu", weights_only=False)
    model.load_state_dict(ckpt["model"])

    trace = {}
    tp = sess_dir / "trace.jsonl"
    if tp.exists():
        with tp.open() as f:
            for line in f:
                try: r = json.loads(line)
                except: continue
                if r.get("tick") in set(ticks):
                    trace[r["tick"]] = r
    labels = {}
    lp = REPO / "data" / "heading_labels" / sess / "labels.jsonl"
    if lp.exists():
        for line in lp.open():
            r = json.loads(line)
            if r.get("tick") in set(ticks):
                labels[r["tick"]] = r

    # Reference background for ship-only masking at inference.
    ref_bg_path = REPO / "data" / "heading_backgrounds" / "bg_000000.png"
    ref_bg = (np.asarray(Image.open(ref_bg_path).convert("RGB"))
              if ref_bg_path.exists() else None)

    samples = []
    for t in ticks:
        fp = sess_dir / f"tick_{t:04d}.png"
        if not fp.exists():
            continue
        rgb_full = np.asarray(Image.open(fp).convert("RGB"))
        H, W, _ = rgb_full.shape
        # Replicate the inference crop-centering logic.
        R, G, B = (rgb_full[..., c].astype(np.int16) for c in range(3))
        mask = (G > 140) & (G - R > 30) & (G - B > 30)
        n_blobs = 0
        biggest_size = 0
        if int(mask.sum()) >= 10:
            lab, n_blobs = cc_label(mask)
            sizes = np.bincount(lab.ravel())[1:]
            biggest = int(np.argmax(sizes)) + 1
            biggest_size = int(sizes[biggest - 1])
            ys, xs = np.where(lab == biggest)
            cy, cx = float(ys.mean()), float(xs.mean())
            crop_src = f"cc{n_blobs}/best={biggest_size}px"
        else:
            cy, cx = H / 2.0, W / 2.0
            crop_src = "no_green→center"
        half = 40
        cy_i = int(round(max(half, min(H - half, cy))))
        cx_i = int(round(max(half, min(W - half, cx))))
        crop_arr = rgb_full[cy_i - half:cy_i + half,
                            cx_i - half:cx_i + half]

        # Apply ship-only masking + composite (matches training)
        if ref_bg is not None:
            from scipy.ndimage import binary_dilation
            cR, cG, cB = (crop_arr[..., c].astype(np.int16) for c in range(3))
            c_green = (cG > 140) & (cG - cR > 30) & (cG - cB > 30)
            c_yellow = ((cR > 180) & (cG > 150) & (cB < 130)
                        & (np.abs(cR - cG) < 40))
            ship_region = binary_dilation(c_green, iterations=2)
            ship_only = ship_region & ~c_yellow
            composite = ref_bg.copy()
            composite[ship_only] = crop_arr[ship_only]
            crop_arr = composite

        # Run CNN
        arr = crop_arr.astype(np.float32) / 255.0
        tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
        with torch.no_grad():
            sin_cos, conf = model(tensor)
            cnn_hdg = float(decode_heading(sin_cos).item())
            cnn_conf = float(conf.item())

        tr = trace.get(t, {})
        lb = labels.get(t, {})
        live = tr.get("heading_deg")
        live_s = f"{live:.0f}°" if isinstance(live, (int, float)) else "?"
        mb = lb.get("motion_bearing")
        mb_s = f"{mb:.0f}°" if mb is not None else "?"

        crop_img = Image.fromarray(crop_arr)
        line1 = f"t{t}  prod={live_s}  cnn={cnn_hdg:.0f}°(c{cnn_conf:.2f})"
        line2 = f"mb={mb_s}  crop={crop_src}"
        line3 = f"crop_at=(y{cy_i},x{cx_i}) of {W}×{H}"

        samples.append({
            "img": crop_img,
            "heading_deg": float(live) if isinstance(live, (int, float))
                else None,
            "heading_deg_cnn": cnn_hdg,
            "caption": [line1, line2, line3],
        })
    return samples, OUT_ROOT / "cnn_crops.png"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode",
                    choices=("source", "augmented", "ticks", "cnn_crops"),
                    default="source")
    ap.add_argument("--n", type=int, default=36,
                    help="how many samples (capped at rows*cols)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--ticks-spec",
                    help="for --mode ticks: 'session_name:t1,t2,t3,...'")
    ap.add_argument("--cell", type=int, default=120,
                    help="grid cell size (for tick mode, render larger)")
    ap.add_argument("--cnn-ckpt", default="data/heading_cnn/best.pt",
                    help="path to CNN checkpoint (tick mode only)")
    args = ap.parse_args()

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    if args.mode == "source":
        samples, out = mode_source(args.n, args.seed)
    elif args.mode == "augmented":
        samples, out = mode_augmented(args.n, args.seed)
    elif args.mode == "ticks":
        if not args.ticks_spec:
            raise SystemExit("--mode ticks requires --ticks-spec")
        samples, out = mode_ticks(args.ticks_spec,
                                  cnn_ckpt=Path(args.cnn_ckpt))
    else:  # cnn_crops
        if not args.ticks_spec:
            raise SystemExit("--mode cnn_crops requires --ticks-spec")
        samples, out = mode_cnn_crops(args.ticks_spec,
                                      cnn_ckpt=Path(args.cnn_ckpt))

    n = len(samples)
    if args.mode == "ticks":
        # Preserve the minimap's 400×190 aspect ratio (≈2.1:1).
        cell_w = args.cell
        cell_h = int(cell_w * 190 / 400)
        cols = min(4, n)
        rows = (n + cols - 1) // cols
        _grid(samples, out, cell_w=cell_w, cell_h=cell_h,
              rows=rows, cols=cols, label_lines=3)
    elif args.mode == "cnn_crops":
        # CNN crops are square (80×80).  Scale up for visibility.
        cell_w = args.cell
        cell_h = args.cell
        cols = min(5, n)
        rows = (n + cols - 1) // cols
        _grid(samples, out, cell_w=cell_w, cell_h=cell_h,
              rows=rows, cols=cols, label_lines=3)
    else:
        cols = min(6, n)
        rows = (n + cols - 1) // cols
        _grid(samples, out, cell_w=args.cell, cell_h=args.cell,
              rows=rows, cols=cols, label_lines=1)
    print(f"{n} samples → {out}")


if __name__ == "__main__":
    main()
