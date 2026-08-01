"""Smoke test for the ai_nav scaffold.

Verifies:
  - the pipeline can be constructed with all defaults
  - ticking it on a real saved frame produces a NavState with the
    expected fields populated
  - VisionFrame's region accessors return non-empty images
  - the HeadingKalman smoother runs without crashing
  - layer swapping (NoOp → NoOp variant) works without pipeline changes

Does NOT validate algorithm correctness — that's per-layer's job.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from brain.ai_nav import AiNavPipeline, NavState, PipelineConfig
from brain.ai_nav.layers.heading import PCAHeading
from brain.ai_nav.layers.planner import ShoreHugPlanner
from brain.ai_nav.layers.segmentation import V11Segmenter
from brain.ai_nav.layers.strategic import NoOpStrategic
from brain.ai_nav.layers.tactical import NoOpTactical
from brain.ai_nav.pipeline import HeadingKalman
from brain.ai_nav.vision_input import FileVisionSource, StaticVisionSource, VisionFrame


REPO = Path(__file__).resolve().parent.parent
# Use a known-good saved minimap-bearing session frame.
SESSION = REPO / "data/sessions/live_centerline_2026-06-17T11-53-04"


def _have_session() -> bool:
    return (SESSION / "tick_0050.png").exists()


@pytest.fixture
def static_frame_source():
    if _have_session():
        # The saved tick_*.png files are already minimap crops, not
        # full screens.  Synthesize a full-screen-shaped frame so the
        # MINIMAP_CROP region accessor works.
        mm = Image.open(SESSION / "tick_0050.png").convert("RGB")
        full = Image.new("RGB", (2400, 1080), (0, 0, 0))
        # paste minimap into the correct region
        from brain.ai_nav.vision_input import MINIMAP_CROP
        full.paste(mm.resize((MINIMAP_CROP[2] - MINIMAP_CROP[0],
                              MINIMAP_CROP[3] - MINIMAP_CROP[1])),
                   (MINIMAP_CROP[0], MINIMAP_CROP[1]))
        return StaticVisionSource(full)
    # No saved session — use a synthetic all-blue frame.  Pipeline
    # should still construct and run without crashing even if
    # perception returns garbage.
    return StaticVisionSource(Image.new("RGB", (2400, 1080), (50, 100, 200)))


def test_vision_frame_regions_non_empty():
    img = Image.new("RGB", (2400, 1080), (10, 20, 30))
    frame = VisionFrame(raw=img, tick=0)
    assert frame.full_screen().size == (2400, 1080)
    assert frame.minimap().size[0] > 100   # 400x190 expected
    assert frame.ship_crop(radius=32).size == (64, 64)


def test_pipeline_constructs_with_defaults():
    src = StaticVisionSource(Image.new("RGB", (2400, 1080)))
    cfg = PipelineConfig()
    pipe = AiNavPipeline(source=src, config=cfg)
    assert pipe.config.heading.name == "pca_legacy"
    assert pipe.config.segmentation.name == "v11_brightness"
    assert pipe.config.planner.name == "shore_hug"
    assert pipe.config.tactical.name == "noop"
    assert pipe.config.strategic.name == "noop"


def test_pipeline_ticks_without_crashing(static_frame_source):
    pipe = AiNavPipeline(source=static_frame_source)
    state = NavState()
    for _ in range(3):
        state = pipe.tick(state)
        # Heading is always set (PCA returns *something*).
        assert state.heading is not None
        # Segmentation always produces a mask.
        assert state.water_mask is not None
        assert state.water_mask.dtype == np.bool_
        # Planner output is set (even when skip_reason).
        assert state.planner_output is not None
        # commit_direction was initialised on first tick.
        assert state.commit_direction is not None


def test_layer_swap_works_via_config():
    src = StaticVisionSource(Image.new("RGB", (2400, 1080)))

    class CustomNoOpTactical(NoOpTactical):
        name = "custom_noop"

    cfg = PipelineConfig(tactical=CustomNoOpTactical())
    pipe = AiNavPipeline(source=src, config=cfg)
    assert pipe.config.tactical.name == "custom_noop"
    # Other layers untouched.
    assert pipe.config.heading.name == "pca_legacy"


def test_heading_kalman_smooths_input():
    smoother = HeadingKalman()
    from brain.ai_nav.state import Heading
    # First measurement just initialises.
    out = smoother.update(Heading(100.0, confidence=0.8, source="t"))
    assert abs(out.bearing_deg - 100.0) < 0.5

    # Second measurement is 30° away with high confidence — gets pulled.
    out = smoother.update(Heading(130.0, confidence=0.8, source="t"))
    assert 100.0 < out.bearing_deg < 130.0

    # Low-confidence measurement gets damped.
    out = smoother.update(Heading(0.0, confidence=0.01, source="t"))
    assert out.bearing_deg > 100.0   # prior dominated


def test_state_snapshot_serializable():
    """NavState.snapshot() must return a JSON-serialisable dict."""
    import json
    state = NavState(tick=42)
    from brain.ai_nav.state import (
        CommitDirection, Heading, PlannerOutput,
    )
    state.heading = Heading(123.4, 0.7, "pca_legacy")
    state.commit_direction = CommitDirection(120.0, "init", 0)
    state.planner_output = PlannerOutput(
        shore_pts=[(1, 2), (3, 4)],
        path_pts=[(5, 6)],
        waypoint_px=(7, 8),
        command="hold_left",
        hold_ms=300,
    )
    snap = state.snapshot()
    assert snap["tick"] == 42
    assert snap["heading_deg"] == pytest.approx(123.4)
    # JSON round-trip — fails if anything non-serialisable slipped in.
    json.dumps(snap)
