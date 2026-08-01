"""
Standalone ship-heading estimator (region-parts model), extracted for embedding.

Self-contained: needs only `part_model_v1.pt` + `canonical.npz` sitting next to this
file, and numpy / pillow / torch. No training code, no dataset folder.

    from ship_parts import ShipHeading
    est = ShipHeading()                      # load once (warms the rotation cache)
    r = est(rgb80)                           # rgb80: uint8 (80,80,3), RGB
    r.heading, r.shift, r.parts_seen
    out = est.reconstruct(rgb80, r)          # de-occluded ship painted on the frame

Pipeline:  frame -> SegNet (per-pixel part labels) -> part_pose (rigid fit) -> heading.
"""
import os
from dataclasses import dataclass, field

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
W, CENTER = 80, (40.0, 40.0)
PART_NAMES = ["bg", "bow", "hull", "stern", "sail_l", "sail_r"]
NP = 5

# ---------------------------------------------------------------- network

def cbr(i, o):
    return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU())

class SegNet(nn.Module):
    """U-Net: 80x80 RGB -> 6 per-pixel class scores (bg + 5 parts)."""
    def __init__(self, nc=len(PART_NAMES)):
        super().__init__()
        self.e1 = nn.Sequential(cbr(3, 32), cbr(32, 32))
        self.e2 = nn.Sequential(cbr(32, 64), cbr(64, 64))
        self.e3 = nn.Sequential(cbr(64, 128), cbr(128, 128))
        self.pool = nn.MaxPool2d(2)
        self.d2 = nn.Sequential(cbr(128 + 64, 64), cbr(64, 64))
        self.d1 = nn.Sequential(cbr(64 + 32, 32), cbr(32, 32))
        self.head = nn.Conv2d(32, nc, 1)

    def forward(self, x):
        e1 = self.e1(x); e2 = self.e2(self.pool(e1)); e3 = self.e3(self.pool(e2))
        d = F.interpolate(e3, scale_factor=2, mode="bilinear", align_corners=False)
        d = self.d2(torch.cat([d, e2], 1))
        d = F.interpolate(d, scale_factor=2, mode="bilinear", align_corners=False)
        d = self.d1(torch.cat([d, e1], 1))
        return self.head(d)

def to_tensor(imgs_u8):
    """(N,H,W,3) uint8 RGB -> (N,3,H,W) float32 normalized as in training."""
    x = torch.from_numpy(imgs_u8.astype(np.float32) / 255.0 - 0.5)
    return x.permute(0, 3, 1, 2).contiguous()

# ---------------------------------------------------------------- masks

def ship_green(rgb):
    """Strict visible ship-green mask (bright, saturated green; rejects grass)."""
    a = np.asarray(rgb).astype(np.int32); r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return (g > 110) & (g - r > 60) & (g - b > 50)

def open_mask_from_crop(crop_rgb):
    """Split a crop into (occluder, open). 'open' = plainly visible water/terrain where a
    hull pixel WOULD have been seen -- the reconstructed ship must not cover it."""
    a = np.asarray(crop_rgb).astype(np.int32); r, g, b = a[..., 0], a[..., 1], a[..., 2]
    ship = (g > 110) & (g - r > 60) & (g - b > 50)
    yellow = (r > 195) & (g > 160) & (r - b > 90) & (g - b > 60)   # portrait yellow, not tan terrain
    white = (r > 200) & (g > 200) & (b > 200)                      # text / diamonds / house markers
    ui = Image.fromarray(((yellow | white) * 255).astype(np.uint8))
    ui = ui.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))    # close blobs
    inv = Image.eval(ui, lambda p: 255 - p)                                      # fill interior holes
    for c in [(0, 0), (ui.width - 1, 0), (0, ui.height - 1), (ui.width - 1, ui.height - 1)]:
        ImageDraw.floodfill(inv, c, 0)
    occ = (np.asarray(ui) > 127) | (np.asarray(inv) > 127)
    occ = np.asarray(Image.fromarray((occ * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))) > 127
    return occ, (~occ) & (~ship)

# ---------------------------------------------------------------- geometry / pose fit

def render_labels(L0, heading, shift=(0, 0)):
    """Project the canonical part-label map to (heading, shift). NEAREST keeps labels."""
    r = Image.fromarray(L0).rotate(-heading % 360, resample=Image.NEAREST, center=CENTER)
    return np.roll(np.roll(np.asarray(r), shift[0], 0), shift[1], 1)

def render_sprite(sprite, heading):
    """Rotate the canonical RGBA sprite to `heading` (0=up, clockwise)."""
    return np.asarray(Image.fromarray(sprite, "RGBA").rotate(
        -heading % 360, resample=Image.BICUBIC, center=CENTER))

