"""Every LLM consult, recorded verbatim: what we asked and what came back.

WE COULD NOT SEE WHAT WE WERE ASKING. On 2026-09-08 Qwen reported the fleet "sailing in the
Atlantic Ocean" twenty times about a bot standing in a market, and finding out why meant
rebuilding the prompt by hand offline. The run log carried `prompt=14516 chars` and a
truncated answer — enough to know a consult happened, not enough to know what it was told.
An LLM's input is the evidence; a report that omits it can only show the symptom.

Same sink shape as `capture.adb_capture` and `vision.omniparser`: producers call `record`
unconditionally and it costs nothing until `action_trace.start()` registers a sink. Nothing
here knows about sessions, files or the viewer.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

_sink: Optional[Callable[[Mapping[str, Any]], None]] = None


def set_sink(fn: Optional[Callable[[Mapping[str, Any]], None]]) -> None:
    """Register the recorder, or `None` to stop recording."""
    global _sink
    _sink = fn


def record(model: str, question: str, prompt: str, response: Any,
           *, elapsed_s: float = 0.0, **extra: Any) -> None:
    """Hand one consult to the sink. Never raises — a trace must not break a run."""
    if _sink is None:
        return
    try:
        _sink({"model": model, "question": question, "prompt": prompt,
               "response": response, "elapsed_s": round(float(elapsed_s or 0.0), 2),
               **extra})
    except Exception:                    # noqa: BLE001 — recording is never load-bearing
        pass
