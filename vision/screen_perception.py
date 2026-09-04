"""Unified whole-screen perception entry point.

Architectural principle (2026-05-13):

    For any *whole-screen* perceive, OmniParser is the primary source of
    truth.  EasyOCR is reserved for *intentional cropped regions* (port
    name in the top-left, screen-title in building views, market-tile
    labels, etc.).

This module wraps the OmniParser parse with semantic role tagging
(see `element_postprocess.py`) and exposes a single `ScreenInventory`
result that downstream consumers (Qwen perception, navigation, planning,
caching) read from.  No consumer should call `parse_fast_cached`
directly for whole-screen analysis — it should go through `parse_screen`
so the semantic tagging is applied consistently and the cache freshness
is guaranteed.

Cache freshness: this entry point clears the per-frame parse cache at
the start of each call, so the OmniParser parse always sees the *current*
frame.  Within a single parse_screen() call, sub-consumers can still
benefit from the per-tick cache because the same frame object is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from vision.element_postprocess import (
    TaggedElement, tag_elements, group_by_role,
    filter_non_noise, NOISE_ROLES,
)


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class ScreenInventory:
    """The result of parsing one frame.  Self-contained: callers can
    derive everything they need (raw OmniParser elements, role-grouped
    views, noise-filtered subsets) without re-parsing the frame."""

    raw_elements:    list                              # DetectedElement list
    tagged:          list[TaggedElement]              # role-annotated
    by_role:         dict[str, list[TaggedElement]]   # role → elements
    frame_dims:      tuple[int, int]
    nav_state:       Optional[str]
    # The frame itself, when the parse had one. Detectors that need PIXELS rather than boxes
    # — DialogModel segmenting a card off its title bar — cannot work from `frame_dims`, and
    # the image is alive for the whole tick anyway under per-frame perception sharing.
    frame:           object = None

    @property
    def non_noise(self) -> list[TaggedElement]:
        """Tagged elements minus NPC bubbles, phone-OS, event banners.
        Use this for Qwen scene description, fingerprint matching, etc."""
        return filter_non_noise(self.tagged)

    def text_content(self, exclude_noise: bool = True) -> str:
        """Concatenated label text from tagged elements, useful for
        keyword scans (interruptor detection, flow trigger matching)."""
        source = self.non_noise if exclude_noise else self.tagged
        return " | ".join(t.label for t in source if t.label and t.label != "icon")

    def has_role(self, role: str) -> bool:
        return role in self.by_role and len(self.by_role[role]) > 0

    def get(self, role: str) -> list[TaggedElement]:
        return self.by_role.get(role, [])

    def summary_line(self) -> str:
        """One-line summary for logging.  Lists role counts."""
        items = sorted(self.by_role.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        return "  ".join(f"{role}={len(els)}" for role, els in items)


# ── Public entry point ───────────────────────────────────────────────────────

def parse_screen(
    frame: Image.Image,
    nav_state: Optional[str] = None,
) -> ScreenInventory:
    """Parse *frame* end-to-end: OmniParser + semantic tagging.

    This is the unified entry point for whole-screen perception.

    Args:
        frame:     PIL.Image of the current screen.
        nav_state: optional navigation state ('port_overworld', etc.) so
                   role classifiers can be screen-type-aware.  Pass None
                   when the state hasn't been determined yet — classifiers
                   that gate on nav_state will behave more permissively.

    Returns:
        ScreenInventory.  When OmniParser is unavailable, the inventory
        is empty (raw_elements=[], tagged=[], by_role={}).
    """
    from vision.omniparser import (
        get_omniparser, parse_fast_cached, clear_parse_fast_cache,
    )
    from loguru import logger

    # Cache freshness: clear cross-tick stale entries.  Within this
    # parse_screen() call, sub-consumers that call parse_fast_cached on
    # the same frame still hit the cache.
    clear_parse_fast_cache()

    try:
        if not get_omniparser().yolo_available():
            logger.warning("[parse_screen] OmniParser YOLO unavailable")
            return _empty(frame, nav_state)
        raw = parse_fast_cached(frame)
    except Exception as e:
        logger.warning(f"[parse_screen] OmniParser parse failed: {e}")
        return _empty(frame, nav_state)

    tagged = tag_elements(raw, nav_state=nav_state,
                            frame_dims=(frame.width, frame.height))
    grouped = group_by_role(tagged)
    return ScreenInventory(
        raw_elements = list(raw),
        tagged       = tagged,
        by_role      = grouped,
        frame_dims   = (frame.width, frame.height),
        nav_state    = nav_state,
        frame        = frame,
    )


def _empty(frame, nav_state) -> ScreenInventory:
    return ScreenInventory(
        raw_elements = [],
        tagged       = [],
        by_role      = {},
        frame_dims   = (frame.width, frame.height),
        nav_state    = nav_state,
    )
