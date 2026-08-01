"""Per-handler typed-vs-legacy dismissal counters.

Phase 2 migration measurement: each migrated `_dismiss_*` handler in
`brain.perceive` records whether it dispatched via the typed DialogModel
path or fell back to the legacy keyword-pattern search.  Counters
accumulate over the session; `format_summary()` produces a table that
the runtime logs at shutdown.

Reading the table after a run tells us:
  - typed_rate per handler — fraction of dismissals that used the
    structural detector (higher = more legacy entries safe to retire)
  - n_total — opportunity count; handlers with zero invocations were
    not exercised by the run

Usage from a migrated handler:

    from brain.dismissal_telemetry import record
    record("dismiss_tap_ok", "typed")   # or "legacy"

The handler may also record "noop" when neither path tapped anything
(e.g. tap_decline without a matching button); those don't count toward
typed_rate but show up in n_total so silent paths are visible.
"""
from __future__ import annotations

import atexit
import logging
from collections import Counter
from typing import Literal

logger = logging.getLogger(__name__)

Path = Literal["typed", "legacy", "noop"]


# Bucket key: (handler_name, path).  Counts are simple ints.
_counters: Counter = Counter()
_registered = False


def record(handler: str, path: Path) -> None:
    """Increment the (handler, path) counter."""
    global _registered
    _counters[(handler, path)] += 1
    if not _registered:
        atexit.register(_log_summary_at_exit)
        _registered = True


def snapshot() -> dict:
    """Return a {handler: {path: count}} dict copy for callers that
    want to inspect or persist mid-session counts."""
    out: dict = {}
    for (h, p), n in _counters.items():
        out.setdefault(h, {})[p] = n
    return out


def reset() -> None:
    """Clear counters.  Used by tests."""
    _counters.clear()


def format_summary() -> str:
    """Return a multi-line human-readable summary of typed-vs-legacy
    fire rates per handler.  Empty string when no records exist."""
    if not _counters:
        return ""
    by_handler: dict = {}
    for (h, p), n in _counters.items():
        by_handler.setdefault(h, Counter())[p] += n

    rows = []
    rows.append(f"{'handler':<28s} {'typed':>6s} {'legacy':>6s} {'noop':>6s} {'total':>6s} {'typed%':>7s}")
    rows.append("-" * 70)
    for handler in sorted(by_handler):
        c = by_handler[handler]
        typed  = c.get("typed",  0)
        legacy = c.get("legacy", 0)
        noop   = c.get("noop",   0)
        total  = typed + legacy + noop
        actionable = typed + legacy
        rate   = (100.0 * typed / actionable) if actionable else 0.0
        rows.append(
            f"{handler:<28s} {typed:>6d} {legacy:>6d} {noop:>6d} {total:>6d} {rate:>6.1f}%"
        )
    return "\n".join(rows)


def _log_summary_at_exit() -> None:
    summary = format_summary()
    if not summary:
        return
    logger.info(
        "\n[dismissal_telemetry] Phase-2 typed-vs-legacy dispatch summary:\n%s",
        summary,
    )
