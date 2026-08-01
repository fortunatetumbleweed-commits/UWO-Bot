"""Tiny CNN for ship-heading regression.

Input  : 80×80 RGB minimap crop centered on the ship icon.
Output : (sin θ, cos θ) for heading + scalar confidence in [0, 1].

Decoded heading = atan2(sin_pred, cos_pred), wrapped to [0, 360).
The 2-output sin/cos representation handles the 0/360 seam cleanly
(see docs/layered_training_with_bounce_and_thrash_rewards.md §5.2).

The trunk is intentionally small (~25K params) — the input is tiny
and the function is geometrically simple.  Larger backbones will
just memorize sprites.
"""
from __future__ import annotations

import torch
import torch.nn as nn


class HeadingCNN(nn.Module):
    """80×80 RGB → (sin θ, cos θ) + confidence.

    No global-average-pool: heading is a *spatial* / orientation
    property, so collapsing to 1×1 before the head destroys the very
    signal we need (the model collapses to predicting the mean of the
    label distribution — output (0, 0) — and gets stuck at MSE=0.5).

    Instead: shallow conv stack → flatten → small FC → heads.  Keeps
    the bow's spatial location in the feature vector.

    v15 addition: pass `multi_head=True` to enable the shape-prior
    architecture — adds a mask-decoder head that outputs an 80×80 ship
    silhouette and a bow-end binary head.  See
    `docs/heading_shape_prior_design.md`.  When `multi_head=False`
    (default), the model is bit-identical to the v10-v14 line.
    """
    def __init__(self, multi_head: bool = False):
        super().__init__()
        # 80→40→20→10 spatial, 3→16→32→32 channels
        self.conv1 = nn.Conv2d(3, 16, 3, padding=1)
        self.conv2 = nn.Conv2d(16, 32, 3, padding=1)
        self.conv3 = nn.Conv2d(32, 32, 3, padding=1)
        self.pool = nn.MaxPool2d(2, 2)
        # Spatial-preserving trunk → FC.  3200 = 32 * 10 * 10.
        self.fc = nn.Linear(32 * 10 * 10, 64)
        self.head_angle = nn.Linear(64, 2)
        self.head_conf = nn.Linear(64, 1)

        # v15 multi-head extensions.
        self.multi_head = multi_head
        if multi_head:
            # Mask decoder: [B, 32, 10, 10] → [B, 1, 80, 80] via 3
            # transposed convs (upsample ×2 each stage).  Small
            # capacity (~16k params) — we're recovering a silhouette,
            # not photo-realistic pixels.
            self.dec1 = nn.ConvTranspose2d(32, 16, 4, stride=2, padding=1)
            self.dec2 = nn.ConvTranspose2d(16, 8, 4, stride=2, padding=1)
            self.dec3 = nn.ConvTranspose2d(8, 4, 4, stride=2, padding=1)
            self.mask_conv = nn.Conv2d(4, 1, kernel_size=1)
            # Bow-end binary head: single scalar per image.
            self.head_bow = nn.Linear(64, 1)

    def forward(self, x: torch.Tensor):
        """Returns (sin_cos, conf) or (sin_cos, conf, mask_logits, bow_logit).

        Single-head mode returns the same 2-tuple as before.
        Multi-head mode also returns:
            mask_logits : [B, 1, 80, 80]  — pre-sigmoid mask
            bow_logit   : [B, 1]          — pre-sigmoid bow-end prob
        """
        h = torch.relu(self.conv1(x))
        h = self.pool(h)
        h = torch.relu(self.conv2(h))
        h = self.pool(h)
        h = torch.relu(self.conv3(h))
        h3 = self.pool(h)                    # [B, 32, 10, 10]
        h_flat = h3.flatten(1)               # [B, 3200]
        h_fc = torch.relu(self.fc(h_flat))   # [B, 64]
        sin_cos = self.head_angle(h_fc)      # [B, 2]
        conf = torch.sigmoid(self.head_conf(h_fc))
        if not self.multi_head:
            return sin_cos, conf
        d = torch.relu(self.dec1(h3))        # [B, 16, 20, 20]
        d = torch.relu(self.dec2(d))         # [B, 8, 40, 40]
        d = torch.relu(self.dec3(d))         # [B, 4, 80, 80]
        mask_logits = self.mask_conv(d)      # [B, 1, 80, 80]
        bow_logit = self.head_bow(h_fc)      # [B, 1]
        return sin_cos, conf, mask_logits, bow_logit


def angular_loss(pred: torch.Tensor, target_deg: torch.Tensor) -> torch.Tensor:
    """MSE between predicted (sin, cos) and target (sin, cos).

    Stable at zero unlike `1 - cos(Δθ)` over a normalized prediction.
    The optimum forces the network to output unit-norm vectors with
    the right angle.
    """
    target_rad = torch.deg2rad(target_deg)
    target_sin_cos = torch.stack([
        torch.sin(target_rad),
        torch.cos(target_rad),
    ], dim=1)                                # [B, 2]
    return ((pred - target_sin_cos) ** 2).mean()


