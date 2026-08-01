"""Bot version string + comparison helpers.

Bump `BOT_VERSION` (manually, via a commit) when shipping a change that
should **invalidate prior KB pessimism** — e.g. a perception fix that
might let the bot succeed where it previously gave up, an obstruction
classifier change, an action-selection upgrade.

KB records that store pessimistic flags (`tried_but_failed`, `disabled`,
`stale`) also persist the bot version at the time the flag was set.
On a new bot version, the flag is treated as expired and the bot
retries.  This avoids the situation where a code improvement is
shipped but the bot still skips work because old run state says
"don't bother".

Optimistic records (cached SceneInventories, learned fingerprints,
obstruction analyses) are NOT invalidated by version bumps — they
just track which version learned them.  Phase D (KB aging) will add
a freshness policy on top.

Origin: 2026-05-15.  After the Phase A1/A2/B2a fixes, several
buildings on London were still being skipped because the explore_port
flow had recorded `tried_but_failed` against them on an older,
buggier version of the code.
"""

from __future__ import annotations

from typing import Tuple


# ── Current version ──────────────────────────────────────────────────────
#
# Bump when shipping a change that may unstick previously-failed work.
# Format: "MAJOR.MINOR.PATCH" — tuple comparison via _parse_version.

BOT_VERSION: str = "0.7.0"


# ── Version comparison ──────────────────────────────────────────────────


def parse_version(v: str) -> Tuple[int, ...]:
    """Parse 'MAJOR.MINOR.PATCH' (or any dotted-int prefix) into a
    comparable tuple.  Unrecognised parts are dropped — non-numeric
    suffixes (e.g. '0.5.0-dev') compare as the numeric prefix.

    Returns (0,) for empty / unparseable input so it sorts BEFORE
    any real version.
    """
    if not v:
        return (0,)
    parts: list[int] = []
    for chunk in v.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def is_older_than_current(v: str) -> bool:
    """True if *v* parses to a version strictly older than BOT_VERSION.

    Used by KB readers to decide whether to honour a pessimistic flag
    (`tried_but_failed`) or treat it as expired and retry."""
    if not v:
        # Missing version stamp means "from before versioning was
        # added".  Treat as older so flags from those runs are
        # automatically expired on first version-aware run.
        return True
    return parse_version(v) < parse_version(BOT_VERSION)
