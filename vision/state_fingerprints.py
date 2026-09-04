# vision/state_fingerprints.py
#
# Phase 6 L2 — registry of state fingerprints.
#
# A Fingerprint is a structured description of how to recognise a game
# state from an OmniParser DetectedElement list.  Fingerprints are
# pure data — the evaluator (evaluate_fingerprint) is generic and
# contains no game-specific knowledge.
#
# Each Fingerprint composes a set of RegionSignals.  A RegionSignal
# checks for a structural pattern in a normalised region:
#   - element_type counts ("≥12 button-or-icon elements in this region")
#   - label set match ("≥3 of these labels appear as substrings")
#   - element-type-specific filters (text vs button vs icon)
#
# The fingerprint's confidence_rule decides what HIGH / MEDIUM / LOW /
# UNKNOWN means based on which signals matched.
#
# Fingerprints are authored from data — see vision/fingerprint_survey.py
# which extracts cross-frame-stable signals from data/labels.jsonl.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from vision.omniparser import DetectedElement
from vision.screen_classifier import (
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_UNKNOWN,
    ScreenClassification,
)


# ── Region signal types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class RegionSignal:
    """A single structural test against an OmniParser element list.

    Each signal has a short identifier (used in the matched-signals
    output) and an evaluation result that's either True (signal fired)
    or False (didn't).  The evaluation is implemented by subclasses.

    Region bounds are normalised (cx_norm, cy_norm) so signals are
    resolution-agnostic.
    """
    name:       str
    region:     tuple[float, float, float, float]  # (l, t, r, b) in [0,1]


@dataclass(frozen=True)
class LabelSetSignal(RegionSignal):
    """Fires when ≥ min_matches of *labels* appear (case-insensitive
    substring) inside any element label whose centre falls in *region*.

    Used for things like 'main_menu has Auction/Rank/Guild/Friend in
    the bottom-right tile bar' — match if ≥3 of the labels appear.
    """
    labels:       frozenset[str]
    min_matches:  int = 1
    # Restrict to specific element_types ('button' / 'text' / 'icon');
    # empty = any.
    element_types: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ElementCountSignal(RegionSignal):
    """Fires when the count of elements with *element_types* whose
    centre falls in *region* is ≥ *min_count*.

    Used for things like 'main_menu has ≥12 button/icon elements
    clustered in the right side' — pure structural density.
    """
    min_count:    int = 1
    element_types: frozenset[str] = frozenset({"button", "icon"})


@dataclass(frozen=True)
class TextContainsSignal(RegionSignal):
    """Fires when concatenated lowercased element labels in *region*
    contain *all* of the substrings in *required*.  Useful for header
    matches like 'top-left contains Ducat AND Total Load Capacity'.
    """
    required:     tuple[str, ...]


@dataclass(frozen=True)
class VerticalListSignal(RegionSignal):
    """Fires when *labels* appear as a VERTICAL COLUMN inside *region*:
    elements clustered on similar `cx` (±x_tolerance), stacked at
    distinct `cy` values (no two within ±y_min_gap), with at least
    *min_matches* of the target labels present in a single column
    cluster.

    Captures structural layouts — building lists on the right panel,
    sub-menu left-strip lists, task lists, recruit-mate rosters,
    shipyard build options — not just "do these strings appear
    somewhere".  A learned fingerprint built from this signal won't
    over-fire when one of the target labels happens to appear in an
    unrelated banner, tooltip, or right-panel info text on another
    screen: the column structure also has to hold.

    Origin: 2026-05-14.  Pure LabelSetSignal-based learned
    fingerprints from Claude analyses were over-firing
    (learned_deposit_withdrawal_savings_acco matched any frame whose
    central region contained both 'savings acco' and 'insurance' as
    substrings, regardless of layout).  Replacing with a structural
    signal eliminates that class of false positive.
    """
    labels:           frozenset[str]
    min_matches:      int = 2
    x_tolerance:      int = 80      # px — labels in same column within this band
    y_min_gap:        int = 60      # px — stacked rows must be vertically distinct
    element_types:    frozenset[str] = frozenset()


