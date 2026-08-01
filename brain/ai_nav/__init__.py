"""ai_nav — five-layer navigation stack.

See `docs/ai_nav_architecture.md` for the design rationale and
`brain/ai_nav/README.md` for the per-layer overview.
"""
from brain.ai_nav.actions import (
    ActionLayer, ActionResult, AdbActionLayer, NoOpActionLayer,
)
from brain.ai_nav.layers.tactical import (
    MoondreamTactical, NoOpTactical, TacticalLayer,
)
from brain.ai_nav.pipeline import AiNavPipeline, PipelineConfig
from brain.ai_nav.state import CommitDirection, Heading, NavState
from brain.ai_nav.vision_input import (
    AdbVisionSource, FileVisionSource, StaticVisionSource,
    VisionFrame, VisionSource,
)

__all__ = [
    # state
    "NavState", "Heading", "CommitDirection",
    # vision input
    "VisionFrame", "VisionSource",
    "AdbVisionSource", "FileVisionSource", "StaticVisionSource",
    # action output
    "ActionLayer", "ActionResult",
    "AdbActionLayer", "NoOpActionLayer",
    # tactical layer (L4)
    "TacticalLayer", "NoOpTactical", "MoondreamTactical",
    # pipeline
    "AiNavPipeline", "PipelineConfig",
]
