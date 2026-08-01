# vision/state_extractor.py
#
# L3 — Structured state extraction from OCR tokens.
#
# Where the four-layer perception architecture (docs/four_layer_perception.md)
# fits this:
#   L1 — Fingerprint (which exact node am I at?)
#   L2 — Component summary (which kind of screen am I on?)
#   L3 — Structured state (what is the actual game state?)  ← THIS
#   L4 — Goal predicate (is the goal achieved?)             ← uses L3 output
#
# Empirical validation (tools/tree_simulator.py on 19 labelled frames):
#   - Extracted crew_capacities + min_crew on every relevant sub_menu/dialog frame.
#   - Detected dialog presence via `cancel` keyword on every confirmation dialog.
#   - Drove L4 `has_enough_crew` predicate with 100% accuracy across the test set.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── Pattern definitions ────────────────────────────────────────────────────

# 'N/M' pairs (e.g. '786/1,676') with sane denominators.  The denominator
# filter excludes giant currency numbers OCR sometimes glues together.
_PAIR_RE = re.compile(r"\b(\d{1,4}(?:,\d{3})*)\s*/\s*(\d{1,4}(?:,\d{3})*)\b")
_PAIR_MAX_DENOM = 100_000

# Anchored fleet-pair extraction — for goal predicates we need to find
# the fleet AGGREGATE pair specifically, not any "/M" pair on the screen.
# The screen has many such pairs:
#   - Fleet Crew Size 141 (+14)/1,676  ← THIS one is fleet aggregate
#   - + 14 /904 -                       ← quantity-selector stepper
#   - 171/337                           ← per-ship row
# A naive max-by-denominator pick chooses the wrong pair when the fleet
# token is garbled by OCR (which happens often — we've seen
# 'Fleet Crew Size (+9 5311/2,275' from misread parentheses).
#
# The anchor regex looks for "Fleet Crew Size" followed by:
#   <current> [optional (+inc) increment] / <max>
# Optional whitespace and the parenthesised increment are tolerated.
# Returns None when the anchor isn't found — the caller's predicate
# then abstains rather than guessing wrongly.
_FLEET_ANCHOR_RE = re.compile(
    r"Fleet\s*Crew\s*Size\s+(\d[\d,]*)\s*(?:\(\s*\+?\s*\d[\d,]*\s*\)\s*)?/\s*(\d[\d,]*)",
    re.IGNORECASE,
)

# 'X Ming' standby crew suffix.  Ming is EasyOCR's mis-read of the unit
# suffix the game uses; it's the most reliable anchor we've got.
_STANDBY_RE = re.compile(r"\b(\d[\d,]*)\s*Ming\b")

# 'Min Crew N' threshold.  Note: EasyOCR sometimes drops the space, so
# we tolerate any whitespace.
_MIN_CREW_RE = re.compile(r"Min\s*Crew\s*(\d[\d,]*)")

# Keywords that strongly suggest a confirmation/modal dialog is on screen.
# Each match is recorded so callers can disambiguate dialog types.
_DIALOG_CUES = (
    "cancel", "confirm", "are you sure", "would you like",
    "pay extra", "want to", "recruit?",
)

# Blocker phrases that label the current state as "cannot proceed for
# reason X."  Used by the planner to pick a resolution.
_BLOCKER_PHRASES = (
    "not enough crew", "not enough supply", "not enough food",
    "not enough water", "insufficient",
)


# ── Result type ────────────────────────────────────────────────────────────

