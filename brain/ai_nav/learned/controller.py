"""LearnedController — Phase 3 of the learned navigation controller plan.

Replaces L1+L2+L3 (heading + segmentation + planner) of the ai_nav
pipeline with a single learned model.  Loads a NavController
checkpoint, preprocesses each tick's image, runs forward pass, and
emits a PlannerOutput with the chosen action.

Checkpoint metadata (added by tools/train_bc.py) tells the
controller which image source to use (minimap vs full_screen) and
what input size the model was trained at.  Old checkpoints without
metadata default to "minimap" at (192, 384).

Usage in PipelineConfig (set via runner flag `--planner learned`):

  config = PipelineConfig(
      planner=LearnedController("models/nav_controller_bc_v1.pt"),
      ...
  )
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np
import torch
from PIL import Image

from brain.ai_nav.learned.model import (
    NavController, N_AUX, HOLD,
    LEFT_SHORT, LEFT_MED, LEFT_LONG,
    RIGHT_SHORT, RIGHT_MED, RIGHT_LONG,
)
from brain.ai_nav.state import CommitDirection, NavState, PlannerOutput
from brain.ai_nav.vision_input import VisionFrame

log = logging.getLogger(__name__)


# ImageNet normalization (same as training).
_IMAGENET_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
_IMAGENET_STD  = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)


# Inverse of the action mapping in tools/build_rl_dataset.py.
ACTION_DECODE: dict[int, tuple[Optional[str], int]] = {
    HOLD:         (None,         0),
    LEFT_SHORT:   ("hold_left",  200),
    LEFT_MED:     ("hold_left",  450),
    LEFT_LONG:    ("hold_left",  800),
    RIGHT_SHORT:  ("hold_right", 200),
    RIGHT_MED:    ("hold_right", 450),
    RIGHT_LONG:   ("hold_right", 800),
}


class LearnedController:
    """L3 replacement: runs a trained NavController model per tick.

    Loads the model + metadata lazily on the first call to plan().
    Subsequent ticks reuse the model.  Supports CPU, MPS, CUDA based
    on availability.

    Checkpoint metadata fields read (with defaults):
      - input_source: "minimap" (default) | "full_screen"
      - input_size:   (192, 384) (default; H, W)
    """
    name = "learned"
    latency_budget_ms = 50.0

    def __init__(self, checkpoint_path: str | Path,
                 device: str = "auto"):
        self.checkpoint_path = Path(checkpoint_path)
        if device == "auto":
            if torch.cuda.is_available():
                device = "cuda"
            elif torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        self.device = torch.device(device)
        self._model: Optional[NavController] = None
        self._input_source: str = "minimap"   # default until checkpoint loaded
        self._input_size: tuple[int, int] = (192, 384)

    def _ensure_model(self):
        if self._model is not None:
            return
        if not self.checkpoint_path.exists():
            raise FileNotFoundError(self.checkpoint_path)
        ckpt = torch.load(self.checkpoint_path, map_location=self.device,
                          weights_only=False)
        self._input_source = ckpt.get("input_source", "minimap")
        self._input_size   = tuple(ckpt.get("input_size", (192, 384)))
        log.info("[learned] loading %s (input=%s @ %s, device=%s)",
                 self.checkpoint_path.name, self._input_source,
                 self._input_size, self.device)
        m = NavController()
        m.load_state_dict(ckpt["model_state"])
        m.to(self.device)
        m.eval()
        self._model = m

    def _preprocess_image(self, frame: VisionFrame) -> torch.Tensor:
        img = (frame.full_screen() if self._input_source == "full_screen"
               else frame.minimap())
        h, w = self._input_size
        img = img.resize((w, h), Image.BILINEAR).convert("RGB")
        arr = np.asarray(img, dtype=np.float32) / 255.0
        t = torch.from_numpy(arr).permute(2, 0, 1)        # C, H, W
        t = (t - _IMAGENET_MEAN) / _IMAGENET_STD
        return t.unsqueeze(0).to(self.device)             # 1, C, H, W

    # Rolling lat/lon history for motion aux.  Matches MOTION_WINDOW
    # in `tools/build_rl_dataset.py` so training and inference see the
    # same window.
    _MOTION_WINDOW = 5

    def _build_aux(self, state: NavState) -> torch.Tensor:
        """Build the v2 aux vector: heading sin/cos + motion + topology.

        Goal-free (v2): no `desired`/`delta_norm`.  The planner above
        still emits commit_direction (used by mission-level termination
        checks), but the learned controller ignores it.
        """
        hdg = state.heading.bearing_deg if state.heading else 0.0

        # Rolling lat/lon history kept as an instance attribute.  At
        # cold-start (fewer than MOTION_WINDOW past readings) dlat/dlon
        # are zero — matches training (early-tick rows are zero too).
        if not hasattr(self, "_latlon_history"):
            self._latlon_history: list[tuple[float, float] | None] = []
        if state.lat is not None and state.lon is not None:
            self._latlon_history.append((state.lat, state.lon))
        else:
            self._latlon_history.append(None)
        # Cap memory.
        if len(self._latlon_history) > self._MOTION_WINDOW + 2:
            self._latlon_history = self._latlon_history[-(self._MOTION_WINDOW + 2):]

        dlat = dlon = 0.0
        if (len(self._latlon_history) > self._MOTION_WINDOW
                and self._latlon_history[-1] is not None
                and self._latlon_history[-1 - self._MOTION_WINDOW] is not None):
            cur = self._latlon_history[-1]
            ref = self._latlon_history[-1 - self._MOTION_WINDOW]
            dlat = float(cur[0] - ref[0])
            dlon = float(cur[1] - ref[1])

        topo = (state.topology or "").lower()
        is_channel  = 1.0 if topo == "channel"  else 0.0
        is_junction = 1.0 if topo == "junction" else 0.0
        is_dead_end = 1.0 if topo == "dead_end" else 0.0

        aux = torch.tensor([
            math.sin(math.radians(hdg)), math.cos(math.radians(hdg)),
            dlat, dlon,
            is_channel, is_junction, is_dead_end,
        ], dtype=torch.float32).unsqueeze(0).to(self.device)
        assert aux.shape == (1, N_AUX), f"aux shape {aux.shape} != (1, {N_AUX})"
        return aux

    def plan(self, frame: VisionFrame, state: NavState) -> NavState:
        # Seed commit_direction from heading on first tick if not set
        # (mirrors what ShoreHugPlanner / CenterlinePlanner do).
        if state.commit_direction is None and state.heading is not None:
            state.commit_direction = CommitDirection(
                bearing_deg=state.heading.bearing_deg,
                reason="init_from_heading_learned",
                set_at_tick=state.tick,
            )

        self._ensure_model()
        img = self._preprocess_image(frame)
        aux = self._build_aux(state)

        with torch.no_grad():
            logits, _value = self._model(img, aux)
            action_idx = int(logits.argmax(dim=1).item())

        cmd, hold_ms = ACTION_DECODE[action_idx]
        state.planner_output = PlannerOutput(
            command=cmd,
            hold_ms=hold_ms,
            primitive=f"learned[{action_idx}]",
        )
        return state
