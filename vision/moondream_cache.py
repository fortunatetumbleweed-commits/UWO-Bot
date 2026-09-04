"""Per-frame, per-question cache for Moondream visual checks.

The three Moondream callers in perceive — daily_news_close_x,
confirm_in_town, confirm_at_sea — sometimes share a frame within one
perceive() tick.  Without caching each fires a separate ~2-3s Moondream
inference; with the cache, the second/third caller on the same frame
gets a free hit.

## Why not a single combined prompt?

Live-run analysis (May-04 log) showed that on most frames only ONE of
the three checks fires.  A unified `classify_scene(frame)` that always
asks all three questions would *worsen* the dominant case: daily_news
firing alone (after a pixel pre-check) would now force inference for
in_town and at_sea too — three questions where today only one fires.

Per-question caching wins in both cases:
  - One check on a frame: same cost as today.
  - Multiple checks on a frame: second and third are free.
  - Repeated tick on same frame (rare): all subsequent free.

If a future caller genuinely needs all three answers up-front, it can
opt in by calling each wrapper sequentially — the cache makes it
equivalent to a single combined call without the prompt-engineering
complexity of asking three yes/no questions in one Moondream prompt.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from vision.frame_cache import FrameCache

# Cache keyed by (id(frame), question_key).  Bounded to a small number
# of distinct frames so id() reuse after GC cannot leak across long
# pauses; frames within one perceive() share id stability so the cache
# is reliable for the dominant case (multiple callers, one frame).
_CACHE = FrameCache("moondream", max_entries=8)


def ask_cached(
    frame, question_key: str, ask_fn: Callable[[], Any],
) -> Any:
    """Memoize *ask_fn* under (id(frame), question_key).

    The first caller for a given (frame, question) pays the inference
    cost; subsequent callers get a cached hit.  Different questions on
    the same frame each cache independently.
    """
    # Keyed on the frame via `vision.frame_cache`, NOT on `id(frame)` alone: an id is unique
    # only while the object lives, and a recycled one hands this frame a previous frame's
    # verdict. Found in omniparser 2026-08-21 (197 collisions in 200 images) and again in
    # family_classifier 2026-08-26 (109 in 120) — the same defect, silent both times, because
    # every wrong answer is a plausible one.
    return _CACHE.memoize(frame, ask_fn, extra=question_key)


def clear_cache() -> None:
    """Clear the cache.  Used by tests."""
    _CACHE.clear()


def cache_size() -> int:
    """Number of cached (frame, question) entries.  Used by tests."""
    return len(_CACHE)