# ── Fingerprint composition ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Fingerprint:
    """A state's structural fingerprint.

    *positive_signals* are tested against the element list.  Each
    signal that fires contributes to the matched set.

    *negative_signals* (optional) are tested too — if any fires, the
    fingerprint does NOT match this state.

    *confidence_rule* maps the count of positive signals that fired to
    a confidence level.  The default rule:
      - all positive signals fire → HIGH
      - >= half fire (rounded down)  → MEDIUM
      - any fire (≥ 1)               → LOW
      - none fire                    → UNKNOWN (no match)
    """
    state_id:           str
    description:        str = ""
    positive_signals:   tuple[RegionSignal, ...] = ()
    negative_signals:   tuple[RegionSignal, ...] = ()
    # If set, requires AT LEAST this many positive signals to fire to
    # produce ANY match (regardless of confidence).  Below this, returns
    # None.
    min_positive_to_match: int = 1


# ── Evaluator ────────────────────────────────────────────────────────────────


def _in_region(el: DetectedElement, region, frame_w, frame_h) -> bool:
    cx_n = el.cx / frame_w
    cy_n = el.cy / frame_h
    l, t, r, b = region
    return l <= cx_n <= r and t <= cy_n <= b


def _distinct_y_labels(
    hits: list[tuple[str, int, int]],
    y_min_gap: int,
) -> list[tuple[str, int, int]]:
    """From *hits* (each (label, cx, cy)), return the subset whose cy
    values are separated by ≥ y_min_gap — i.e. the rows that form a
    truly stacked list rather than several labels packed on one line.

    Greedy sweep top-to-bottom: keep a row if its cy is at least
    y_min_gap below the last accepted row's cy.  Among rows that
    share an accepted cy band, keep only the first.
    """
    if not hits:
        return []
    sorted_hits = sorted(hits, key=lambda h: h[2])
    kept: list[tuple[str, int, int]] = []
    for h in sorted_hits:
        if not kept or h[2] - kept[-1][2] >= y_min_gap:
            kept.append(h)
    return kept


def _evaluate_signal(
    signal: RegionSignal,
    elements: Iterable[DetectedElement],
    frame_w: int,
    frame_h: int,
) -> tuple[bool, str]:
    """Return (fired, debug_string).  debug_string is included in the
    output signals list of ScreenClassification when fired."""
    in_region = [
        e for e in elements
        if _in_region(e, signal.region, frame_w, frame_h)
    ]

    if isinstance(signal, LabelSetSignal):
        elems = (
            in_region if not signal.element_types
            else [e for e in in_region if e.element_type in signal.element_types]
        )
        # Sort spatially (top-to-bottom, left-to-right) before joining so
        # multi-word labels like "item shop" / "fortune teller" / "mercator
        # estate" reliably appear as a contiguous substring in the joined
        # text.  OmniParser does not guarantee detection order; without
        # this sort, a title bar showing "Item Shop" can yield text
        # "shop item" and only the bare "shop" substring matches.
        elems_sorted = sorted(elems, key=lambda e: (e.cy, e.cx))
        text = " ".join((e.label or "").lower().strip() for e in elems_sorted)
        # Sort matched labels by length descending so the most specific
        # (longest) label surfaces first in the detail string — i.e.
        # `building: item shop` not `building: shop`.
        matched = sorted(
            (lbl for lbl in signal.labels if lbl in text),
            key=lambda s: (-len(s), s),
        )
        fired = len(matched) >= signal.min_matches
        return fired, (
            f"{signal.name}=" + (
                f"matched({matched})" if fired
                else f"only_matched({matched})"
            )
        )

    if isinstance(signal, ElementCountSignal):
        elems = [e for e in in_region if e.element_type in signal.element_types]
        fired = len(elems) >= signal.min_count
        return fired, f"{signal.name}=count({len(elems)}/{signal.min_count})"

    if isinstance(signal, VerticalListSignal):
        elems = (
            in_region if not signal.element_types
            else [e for e in in_region if e.element_type in signal.element_types]
        )
        # For each target label, find every element whose lowercased
        # label contains it as a substring (preserve the element's cx,
        # cy for column clustering).
        hits: list[tuple[str, int, int]] = []  # (matched_label, cx, cy)
        seen_labels: set[str] = set()
        for el in elems:
            lab = (el.label or "").lower().strip()
            if not lab:
                continue
            for target in signal.labels:
                if target in lab:
                    hits.append((target, el.cx, el.cy))
                    seen_labels.add(target)
                    break  # one match per element is enough
        # Cluster hits by cx — pick the column with the most distinct
        # target labels.  Distinct y values within the cluster must be
        # separated by ≥ y_min_gap (a true stacked list, not several
        # labels packed on one row).
        hits.sort(key=lambda h: h[1])   # by cx
        best_cluster: list[tuple[str, int, int]] = []
        cluster: list[tuple[str, int, int]] = []
        for h in hits:
            if cluster and abs(h[1] - (sum(c[1] for c in cluster) / len(cluster))) > signal.x_tolerance:
                if len(_distinct_y_labels(cluster, signal.y_min_gap)) > \
                   len(_distinct_y_labels(best_cluster, signal.y_min_gap)):
                    best_cluster = cluster
                cluster = []
            cluster.append(h)
        if len(_distinct_y_labels(cluster, signal.y_min_gap)) > \
           len(_distinct_y_labels(best_cluster, signal.y_min_gap)):
            best_cluster = cluster

        column_matched = sorted({
            h[0] for h in _distinct_y_labels(best_cluster, signal.y_min_gap)
        })
        fired = len(column_matched) >= signal.min_matches
        return fired, (
            f"{signal.name}=" + (
                f"column({column_matched})" if fired
                else f"only_column({column_matched})"
            )
        )

    if isinstance(signal, TextContainsSignal):
        text = " ".join((e.label or "").lower().strip() for e in in_region)
        missing = [s for s in signal.required if s not in text]
        fired = not missing
        return fired, (
            f"{signal.name}=" + ("all_present" if fired else f"missing({missing})")
        )

    raise ValueError(f"Unknown signal type: {type(signal).__name__}")