@dataclass
class ScreenState:
    """L3 output: the structured state extracted from a screen's OCR.

    Fields are present-when-detected — absence is meaningful (means the
    relevant text wasn't visible / OCR'd, not that the value is 0).
    """
    # Pairs of (current, max) where 0 < max < 100_000.  Includes per-ship
    # rows, quantity-selector pairs, and possibly the fleet aggregate.
    # IMPORTANT: do NOT use max-by-denominator on this list to pick the
    # fleet aggregate — that fails when OCR garbles the fleet token.
    # Use `fleet_pair_anchored` instead.
    crew_capacities: list[tuple[int, int]] = field(default_factory=list)

    # Fleet aggregate specifically — extracted by anchoring on the
    # "Fleet Crew Size" text.  None when the anchor isn't found
    # (predicate should abstain).
    fleet_pair_anchored: Optional[tuple[int, int]] = None

    # Numeric values when found, otherwise None.
    min_crew:     Optional[int] = None
    standby_crew: Optional[int] = None

    # Cue lists — empty when no match (not None).
    dialog_text_cues: list[str] = field(default_factory=list)
    blocker_phrases:  list[str] = field(default_factory=list)

    @property
    def fleet_pair(self) -> Optional[tuple[int, int]]:
        """The fleet aggregate (current, max).

        Returns the anchored extraction when available.  Returns None
        when the anchor isn't found — callers should treat this as
        'fleet state unknown' (not 'fleet is zero').  The previous
        max-by-denominator behaviour gave wrong answers when the
        fleet token was garbled by OCR and a smaller pair (like the
        quantity-selector '14/904') was the only candidate — see the
        2026-05-12 14:51:29 stuck-state diagnosis."""
        return self.fleet_pair_anchored

    @property
    def fleet_current(self) -> Optional[int]:
        p = self.fleet_pair
        return p[0] if p else None

    @property
    def fleet_max(self) -> Optional[int]:
        p = self.fleet_pair
        return p[1] if p else None

    @property
    def has_dialog(self) -> bool:
        return bool(self.dialog_text_cues)

    @property
    def has_blocker(self) -> bool:
        return bool(self.blocker_phrases)

    def to_dict(self) -> dict:
        """Serialisable view — used by per-tap state diffing and logging."""
        return {
            "crew_capacities":  list(self.crew_capacities),
            "min_crew":         self.min_crew,
            "standby_crew":     self.standby_crew,
            "dialog_text_cues": list(self.dialog_text_cues),
            "blocker_phrases":  list(self.blocker_phrases),
        }


# ── Public API ─────────────────────────────────────────────────────────────

def extract_state(tokens) -> ScreenState:
    """Run L3 regex extraction over OCR *tokens* (list of `(text, conf,
    cx, cy)` 4-tuples from `_ocr_frame`).

    Returns a `ScreenState`.  Fields are populated only when the
    corresponding text is visible; absence is informative (the caller
    can tell "we couldn't tell" apart from "we know it's zero").
    """
    text = " | ".join(t[0] for t in tokens)
    return _extract_from_joined_text(text)


def _extract_from_joined_text(text: str) -> ScreenState:
    """Pure-string extractor — separated so tests can hit it without
    constructing OCR tuples."""
    state = ScreenState()

    # Crew capacities: 'N/M' pairs with sane denominators (any pair on
    # the screen — per-ship rows, quantity stepper, etc.).
    for a, b in _PAIR_RE.findall(text):
        try:
            ai = int(a.replace(",", ""))
            bi = int(b.replace(",", ""))
        except ValueError:
            continue
        if 0 < bi < _PAIR_MAX_DENOM:
            state.crew_capacities.append((ai, bi))

    # Fleet aggregate: extract specifically via the "Fleet Crew Size"
    # anchor.  Without this, max-by-denominator picks the wrong pair
    # when OCR garbles the fleet token (live bug 2026-05-12).
    m = _FLEET_ANCHOR_RE.search(text)
    if m:
        try:
            current = int(m.group(1).replace(",", ""))
            maxv    = int(m.group(2).replace(",", ""))
            if 0 < maxv < _PAIR_MAX_DENOM:
                state.fleet_pair_anchored = (current, maxv)
        except ValueError:
            pass

    # Standby crew
    m = _STANDBY_RE.search(text)
    if m:
        try:
            state.standby_crew = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Min crew threshold
    m = _MIN_CREW_RE.search(text)
    if m:
        try:
            state.min_crew = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Cues (case-insensitive substring match)
    text_low = text.lower()
    state.dialog_text_cues = [kw for kw in _DIALOG_CUES if kw in text_low]
    state.blocker_phrases  = [p  for p  in _BLOCKER_PHRASES if p  in text_low]

    return state


def state_diff(before: ScreenState, after: ScreenState) -> list[str]:
    """Return the list of field names whose value differs between two
    states.  Used by callers to detect "what changed?" after a tap.
    Empty list = state-equivalent (no progress / pure no-op)."""
    keys = ("crew_capacities", "min_crew", "standby_crew",
             "dialog_text_cues", "blocker_phrases")
    return [k for k in keys if getattr(before, k) != getattr(after, k)]
