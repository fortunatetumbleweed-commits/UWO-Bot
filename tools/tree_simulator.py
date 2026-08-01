#!/usr/bin/env python
"""Empirical analysis for the tree-search foundation.

Loads the two labelled capture sessions:
  - 2026-05-12_09-15-51 (Port Royal, 15 frames — harbor + inn paths,
    cycle case, success overlay, etc.)
  - 2026-05-12_09-52-44 (Southside, 4 frames — normal-recruit branch,
    overworld arrival)

Runs OmniParser + OCR on each, computes screen_fingerprint under several
candidate recipes, prints equivalence classes, scores each recipe against
the expected clusters/distinctions documented from the live captures,
and simulates two tree walks (success + cycle).

Output is plain text designed to make design choices empirical, not
to be parsed by other code.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
from loguru import logger


# ── Frame inventory ──────────────────────────────────────────────────────────

SESSIONS_DIR = Path("data/sessions")
LABELS_FILE  = Path("data/labels.jsonl")

TARGET_SESSIONS = [
    "2026-05-12_09-15-51",  # Port Royal
    "2026-05-12_09-52-44",  # Southside
]


def _short_id(session_idx: int, file_name: str) -> str:
    """Stable short identifier: s1.04 = session 1 frame 0004."""
    n = int(file_name.split("_")[0])
    return f"s{session_idx + 1}.{n:02d}"


def load_frames() -> list[dict]:
    labels: dict[tuple[str, str], dict] = {}
    if LABELS_FILE.exists():
        for line in LABELS_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            sess = r.get("session_id")
            if sess in TARGET_SESSIONS:
                labels[(sess, r["file"])] = r

    frames = []
    for sess_idx, sess in enumerate(TARGET_SESSIONS):
        frames_dir = SESSIONS_DIR / sess / "frames"
        for png in sorted(frames_dir.glob("*.png")):
            label = labels.get((sess, png.name), {})
            frames.append({
                "session":     sess,
                "session_idx": sess_idx,
                "file":        png.name,
                "path":        png,
                "short_id":    _short_id(sess_idx, png.name),
                "screen_type": label.get("screen_type", "?"),
                "tags":        label.get("tags", []),
                "notes":       label.get("notes", ""),
            })
    return frames


# ── Perception on each frame ─────────────────────────────────────────────────

def run_perception(frame: dict) -> dict:
    """Add 'elements', 'tokens', 'frame_size' to *frame* by running
    OmniParser + OCR.  Cleared between frames so the per-frame OCR cache
    doesn't deliver another frame's tokens."""
    from vision.omniparser import parse_fast_cached
    from actions.sail_actions import _ocr_frame, clear_ocr_frame_cache
    clear_ocr_frame_cache()
    img = Image.open(frame["path"])
    frame["frame_size"] = img.size
    frame["elements"]   = parse_fast_cached(img)
    frame["tokens"]     = _ocr_frame(img, min_conf=0.30)
    return frame


# ── Fingerprint recipes ──────────────────────────────────────────────────────

def _strip_numeric(label: str) -> str:
    """'218,714 Recruit' → 'Recruit'; '0/325' → ''."""
    out = re.sub(r"^[\d,.\s/]+", "", label.lower())
    out = re.sub(r"[\d,.\s/]+$", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def fp_recipe_A(elements, frame_w=2400, frame_h=1080, position_bins=20) -> str:
    """A: button+icon, position binned, NO numeric stripping."""
    items = []
    for el in elements:
        if el.element_type not in ("button", "icon"):
            continue
        if not el.label:
            continue
        bx = int(round(el.cx / frame_w * position_bins))
        by = int(round(el.cy / frame_h * position_bins))
        items.append(f"{el.label.lower().strip()}@{bx},{by}")
    return hashlib.sha1("|".join(sorted(items)).encode()).hexdigest()[:12]


def fp_recipe_B(elements, frame_w=2400, frame_h=1080, position_bins=20) -> str:
    """B: A + strip numbers from labels (so prices/counts don't break clusters)."""
    items = []
    for el in elements:
        if el.element_type not in ("button", "icon"):
            continue
        if not el.label:
            continue
        bx = int(round(el.cx / frame_w * position_bins))
        by = int(round(el.cy / frame_h * position_bins))
        label = _strip_numeric(el.label)
        if not label:
            continue   # element was purely numeric — skip
        items.append(f"{label}@{bx},{by}")
    return hashlib.sha1("|".join(sorted(items)).encode()).hexdigest()[:12]


def fp_recipe_C(elements, frame_w=2400, frame_h=1080, position_bins=20,
                top_y_norm=0.10, bottom_y_norm=0.97) -> str:
    """C: B + drop top chrome bar (currency counters, etc.) and bottom debug bar."""
    items = []
    for el in elements:
        if el.element_type not in ("button", "icon"):
            continue
        if not el.label:
            continue
        cy_norm = el.cy / frame_h
        if cy_norm < top_y_norm or cy_norm > bottom_y_norm:
            continue
        bx = int(round(el.cx / frame_w * position_bins))
        by = int(round(cy_norm * position_bins))
        label = _strip_numeric(el.label)
        if not label:
            continue
        items.append(f"{label}@{bx},{by}")
    return hashlib.sha1("|".join(sorted(items)).encode()).hexdigest()[:12]


def fp_recipe_D(elements, frame_w=2400, frame_h=1080, position_bins=20,
                top_y_norm=0.10, bottom_y_norm=0.97) -> str:
    """D: C + include 'text' elements (so harbor's vs inn's left-menu
    items get into the hash; OmniParser sometimes labels menu items as
    text rather than button)."""
    items = []
    for el in elements:
        if el.element_type not in ("button", "icon", "text"):
            continue
        if not el.label or el.label.lower() == "icon":
            continue
        cy_norm = el.cy / frame_h
        if cy_norm < top_y_norm or cy_norm > bottom_y_norm:
            continue
        bx = int(round(el.cx / frame_w * position_bins))
        by = int(round(cy_norm * position_bins))
        label = _strip_numeric(el.label)
        if not label:
            continue
        items.append(f"{label}@{bx},{by}")
    return hashlib.sha1("|".join(sorted(items)).encode()).hexdigest()[:12]


def fp_recipe_E(elements, frame_w=2400, frame_h=1080, position_bins=20,
                top_y_norm=0.10, bottom_y_norm=0.97,
                conf_floor=0.50) -> str:
    """E: Recipe C + confidence floor.  YOLO detections below 0.5 are
    unstable across captures (the source of s1.04 ≠ s1.05 noise) — drop
    them so the fingerprint depends only on confidently-detected elements."""
    items = []
    for el in elements:
        if el.element_type not in ("button", "icon"):
            continue
        if not el.label:
            continue
        if el.confidence < conf_floor:
            continue
        cy_norm = el.cy / frame_h
        if cy_norm < top_y_norm or cy_norm > bottom_y_norm:
            continue
        bx = int(round(el.cx / frame_w * position_bins))
        by = int(round(cy_norm * position_bins))
        label = _strip_numeric(el.label)
        if not label:
            continue
        items.append(f"{label}@{bx},{by}")
    return hashlib.sha1("|".join(sorted(items)).encode()).hexdigest()[:12]


# Region masks (normalised) — what's reliably the "identity" of a screen:
#   - top-left title:    cx_norm < 0.30, cy_norm < 0.12
#   - left-menu column:  cx_norm < 0.20, 0.12 <= cy_norm < 0.50
#   - right-action area: cx_norm > 0.70, cy_norm > 0.30
# Everything else (centre ship list, background, decorations) is decoration
# that drifts between captures.
def _in_identity_region(cx_n: float, cy_n: float) -> bool:
    title = cx_n < 0.30 and cy_n < 0.12
    left  = cx_n < 0.22 and 0.10 <= cy_n < 0.50
    right = cx_n > 0.70 and cy_n > 0.30 and cy_n < 0.97
    return title or left or right


def fp_recipe_F(elements, frame_w=2400, frame_h=1080, position_bins=20,
                conf_floor=0.50) -> str:
    """F: Stable-signature only.  Hash only elements whose centre falls
    inside the top-left title region, the left-menu column, or the
    right-side action region.  Skip middle-of-screen content (ship rows,
    decoration, NPC speech bubbles) — those drift and contribute noise.
    Confidence floor as in E.  Includes 'text' since left-menu items
    often come back as text."""
    items = []
    for el in elements:
        if el.element_type not in ("button", "icon", "text"):
            continue
        if not el.label or el.label.lower() == "icon":
            continue
        if el.confidence < conf_floor:
            continue
        cx_n = el.cx / frame_w
        cy_n = el.cy / frame_h
        if not _in_identity_region(cx_n, cy_n):
            continue
        bx = int(round(cx_n * position_bins))
        by = int(round(cy_n * position_bins))
        label = _strip_numeric(el.label)
        if not label:
            continue
        items.append(f"{label}@{bx},{by}")
    return hashlib.sha1("|".join(sorted(items)).encode()).hexdigest()[:12]


RECIPES: dict[str, Callable] = {
    "A — button+icon, no strip":      fp_recipe_A,
    "B — A + strip numerics":         fp_recipe_B,
    "C — B + drop top/bottom chrome": fp_recipe_C,
    "D — C + include left-side text": fp_recipe_D,
    "E — C + confidence floor 0.5":   fp_recipe_E,
    "F — identity regions only":      fp_recipe_F,
}


# ── Layer 2: component summary (region/type counts) ─────────────────────────

def _region_3x3(cx_n: float, cy_n: float) -> str:
    """3×3 grid label (TL, TC, TR, ML, MC, MR, BL, BC, BR)."""
    col = "L" if cx_n < 0.33 else ("C" if cx_n < 0.67 else "R")
    row = "T" if cy_n < 0.33 else ("M" if cy_n < 0.67 else "B")
    return row + col


def L2_component_summary(elements, frame_w=2400, frame_h=1080) -> Counter:
    """Count (element_type, region) tuples across the frame.  This is the
    'structural shape' of the screen — robust to OCR drift on individual
    labels and to small confidence-jitter element flicker."""
    summary: Counter = Counter()
    for el in elements:
        # Treat unlabelled icons as one bucket; labelled icons (rare in
        # parse_fast) stay separate via element_type='button' fusion above.
        etype = el.element_type
        if etype == "icon" and (not el.label or el.label.lower() == "icon"):
            etype = "icon"
        r = _region_3x3(el.cx / frame_w, el.cy / frame_h)
        summary[(etype, r)] += 1
    return summary


def L2_similarity_jaccard_presence(sum_a: Counter, sum_b: Counter) -> float:
    """Binary set-overlap: how many (etype, region) cells are present in
    both frames vs in either?  Ignores counts — just 'does this cell
    have any elements of this type.'  Most forgiving."""
    keys_a = {k for k, v in sum_a.items() if v > 0}
    keys_b = {k for k, v in sum_b.items() if v > 0}
    union = keys_a | keys_b
    if not union:
        return 1.0
    return len(keys_a & keys_b) / len(union)


def L2_similarity_count_tolerance(sum_a: Counter, sum_b: Counter,
                                   tolerance: int = 1) -> float:
    """For each cell key in either frame, count it as 'matched' if the
    counts differ by ≤ tolerance.  Catches 'one icon flickered into
    adjacent cell' without losing real differences."""
    keys = set(sum_a) | set(sum_b)
    if not keys:
        return 1.0
    matched = sum(1 for k in keys
                  if abs(sum_a.get(k, 0) - sum_b.get(k, 0)) <= tolerance)
    return matched / len(keys)


def L2_similarity_weighted(sum_a: Counter, sum_b: Counter) -> float:
    """Min-over-max (a.k.a. weighted Jaccard for multisets).  Counts the
    *amount* of overlap, not just presence.  Strictest of the three."""
    keys = set(sum_a) | set(sum_b)
    num = sum(min(sum_a.get(k, 0), sum_b.get(k, 0)) for k in keys)
    den = sum(max(sum_a.get(k, 0), sum_b.get(k, 0)) for k in keys)
    return (num / den) if den else 1.0


# ── Layer 3: structured state extraction from OCR tokens ────────────────────

def L3_structured_state(tokens) -> dict:
    """Regex over OCR tokens to pull numeric / categorical game state."""
    text = " | ".join(t[0] for t in tokens)

    state: dict = {}

    # Crew capacities: 'N/M' pairs.  Filter out tiny denominators (probably
    # not crew — could be ratios, dates) and the obviously-huge currency
    # numbers that EasyOCR sometimes glues together.
    pairs = re.findall(r"\b(\d{1,4}(?:,\d{3})*)\s*/\s*(\d{1,4}(?:,\d{3})*)\b", text)
    caps = []
    for a, b in pairs:
        try:
            ai = int(a.replace(",", ""))
            bi = int(b.replace(",", ""))
            if 0 < bi < 100_000:        # discard nonsensical denominators
                caps.append((ai, bi))
        except ValueError:
            pass
    if caps:
        state["crew_capacities"] = caps

    # Standby crew ("X Ming" — Ming is the OCR mis-read of the unit suffix)
    m = re.search(r"\b(\d[\d,]*)\s*Ming\b", text)
    if m:
        try:
            state["standby_crew"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Min crew threshold
    m = re.search(r"Min\s*Crew\s*(\d[\d,]*)", text)
    if m:
        try:
            state["min_crew"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Modal dialog cues — keywords that appear on confirmation/result dialogs
    DIALOG_CUES = (
        "cancel", "confirm", "are you sure", "would you like",
        "pay extra", "would you", "recruit?", "want to",
    )
    text_low = text.lower()
    cue_hits = [kw for kw in DIALOG_CUES if kw in text_low]
    state["dialog_text_cues"] = cue_hits

    # "Not Enough Crew" style blocker indicators
    BLOCKER_PHRASES = ("not enough crew", "not enough supply", "insufficient")
    blocker_hits = [p for p in BLOCKER_PHRASES if p in text_low]
    if blocker_hits:
        state["blocker_phrases"] = blocker_hits

    return state


# ── Layer 4: goal predicates ────────────────────────────────────────────────

def L4_evaluate_has_enough_crew(state: dict) -> Optional[bool]:
    """For goal=has_enough_crew: returns True if current_crew >= min_crew,
    False if known short, None if can't tell from the state."""
    caps = state.get("crew_capacities")
    min_crew = state.get("min_crew")
    if not caps or min_crew is None:
        return None
    # First (largest-denominator) pair is conventionally the fleet aggregate.
    fleet_pair = max(caps, key=lambda p: p[1])
    current, _max = fleet_pair
    return current >= min_crew


# ── Expected outcomes (the empirical scoring rubric) ─────────────────────────

EXPECTED_CLUSTERS = [
    # Each inner list = frames that SHOULD hash together
    {"name": "harbor recruit-crew before vs after",       "members": ["s1.01", "s1.06"]},
    {"name": "inn    recruit-crew before vs after",       "members": ["s1.11", "s1.14"]},
    {"name": "cancel returns to recruit-crew (cycle)",    "members": ["s1.01", "s1.03"]},
    {"name": "success overlay on recruit-crew (2 frames)","members": ["s1.04", "s1.05"]},
]

# Two captures we have a deliberate design question about:
DESIGN_QUESTIONS = [
    {"q":   "Cross-port: should harbor recruit-crew at Port Royal hash same as Southside?",
     "frames": ["s1.01", "s2.00"]},
    {"q":   "Cross-port: post-recruit overlay at Port Royal vs Southside?",
     "frames": ["s1.04", "s2.02"]},
]

EXPECTED_DISTINCTIONS = [
    {"name": "harbor main view ≠ harbor recruit-crew",  "members": ["s1.00", "s1.01"]},
    {"name": "harbor recruit-crew ≠ inn recruit-crew",  "members": ["s1.01", "s1.11"]},
    {"name": "recruit screen ≠ confirm dialog",         "members": ["s1.01", "s1.02"]},
    {"name": "confirm dialog ≠ success overlay",        "members": ["s1.02", "s1.04"]},
    {"name": "harbor not-enough-crew ≠ inn entry",      "members": ["s1.00", "s1.10"]},
    {"name": "harbor repair ≠ harbor recruit",          "members": ["s1.07", "s1.01"]},
    {"name": "harbor supply ≠ harbor recruit",          "members": ["s1.08", "s1.01"]},
]


def score_recipe(recipe_fn, frames_by_id) -> dict:
    """Return scored breakdown: passes / fails / design-question outcomes."""
    fps = {fid: recipe_fn(f["elements"]) for fid, f in frames_by_id.items()}

    cluster_results = []
    for spec in EXPECTED_CLUSTERS:
        ids = spec["members"]
        present = [i for i in ids if i in fps]
        if len(present) < 2:
            cluster_results.append({"name": spec["name"], "status": "skip",
                                    "detail": f"missing frames {[i for i in ids if i not in fps]}"})
            continue
        same = len(set(fps[i] for i in present)) == 1
        cluster_results.append({
            "name": spec["name"], "status": "PASS" if same else "FAIL",
            "detail": ", ".join(f"{i}={fps[i]}" for i in present),
        })

    distinct_results = []
    for spec in EXPECTED_DISTINCTIONS:
        a, b = spec["members"]
        if a not in fps or b not in fps:
            distinct_results.append({"name": spec["name"], "status": "skip"})
            continue
        diff = fps[a] != fps[b]
        distinct_results.append({
            "name": spec["name"], "status": "PASS" if diff else "FAIL",
            "detail": f"{a}={fps[a]}  {b}={fps[b]}",
        })

    design_results = []
    for spec in DESIGN_QUESTIONS:
        a, b = spec["frames"]
        if a not in fps or b not in fps:
            design_results.append({"q": spec["q"], "answer": "missing frames"})
            continue
        design_results.append({"q": spec["q"],
                               "answer": "SAME" if fps[a] == fps[b] else "DIFFERENT",
                               "detail": f"{a}={fps[a]}  {b}={fps[b]}"})

    return {
        "fps": fps,
        "n_classes": len(set(fps.values())),
        "clusters": cluster_results,
        "distinctions": distinct_results,
        "design": design_results,
    }


# ── Tree-walk simulator ─────────────────────────────────────────────────────

def simulate_walk(name: str, walk: list[str], fps: dict[str, str]) -> None:
    print(f"\n  walk: {name}")
    print(f"  seq:  {' → '.join(walk)}")
    path_fps = []
    for fid in walk:
        fp = fps.get(fid)
        if fp is None:
            print(f"    {fid}  ! missing perception")
            continue
        cycle_at = None
        for i, prev in enumerate(path_fps):
            if prev == fp:
                cycle_at = i
                break
        marker = ""
        if cycle_at is not None:
            if cycle_at == 0:
                marker = "  ★ CYCLE — back at root"
            else:
                marker = f"  ★ CYCLE — back at step {cycle_at}"
        print(f"    {fid}  fp={fp}{marker}")
        path_fps.append(fp)


def simulate_walk_layered(name: str, walk: list[str], frames_by_id: dict) -> None:
    """Walk a path printing L1 fp, L2 similarity-to-prev, L3 state-diff,
    L4 goal-eval at each step — the multi-layer signal stack."""
    print(f"\n  walk: {name}")
    print(f"  seq:  {' → '.join(walk)}")
    prev_state = None
    prev_l2    = None
    prev_fp    = None
    path_fps   = []
    for fid in walk:
        f = frames_by_id.get(fid)
        if f is None:
            print(f"    {fid}  ! missing")
            continue
        fp   = fp_recipe_D(f["elements"])
        l2   = L2_component_summary(f["elements"])
        l3   = L3_structured_state(f["tokens"])
        l4   = L4_evaluate_has_enough_crew(l3)

        # L1 cycle check
        cycle_marker = ""
        for i, p in enumerate(path_fps):
            if p == fp:
                cycle_marker = f"  ★L1 cycle to step {i}"
                break
        # L2 similarity vs previous step
        if prev_l2 is not None:
            sim = L2_similarity_count_tolerance(prev_l2, l2)
            l2_str = f"  L2sim={sim:.2f}"
        else:
            l2_str = ""
        # L3 state-change signal
        state_change = []
        if prev_state is not None:
            for k in ("crew_capacities", "standby_crew", "min_crew",
                       "dialog_text_cues", "blocker_phrases"):
                if prev_state.get(k) != l3.get(k):
                    state_change.append(k)
        l3_str = f"  ΔL3=[{','.join(state_change)}]" if state_change else "  ΔL3=∅"
        # L4 goal status
        l4_str = (f"  L4=✓goal-met" if l4 is True
                  else f"  L4=✗not-met" if l4 is False
                  else "  L4=?")

        # State summary (current crew vs min for context)
        caps = l3.get("crew_capacities") or []
        min_c = l3.get("min_crew")
        fleet = max(caps, key=lambda p: p[1]) if caps else None
        state_brief = ""
        if fleet and min_c:
            state_brief = f"  fleet={fleet[0]}/{fleet[1]}  min={min_c}"
        if l3.get("dialog_text_cues"):
            state_brief += f"  dialog_cues={l3['dialog_text_cues']}"

        print(f"    {fid}  fp={fp}{cycle_marker}{l2_str}{l3_str}{l4_str}{state_brief}")
        prev_state = l3
        prev_l2    = l2
        prev_fp    = fp
        path_fps.append(fp)


# ── Multi-layer pairwise diagnostics ────────────────────────────────────────

def print_per_frame_layers(frames_by_id: dict) -> None:
    """One row per frame: L1 (recipe D), L2 (cell count), L3 state-summary,
    L4 goal-status."""
    print(f"\n{'=' * 78}")
    print("PER-FRAME LAYERED VIEW (L1 fp / L2 cells / L3 state / L4 goal)")
    print(f"{'=' * 78}")
    for fid in sorted(frames_by_id):
        f = frames_by_id[fid]
        fp = fp_recipe_D(f["elements"])
        l2 = L2_component_summary(f["elements"])
        l3 = L3_structured_state(f["tokens"])
        l4 = L4_evaluate_has_enough_crew(l3)

        # L2 brief: count of distinct (etype, region) cells + total elements
        cells_occupied = len(l2)
        elem_total = sum(l2.values())
        # L3 brief: just the most-distinctive fields
        caps = l3.get("crew_capacities") or []
        min_c = l3.get("min_crew")
        fleet = max(caps, key=lambda p: p[1]) if caps else None
        l3_brief = []
        if fleet:
            l3_brief.append(f"fleet={fleet[0]}/{fleet[1]}")
        if min_c is not None:
            l3_brief.append(f"min_crew={min_c}")
        if l3.get("standby_crew") is not None:
            l3_brief.append(f"standby={l3['standby_crew']}")
        if l3.get("dialog_text_cues"):
            l3_brief.append(f"dialog_cues={l3['dialog_text_cues']}")
        if l3.get("blocker_phrases"):
            l3_brief.append(f"blocker={l3['blocker_phrases']}")
        l3_str = "; ".join(l3_brief) if l3_brief else "(no state extracted)"

        l4_marker = ("✓" if l4 is True else "✗" if l4 is False else "?")
        print(f"\n  {fid}  [{f['screen_type']:18s}]")
        print(f"    L1 fp:    {fp}")
        print(f"    L2:       {cells_occupied} cells, {elem_total} elements")
        print(f"    L3:       {l3_str}")
        print(f"    L4 goal:  {l4_marker} has_enough_crew")


def print_l2_similarity_matrix(frames_by_id: dict) -> None:
    """For the key expected pairs, compute three L2 similarity scores
    side-by-side so we can see which formulation works."""
    print(f"\n{'=' * 78}")
    print("L2 SIMILARITY ON KEY PAIRS")
    print(f"{'=' * 78}")
    print("  (presence = Jaccard on cell presence; tol = matched within ±1; weighted = min/max)")
    pairs = [
        ("EXPECT MATCH:  s1.01 ≡ s1.06  (harbor before/after recruit)",  "s1.01", "s1.06"),
        ("EXPECT MATCH:  s1.11 ≡ s1.14  (inn before/after recruit)",     "s1.11", "s1.14"),
        ("EXPECT MATCH:  s1.04 ≡ s1.05  (overlay captured twice)",       "s1.04", "s1.05"),
        ("EXPECT MATCH:  s1.01 ≡ s1.03  (cycle: cancel returns to root)","s1.01", "s1.03"),
        ("EXPECT MATCH:  s1.01 ≡ s2.00  (cross-port: harbor recruit)",   "s1.01", "s2.00"),
        ("EXPECT MISS :  s1.14 ≢ s2.01  (Recipe D collision case)",      "s1.14", "s2.01"),
        ("EXPECT MISS :  s1.01 ≢ s1.02  (recruit screen vs confirm)",    "s1.01", "s1.02"),
        ("EXPECT MISS :  s1.01 ≢ s1.11  (harbor vs inn recruit)",        "s1.01", "s1.11"),
        ("EXPECT MISS :  s1.00 ≢ s1.01  (harbor main vs recruit-crew)",  "s1.00", "s1.01"),
        ("EXPECT MISS :  s1.07 ≢ s1.01  (repair vs recruit sub-menu)",   "s1.07", "s1.01"),
    ]
    print(f"\n  {'PAIR':70s}  {'presence':>8s}  {'tol±1':>6s}  {'weighted':>8s}")
    for desc, a, b in pairs:
        if a not in frames_by_id or b not in frames_by_id:
            continue
        sa = L2_component_summary(frames_by_id[a]["elements"])
        sb = L2_component_summary(frames_by_id[b]["elements"])
        p  = L2_similarity_jaccard_presence(sa, sb)
        t  = L2_similarity_count_tolerance(sa, sb)
        w  = L2_similarity_weighted(sa, sb)
        print(f"  {desc:70s}  {p:>8.2f}  {t:>6.2f}  {w:>8.2f}")


def print_l3_diff_pairs(frames_by_id: dict) -> None:
    """Show L3 structured state side-by-side for the 'goal achieved'
    pairs.  This is where the empirical case for L3 lives."""
    print(f"\n{'=' * 78}")
    print("L3 STATE DIFF ON 'GOAL ACHIEVED' PAIRS")
    print(f"{'=' * 78}")
    pairs = [
        ("Harbor before/after recruit", "s1.01", "s1.06"),
        ("Inn   before/after recruit",  "s1.11", "s1.14"),
        ("Recruit-confirm dialog (Q3)", "s1.14", "s2.01"),
    ]
    for desc, a, b in pairs:
        if a not in frames_by_id or b not in frames_by_id:
            continue
        sa = L3_structured_state(frames_by_id[a]["tokens"])
        sb = L3_structured_state(frames_by_id[b]["tokens"])
        l4a = L4_evaluate_has_enough_crew(sa)
        l4b = L4_evaluate_has_enough_crew(sb)
        print(f"\n  {desc}  ({a} → {b})")
        for k in sorted(set(sa) | set(sb)):
            va = sa.get(k)
            vb = sb.get(k)
            marker = "  ←DIFFERS" if va != vb else ""
            print(f"    {k}:")
            print(f"      A = {va}")
            print(f"      B = {vb}{marker}")
        print(f"    L4 has_enough_crew:")
        print(f"      A = {l4a}")
        print(f"      B = {l4b}")


# ── Reporting ───────────────────────────────────────────────────────────────

def print_recipe_report(name: str, recipe_fn, frames_by_id) -> dict:
    print(f"\n{'=' * 78}")
    print(f"Recipe {name}")
    print(f"{'=' * 78}")

    result = score_recipe(recipe_fn, frames_by_id)
    fps = result["fps"]

    # Equivalence classes
    classes = defaultdict(list)
    for fid, fp in fps.items():
        classes[fp].append(fid)
    print(f"\n  {result['n_classes']} equivalence classes "
          f"({len(fps)} frames):")
    for fp, members in sorted(classes.items(), key=lambda x: (-len(x[1]), x[0])):
        members.sort()
        print(f"    fp={fp}  ({len(members)}): {' '.join(members)}")
        for fid in members:
            f = frames_by_id[fid]
            note = (f["notes"][:70] + "…") if len(f["notes"]) > 70 else f["notes"]
            print(f"        {fid}  [{f['screen_type']:18s}]  {note}")

    print(f"\n  ── Expected clusters (must hash same) ──")
    for r in result["clusters"]:
        print(f"    [{r['status']}] {r['name']}")
        if "detail" in r:
            print(f"           {r['detail']}")

    print(f"\n  ── Expected distinctions (must hash different) ──")
    for r in result["distinctions"]:
        print(f"    [{r['status']}] {r['name']}")
        if "detail" in r:
            print(f"           {r['detail']}")

    print(f"\n  ── Open design questions ──")
    for r in result["design"]:
        print(f"    [{r['answer']}] {r['q']}")
        if "detail" in r:
            print(f"           {r['detail']}")

    return result


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    print("Loading frames + labels …")
    frames = load_frames()
    print(f"  {len(frames)} frame(s) across {len(TARGET_SESSIONS)} session(s).")

    print("\nRunning OmniParser perception on each frame …")
    for f in frames:
        run_perception(f)
        print(f"  {f['short_id']}  {f['file']}  → "
              f"{len(f['elements'])} elements  [{f['screen_type']}]")

    frames_by_id = {f["short_id"]: f for f in frames}

    summary = {}
    for name, fn in RECIPES.items():
        summary[name] = print_recipe_report(name, fn, frames_by_id)

    # ── Tree-walk simulations using the best-scoring recipe ─────────────
    best_name = max(
        summary.keys(),
        key=lambda n: (
            sum(1 for r in summary[n]["clusters"] if r["status"] == "PASS")
            + sum(1 for r in summary[n]["distinctions"] if r["status"] == "PASS")
        ),
    )
    print(f"\n{'=' * 78}")
    print(f"TREE WALK SIMULATIONS (using best-scoring recipe: {best_name})")
    print(f"{'=' * 78}")
    fps = summary[best_name]["fps"]

    simulate_walk(
        "Harbor success path (recruit → confirm → OK → overlay → dismiss → done)",
        ["s1.00", "s1.01", "s1.02", "s1.04", "s1.05", "s1.06"],
        fps,
    )
    simulate_walk(
        "Harbor cycle path (recruit → confirm → CANCEL → back at recruit screen)",
        ["s1.01", "s1.02", "s1.03"],
        fps,
    )
    simulate_walk(
        "Inn success path (inn entry → recruit-crew → confirm → overlay → done)",
        ["s1.10", "s1.11", "s1.12", "s1.13", "s1.14"],
        fps,
    )
    simulate_walk(
        "Southside (cross-port) recruit success path",
        ["s2.00", "s2.01", "s2.02", "s2.03"],
        fps,
    )

    # ── Multi-layer analysis ─────────────────────────────────────────────
    print_per_frame_layers(frames_by_id)
    print_l2_similarity_matrix(frames_by_id)
    print_l3_diff_pairs(frames_by_id)

    # ── Tree walks annotated with all four layers ────────────────────────
    print(f"\n{'=' * 78}")
    print("LAYERED TREE WALKS (L1 fp / L2 sim-to-prev / L3 state-delta / L4 goal)")
    print(f"{'=' * 78}")
    simulate_walk_layered(
        "Harbor success path",
        ["s1.00", "s1.01", "s1.02", "s1.04", "s1.05", "s1.06"],
        frames_by_id,
    )
    simulate_walk_layered(
        "Harbor cycle path (Cancel returns to recruit)",
        ["s1.01", "s1.02", "s1.03"],
        frames_by_id,
    )
    simulate_walk_layered(
        "Inn success path",
        ["s1.10", "s1.11", "s1.12", "s1.13", "s1.14"],
        frames_by_id,
    )
    simulate_walk_layered(
        "Southside (cross-port) success path",
        ["s2.00", "s2.01", "s2.02", "s2.03"],
        frames_by_id,
    )

    # ── Final tally ──────────────────────────────────────────────────────
    print(f"\n{'=' * 78}")
    print("RECIPE SUMMARY")
    print(f"{'=' * 78}")
    for name, r in summary.items():
        c_pass = sum(1 for x in r["clusters"]     if x["status"] == "PASS")
        c_tot  = sum(1 for x in r["clusters"]     if x["status"] != "skip")
        d_pass = sum(1 for x in r["distinctions"] if x["status"] == "PASS")
        d_tot  = sum(1 for x in r["distinctions"] if x["status"] != "skip")
        print(f"  {name}")
        print(f"    classes={r['n_classes']}/{len(frames_by_id)}  "
              f"clusters={c_pass}/{c_tot}  distinctions={d_pass}/{d_tot}")
    print()
    print(f"Best-scoring recipe: {best_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