def evaluate_fingerprint(
    fp: Fingerprint,
    elements: Iterable[DetectedElement],
    frame_w: int,
    frame_h: int,
) -> Optional[ScreenClassification]:
    """Evaluate *fp* against *elements*.  Returns a ScreenClassification
    if matched (positive >= min_positive_to_match AND no negative fires),
    else None.

    Confidence:
      - all positives fire AND >= 2 positives total → HIGH
      - >= ceil(N/2) positives fire                  → MEDIUM
      - >= 1 positive fires                          → LOW
    (where N = number of positive signals)
    """
    elements = list(elements)

    # Negative signals — any firing → no match
    for neg in fp.negative_signals:
        fired, _ = _evaluate_signal(neg, elements, frame_w, frame_h)
        if fired:
            return None

    # Positive signals
    n_positive = len(fp.positive_signals)
    matched_signals: List[str] = []
    n_fired = 0
    for sig in fp.positive_signals:
        fired, dbg = _evaluate_signal(sig, elements, frame_w, frame_h)
        if fired:
            n_fired += 1
            matched_signals.append(dbg)

    if n_fired < fp.min_positive_to_match:
        return None

    # Confidence ladder by fire ratio:
    #   HIGH   — ≥ 75% of positive signals fired (one optional miss tolerated)
    #   MEDIUM — ≥ 50% fired
    #   LOW    — at least one fired but below 50%
    fire_ratio = n_fired / n_positive if n_positive > 0 else 0
    if fire_ratio >= 0.75:
        confidence = CONFIDENCE_HIGH
    elif fire_ratio >= 0.5:
        confidence = CONFIDENCE_MEDIUM
    else:
        confidence = CONFIDENCE_LOW

    return ScreenClassification(
        state=fp.state_id,
        detail=_build_detail(fp.state_id, matched_signals, n_fired, n_positive),
        confidence=confidence,
        signals=matched_signals,
    )