def decode_heading(pred: torch.Tensor) -> torch.Tensor:
    """Inverse of the encoding: [B, 2] (sin, cos) → [B] degrees in [0, 360).

    `atan2(sin, cos)` is scale-invariant, so the raw network output
    doesn't need explicit normalization for decoding.
    """
    return (torch.rad2deg(torch.atan2(pred[:, 0], pred[:, 1])) + 360.0) % 360.0


# ─── v15 multi-head loss helpers ─────────────────────────────────────

def _rotate_mask(mask: torch.Tensor, angle_deg: torch.Tensor) -> torch.Tensor:
    """Differentiable rotation of a [B, 1, H, W] mask by per-sample
    angles (in degrees).  Uses `torch.nn.functional.grid_sample`.

    Positive angle rotates counter-clockwise in image coords (with y
    pointing down).  Matches the convention that heading 0° points
    "up" (north) in the mini-map.
    """
    B, C, H, W = mask.shape
    theta = torch.deg2rad(angle_deg).to(mask.device)
    cos_t = torch.cos(theta)
    sin_t = torch.sin(theta)
    # Build [B, 2, 3] affine matrix — rotation only, no translation.
    zero = torch.zeros_like(cos_t)
    affine = torch.stack([
        torch.stack([cos_t, -sin_t, zero], dim=-1),
        torch.stack([sin_t, cos_t, zero], dim=-1),
    ], dim=-2)  # [B, 2, 3]
    grid = torch.nn.functional.affine_grid(
        affine, size=(B, C, H, W), align_corners=False)
    return torch.nn.functional.grid_sample(
        mask, grid, mode="bilinear", padding_mode="zeros",
        align_corners=False)


def symmetry_loss(mask_prob: torch.Tensor,
                  heading_deg: torch.Tensor) -> torch.Tensor:
    """L_sym: predicted mask should be mirror-symmetric across the
    predicted heading axis.

    Rotate mask so heading points "up" (0°), then flip left-right,
    then MSE-compare to the un-flipped rotated mask.  Ship pixels
    that violate bilateral symmetry get penalized.
    """
    # Rotate mask by -heading so heading is at 0° (up)
    rotated = _rotate_mask(mask_prob, -heading_deg)
    mirrored = torch.flip(rotated, dims=[-1])   # left-right flip
    return ((rotated - mirrored) ** 2).mean()


def mask_pca_angle(mask_prob: torch.Tensor) -> torch.Tensor:
    """Differentiable PCA principal-axis angle for a [B, 1, H, W]
    weighted mask.  Returns angle in DEGREES in [0, 180).

    Uses closed-form 2×2 eigendecomposition — the direction of the
    larger eigenvalue.  Note: PCA is orientation-only (mod 180°), so
    the returned angle doesn't distinguish head from tail.
    """
    B, _, H, W = mask_prob.shape
    device = mask_prob.device
    ys, xs = torch.meshgrid(
        torch.arange(H, device=device, dtype=torch.float32),
        torch.arange(W, device=device, dtype=torch.float32),
        indexing="ij")
    w = mask_prob.squeeze(1)                    # [B, H, W]
    w_sum = w.sum(dim=(1, 2)).clamp(min=1e-6)
    cx = (w * xs).sum(dim=(1, 2)) / w_sum
    cy = (w * ys).sum(dim=(1, 2)) / w_sum
    dx = xs.unsqueeze(0) - cx.view(-1, 1, 1)
    dy = ys.unsqueeze(0) - cy.view(-1, 1, 1)
    sxx = (w * dx * dx).sum(dim=(1, 2)) / w_sum
    syy = (w * dy * dy).sum(dim=(1, 2)) / w_sum
    sxy = (w * dx * dy).sum(dim=(1, 2)) / w_sum
    # Principal-axis angle for 2×2 [[sxx, sxy], [sxy, syy]] — closed
    # form: theta = 0.5 * atan2(2*sxy, sxx - syy).  Result is the
    # angle of the eigenvector w.r.t. the x-axis in [−90°, 90°].
    # We convert to compass bearing (0° = up, clockwise) so it aligns
    # with the heading convention.
    theta = 0.5 * torch.atan2(2.0 * sxy, sxx - syy)
    # atan2(dx, -dy) is the compass-bearing convention used in
    # decode_heading; we want the same convention for alignment.
    # The eigenvector angle in image coords maps to compass bearing
    # via bearing = 90° - degrees(theta) mod 180°.
    bearing = (90.0 - torch.rad2deg(theta)) % 180.0
    return bearing


def pca_align_loss(mask_prob: torch.Tensor,
                   heading_deg: torch.Tensor) -> torch.Tensor:
    """L_pca_align: predicted heading should align with the principal
    axis of the predicted mask, modulo 180°.

    Uses `1 - cos(2·Δθ)` — smooth, differentiable, treats θ and θ+180°
    as equivalent (both give cos(2Δ) = cos(0) = 1).
    """
    axis_deg = mask_pca_angle(mask_prob)
    delta_rad = torch.deg2rad(heading_deg - axis_deg)
    return (1.0 - torch.cos(2.0 * delta_rad)).mean()