class _Rot:
    """Per-heading FFTs of the canonical part masks and full footprint (built once)."""
    def __init__(self, L0, step):
        L0img = Image.fromarray(L0)
        foot = Image.fromarray((L0 > 0).astype(np.uint8) * 255)
        self.headings, self.masks, self.foot = list(range(0, 360, step)), [], []
        for h in self.headings:
            r = np.asarray(L0img.rotate(-h % 360, resample=Image.NEAREST, center=CENTER))
            self.masks.append([np.conj(np.fft.rfft2((r == p).astype(np.float32))) for p in range(1, 6)])
            f = np.asarray(foot.rotate(-h % 360, resample=Image.NEAREST, center=CENTER)) > 127
            self.foot.append(np.conj(np.fft.rfft2(f.astype(np.float32))))

def part_pose(P, rot, open_mask=None, ship_mask=None, lam=0.4, beta=1.0, smax=16):
    """Recover (heading, (dy,dx)) -- the rigid pose scoring highest on three terms:

      + part overlap      (recall on labels)  predicted part probs vs canonical part masks
      + beta * green      (recall on pixels)  footprint covering the ACTUAL visible ship-green
      - lam  * open water (precision)         footprint over plainly visible water/terrain

    Each term is an FFT cross-correlation, giving the pixel count at every shift at once.
    """
    Pf = [np.fft.rfft2(P[p]) for p in range(1, 6)]
    idxs = np.r_[0:smax + 1, W - smax:W]                       # small +/- shifts (circular)
    Of = np.fft.rfft2(open_mask.astype(np.float32)) if open_mask is not None else None
    Gf = np.fft.rfft2(ship_mask.astype(np.float32)) if ship_mask is not None else None
    best = (-1e9, 0, (0, 0))
    for k, h in enumerate(rot.headings):
        acc = np.zeros((W, W), np.float32)
        for j in range(NP):
            acc += np.fft.irfft2(Pf[j] * rot.masks[k][j], s=(W, W))
        if Gf is not None:
            acc += beta * np.maximum(np.fft.irfft2(Gf * rot.foot[k], s=(W, W)), 0)
        if Of is not None:
            acc -= lam * np.maximum(np.fft.irfft2(Of * rot.foot[k], s=(W, W)), 0)
        sub = acc[np.ix_(idxs, idxs)]; ij = np.unravel_index(int(sub.argmax()), sub.shape)
        dy, dx = int(idxs[ij[0]]), int(idxs[ij[1]])
        if sub[ij] > best[0]:
            best = (float(sub[ij]), h, (dy - W if dy > smax else dy, dx - W if dx > smax else dx))
    return best[1], best[2]

# ---------------------------------------------------------------- public API

@dataclass
class Result:
    heading: float                       # degrees, 0 = up/north, clockwise
    shift: tuple                         # (dy, dx) placement of the canonical ship
    labels: np.ndarray = field(repr=False)   # (80,80) uint8 predicted part labels
    probs: np.ndarray = field(repr=False)    # (6,80,80) float32 class probabilities
    parts_seen: list = field(default_factory=list)

class ShipHeading:
    """Estimate ship heading from an 80x80 game frame. Load once, call many times."""

    def __init__(self, model_path=None, canonical_path=None, device="cpu", step=3):
        self.device = device
        ck = torch.load(model_path or os.path.join(HERE, "part_model_v1.pt"),
                        map_location=device, weights_only=False)
        state, self.meta = (ck["state_dict"], ck.get("meta", {})) if "state_dict" in ck else (ck, {})
        self.net = SegNet().to(device); self.net.load_state_dict(state); self.net.eval()
        z = np.load(canonical_path or os.path.join(HERE, "canonical.npz"))
        self.L0, self.sprite = z["L0"], z["sprite"]
        self.rot = _Rot(self.L0, step)          # warm the rotation cache up front

    @torch.no_grad()
    def segment(self, rgb80):
        """-> (probs (6,80,80), labels (80,80))"""
        x = to_tensor(np.asarray(rgb80, dtype=np.uint8)[None]).to(self.device)
        P = torch.softmax(self.net(x), 1)[0].cpu().numpy()
        return P, P.argmax(0).astype(np.uint8)

    def estimate(self, rgb80, min_part_px=8):
        img = np.asarray(rgb80, dtype=np.uint8)
        if img.shape != (W, W, 3):
            raise ValueError(f"expected uint8 RGB {(W, W, 3)}, got {img.shape}")
        P, labels = self.segment(img)
        _, open_mask = open_mask_from_crop(img)
        heading, shift = part_pose(P, self.rot, open_mask=open_mask, ship_mask=ship_green(img))
        seen = [PART_NAMES[p] for p in range(1, 6) if (labels == p).sum() >= min_part_px]
        return Result(float(heading), shift, labels, P, seen)

    __call__ = estimate

    def reconstruct(self, rgb80, result=None):
        """Paint the whole canonical ship at the estimated pose -> (80,80,3) uint8."""
        img = np.asarray(rgb80, dtype=np.uint8)
        r = result or self.estimate(img)
        ship = render_sprite(self.sprite, r.heading)
        ship = np.roll(np.roll(ship, r.shift[0], 0), r.shift[1], 1)
        a = ship[..., 3:4] / 255.0
        return (img * (1 - a) + ship[..., :3] * a).astype(np.uint8)