def _build_detail(
    state_id: str,
    matched_signals: List[str],
    n_fired: int,
    n_positive: int,
) -> str:
    """
    Build the detail string for a fingerprint match.

    When a *_title signal (e.g. building_title, sub_menu_title) matched a
    specific label, surface that label so downstream code can branch on the
    actual building/sub-menu name.  Otherwise fall back to the generic
    "{state}: N/M signals matched" placeholder.

    Examples:
      ["building_title=matched(['harbor'])"]      → "building: harbor"
      ["sub_menu_title=matched(['recruit crew'])"] → "sub_menu: recruit crew"
      []                                            → "building: 0/2 signals matched"
    """
    import re
    for sig_dbg in matched_signals:
        # Look for label-set matches that name a specific UI title.
        if "_title=matched(" not in sig_dbg:
            continue
        m = re.search(r"matched\(\[(.*?)\]\)", sig_dbg)
        if not m:
            continue
        # Crude but adequate parse — labels are quoted strings; first one wins.
        first_label = re.findall(r"'([^']*)'|\"([^\"]*)\"", m.group(1))
        if first_label:
            label = first_label[0][0] or first_label[0][1]
            if label:
                return f"{state_id}: {label}"
    return f"{state_id}: {n_fired}/{n_positive} signals matched"


# ── Registry ─────────────────────────────────────────────────────────────────
#
# Populated by vision/state_fingerprints_data.py (Phase 6 L3).  The
# registry is keyed by state_id; each entry is a Fingerprint.

FINGERPRINT_REGISTRY: dict[str, Fingerprint] = {}


def register_fingerprint(fp: Fingerprint) -> None:
    """Register a fingerprint.  Raises if state_id already registered
    (catch duplicate state_id at import time)."""
    if fp.state_id in FINGERPRINT_REGISTRY:
        raise ValueError(
            f"Fingerprint for state {fp.state_id!r} already registered"
        )
    FINGERPRINT_REGISTRY[fp.state_id] = fp


# ── Learned fingerprints — discovered via the Claude scene-analyser ────────
#
# When the bot encounters an unknown screen and Claude analyses it, a
# candidate Fingerprint is persisted to
# memory/knowledge/learned_fingerprints/<key>.json.  The candidate is
# generated from Claude's description + OmniParser elements (see
# vision/claude_vision._maybe_save_learned_candidate).
#
# load_learned_fingerprints() reads those files at startup and registers
# each as a regular Fingerprint, so the bot uses learned screens on the
# very next encounter without any manual code change.

import json as _json
from pathlib import Path as _Path

_LEARNED_FP_DIR_DEFAULT = _Path("memory/knowledge/learned_fingerprints")


def _signal_from_dict(d: dict) -> RegionSignal:
    """Reconstruct a RegionSignal from its on-disk dict form."""
    kind = d["kind"]
    region = tuple(d["region"])
    name = d["name"]
    if kind == "LabelSetSignal":
        return LabelSetSignal(
            name=name, region=region,
            labels=frozenset(d["labels"]),
            min_matches=d.get("min_matches", 1),
            element_types=frozenset(d.get("element_types", ())),
        )
    if kind == "ElementCountSignal":
        return ElementCountSignal(
            name=name, region=region,
            min_count=d.get("min_count", 1),
            element_types=frozenset(d.get("element_types", ("button", "icon"))),
        )
    if kind == "TextContainsSignal":
        return TextContainsSignal(
            name=name, region=region,
            required=tuple(d["required"]),
        )
    if kind == "VerticalListSignal":
        return VerticalListSignal(
            name=name, region=region,
            labels=frozenset(d["labels"]),
            min_matches=d.get("min_matches", 2),
            x_tolerance=d.get("x_tolerance", 80),
            y_min_gap=d.get("y_min_gap", 60),
            element_types=frozenset(d.get("element_types", ())),
        )
    raise ValueError(f"Unknown signal kind: {kind!r}")


