"""Learned navigation controller — model architecture.

v2 reframe — see `docs/learned_navigation_controller_v2_reframe.md`.
Goal-free hugging policy: no desired direction in the aux vector.

Two pieces:
  - NavEncoder: tiny CNN (~250k params) that processes the input
    image and produces a 256-d feature vector.  Uses AdaptiveAvgPool
    at the end for resolution invariance.
  - NavController: full model.  Takes (image, auxiliary state) and
    outputs action logits (7 classes) + value scalar (for IQL; ignored
    during behavioral cloning).

Auxiliary state vector (7 floats — v2 schema):
  - heading_sin, heading_cos          (2)   ship bow direction
  - dlat_5tick, dlon_5tick            (2)   smoothed motion (immune
                                            to antipode flip; lat/lon
                                            is a different perception
                                            channel than heading)
  - is_channel, is_junction, is_dead_end (3)   topology one-hot
                                            (from JunctionDetector)

DROPPED in v2: desired_sin, desired_cos, delta_norm — the policy
no longer conditions on a goal direction.  Either direction along
a shore is a valid hugging action; bouncing is what's wrong.

Speed_kt is NOT in the auxiliary input yet — existing training
data has 0% speed coverage on pre-Phase-0 sessions.
"""
from __future__ import annotations

import torch
import torch.nn as nn

# Image canonical size for training + inference.  Original game is
# 2400×1080 (aspect 2.22:1).  We downscale to 384×192 (aspect 2:1)
# — slight squeeze, but dimensions are divisible by 32 which keeps
# MPS's AdaptiveAvgPool happy.  After 3 maxpools: 12×6.  Then pool
# to (4,4) via 6/2=3 and 12/4=3 — divisible.
IMG_HEIGHT = 192
IMG_WIDTH  = 384

# Action space — must match tools/build_rl_dataset.py.
N_ACTIONS = 7
HOLD, LEFT_SHORT, LEFT_MED, LEFT_LONG, RIGHT_SHORT, RIGHT_MED, RIGHT_LONG = range(7)

# Auxiliary input vector length (v2 schema — 7 floats).
N_AUX = 7


class NavEncoder(nn.Module):
    """Tiny CNN: image → 256-d feature vector.

    Uses standard Conv → ReLU → MaxPool blocks plus AdaptiveAvgPool
    at the end so the same model handles arbitrary input image
    sizes (resolution invariance — see learned_navigation_controller
    _plan.md §architecture).
    """

    def __init__(self, out_dim: int = 256):
        super().__init__()
        self.features = nn.Sequential(
            # in: 3 × H × W
            nn.Conv2d(3,   32, kernel_size=5, stride=2, padding=2),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(32,  64, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(128, 128, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d((4, 4)),     # → 128 × 4 × 4 always
        )
        self.proj = nn.Linear(128 * 4 * 4, out_dim)

    def forward(self, img: torch.Tensor) -> torch.Tensor:
        x = self.features(img)
        x = x.flatten(start_dim=1)
        return self.proj(x)


class NavController(nn.Module):
    """Full navigation controller: (image, aux) → (action_logits, value).

    During behavioral cloning we train action_logits only.  The value
    head is included so the same architecture works for IQL Phase 2.5
    without architectural changes.
    """

    def __init__(self, encoder_dim: int = 256, hidden: int = 128):
        super().__init__()
        self.encoder = NavEncoder(out_dim=encoder_dim)
        self.mlp = nn.Sequential(
            nn.Linear(encoder_dim + N_AUX, hidden * 2),
            nn.ReLU(inplace=True),
            nn.Linear(hidden * 2, hidden),
            nn.ReLU(inplace=True),
        )
        self.action_head = nn.Linear(hidden, N_ACTIONS)
        self.value_head  = nn.Linear(hidden, 1)

    def forward(self, img: torch.Tensor, aux: torch.Tensor):
        """img: (B, 3, H, W) float32 in [0, 1] (or normalized).
        aux: (B, N_AUX) float32.
        Returns: (action_logits, value)
          action_logits: (B, N_ACTIONS)
          value:         (B,)
        """
        feat = self.encoder(img)
        x = torch.cat([feat, aux], dim=1)
        x = self.mlp(x)
        return self.action_head(x), self.value_head(x).squeeze(-1)


def count_params(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())