def load_learned_fingerprints(
    learned_dir: _Path = _LEARNED_FP_DIR_DEFAULT,
) -> list[Fingerprint]:
    """Load every learned-fingerprint candidate from disk and register
    it.  Called at registry initialisation to absorb discoveries from
    prior runs.

    Each on-disk record must include a `fingerprint` key containing the
    state_id, signals, and min_positive_to_match.  Records lacking a
    `fingerprint` key (e.g. raw discovery records that haven't been
    promoted to candidate fingerprints yet) are skipped.

    Already-registered state_ids (collisions with hand-authored
    foundational fingerprints) are skipped — the foundation wins.
    Returns the list of fingerprints actually loaded.
    """
    loaded: list[Fingerprint] = []
    if not learned_dir.exists():
        return loaded

    for path in sorted(learned_dir.glob("*.json")):
        try:
            rec = _json.loads(path.read_text())
        except Exception:
            continue
        fp_data = rec.get("fingerprint")
        if not fp_data:
            continue
        state_id = fp_data.get("state_id")
        if not state_id or state_id in FINGERPRINT_REGISTRY:
            continue  # collision with foundation; skip
        try:
            positive = tuple(_signal_from_dict(s) for s in fp_data.get("positive_signals", []))
            negative = tuple(_signal_from_dict(s) for s in fp_data.get("negative_signals", []))
            fp = Fingerprint(
                state_id=state_id,
                description=fp_data.get(
                    "description",
                    f"Learned from Claude on {rec.get('first_seen_at', '?')}",
                ),
                positive_signals=positive,
                negative_signals=negative,
                min_positive_to_match=fp_data.get("min_positive_to_match", 1),
            )
        except Exception:
            continue
        FINGERPRINT_REGISTRY[state_id] = fp
        loaded.append(fp)
    return loaded


def classify_via_registry(
    elements: Iterable[DetectedElement],
    frame_w: int,
    frame_h: int,
) -> Optional[ScreenClassification]:
    """Iterate every registered fingerprint, return the highest-
    confidence match.  Returns None if no fingerprint matches.

    Tie-break: HIGH > MEDIUM > LOW; within a confidence level, the
    fingerprint with more matched signals wins; within that, the
    state_id whose name is alphabetically first."""
    elements = list(elements)
    candidates: list[ScreenClassification] = []
    for fp in FINGERPRINT_REGISTRY.values():
        cls = evaluate_fingerprint(fp, elements, frame_w, frame_h)
        if cls is not None:
            candidates.append(cls)

    if not candidates:
        return None

    confidence_rank = {
        CONFIDENCE_HIGH: 3, CONFIDENCE_MEDIUM: 2,
        CONFIDENCE_LOW: 1, CONFIDENCE_UNKNOWN: 0,
    }
    # Sort priority (lowest tuple wins):
    #   1. higher confidence
    #   2. standard fingerprints beat learned_* ones — standard are
    #      deliberately authored against the FSM state graph, learned
    #      ones are heuristic Claude-derived candidates and can fire on
    #      shadows of well-known states (e.g. `learned_repair_supply`
    #      matches the harbor main panel because its observed column
    #      Repair+Supply is visible across all harbor sub-tabs, which
    #      sail_to's FSM has no path from).  Origin: 2026-05-22 harbor
    #      stuck loop — bot couldn't depart because learned_repair_supply
    #      was winning over `building: harbor` on the FLEET_CHECK frame.
    #   3. more signals matched
    #   4. alphabetical state_id (stable tiebreak)
    # A LEARNED FINGERPRINT NEVER OUTRANKS A STANDARD ONE, whatever its confidence.
    #
    # Confidence used to be compared FIRST, and confidence is a fire RATIO — so a learned
    # fingerprint with a single signal fired 1/1 = 100% and was graded HIGH, the top band, on
    # the thinnest evidence there is. It then beat a standard fingerprint that had matched
    # less completely. Live 2026-08-26 the screen came back as
    # 'learned_updates_august_10_mon_update_advance' — an announcement popup, a thing the FSM
    # has no node for — and `open_world_map` spent three attempts on a state it could do
    # nothing with, then gave up.
    #
    # Standard fingerprints are authored against the FSM state graph; learned ones are
    # heuristic Claude-derived candidates that fire on shadows of well-known states. The
    # earlier note below records the same lesson from 2026-05-22. Ordering them ahead of
    # confidence is what makes that preference actually hold (user, 2026-08-26: no single
    # signature should overrule all the others).
    candidates.sort(
        key=lambda c: (
            c.state.startswith("learned_"),
            -confidence_rank.get(c.confidence, 0),
            -len(c.signals),
            c.state,
        )
    )
    return candidates[0]
