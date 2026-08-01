"""Temporal Moondream + LLM-aggregator calibration harness.

Design: docs/temporal_scene_classifier.md

What this script does
---------------------
1. Group `data/labels.jsonl` frames by session, ordered by capture time.
2. For each session, subsample three ways (dense / sparse / production-like).
3. For each frame in the subsample, run a Moondream binary cascade
   (Q1: 3D world?  Q2: chromed UI?  Q3: loading/transition?  Q4: dialog?).
4. Append (timestamp, answers, sceneModel.scene_type) to a ring buffer.
5. Ask a text LLM to aggregate the buffer + FSM transition graph and
   emit its best estimate of the CURRENT state.
6. Score against the labelled `screen_type`.

Outputs
-------
- A per-variant accuracy table written to stdout.
- A JSONL trace at `data/calibration/temporal_<variant>_<timestamp>.jsonl`
  (one row per frame, capturing buffer state, LLM output, label).

Usage
-----
    python tools/moondream_temporal_calibration.py \
        --sessions 2026-04-15_16-05-29 2026-04-15_21-54-26 \
        --variants dense sparse production_like \
        --aggregator qwen           # or 'claude' or 'rule_only'
        --max-frames-per-session 80
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator, Optional

REPO_ROOT       = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image

LABELS_PATH     = REPO_ROOT / "data" / "labels.jsonl"
SESSIONS_DIR    = REPO_ROOT / "data" / "sessions"
FSM_STATES_PATH = REPO_ROOT / "memory" / "knowledge" / "fsm" / "states.json"
OUTPUT_DIR      = REPO_ROOT / "data" / "calibration"

BUFFER_SIZE     = 8

# Categories that animate fast — production-like sampling polls these dense.
FAST_CATEGORIES = {
    "loading", "port_loading", "port_arrival_overlay",
    "dialog_overlay", "result_screen", "announcement",
}


# SceneModel uses its own taxonomy; labels.jsonl uses a slightly different one.
# Normalize both sides to a shared family for scoring.
def normalize(state: str) -> str:
    if not state:
        return "unknown"
    s = state.lower()
    # Strip SceneModel sub-typing: 'sub_menu:recruit crew' -> 'sub_menu'.
    s = s.split(":", 1)[0].strip()
    aliases = {
        "building":         "building_interior",
        "port_arrival":     "port_arrival_overlay",
        "arrival_overlay":  "port_arrival_overlay",
        "dialog":           "dialog_overlay",
        "sailing":          "sea",
        "town":             "port_overworld",
    }
    return aliases.get(s, s)


# Label categories that are now modeled as (base, overlay) tuples rather
# than independent base scenes.  A frame labelled with one of these is
# scored correct when our overlay detector fires (regardless of which
# overlay kind), and the base scene is something reasonable underneath.
OVERLAY_LABELS = {
    "dialog_transaction", "dialog_event", "dialog_reward", "dialog_system",
    "dialog_gameplay",    "dialog_overlay", "dialog_game_notice",
    "main_menu",          "building_npc_overlay", "announcement", "result_screen",
    "dialog_shop",
}


def score_with_overlay(label: str, stage2_kind: str, overlay_kind: Optional[str]) -> bool:
    """Score a frame with the (base, overlay) tuple model.

    - If the label is an overlay category, we match when our overlay
      detector fired (any overlay kind).  The base scene under the
      overlay is incidental.
    - Otherwise fall back to flat normalised scene_kind comparison.
    """
    if label in OVERLAY_LABELS:
        return overlay_kind is not None
    return normalize(stage2_kind) == normalize(label)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@dataclass
class LabelledFrame:
    session_id:   str
    file:         str
    screen_type:  str
    capture_idx:  int           # numeric prefix from filename
    capture_ts:   int           # ms-resolution stamp from filename
    path:         Path


def _parse_file_name(name: str) -> tuple[int, int]:
    """`0042_2152316512.png` -> (capture_idx=42, capture_ts=2152316512)."""
    stem = name.rsplit(".", 1)[0]
    parts = stem.split("_", 1)
    idx = int(parts[0])
    ts  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else idx
    return idx, ts


def load_labels(session_filter: Optional[set[str]] = None) -> dict[str, list[LabelledFrame]]:
    """Returns {session_id: [frames sorted by capture order]}."""
    out: dict[str, list[LabelledFrame]] = {}
    for line in LABELS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        sid = r["session_id"]
        if session_filter and sid not in session_filter:
            continue
        fname = r["file"]
        path  = SESSIONS_DIR / sid / "frames" / fname
        if not path.exists():
            continue
        idx, ts = _parse_file_name(fname)
        out.setdefault(sid, []).append(LabelledFrame(
            session_id=sid, file=fname, screen_type=r.get("screen_type", ""),
            capture_idx=idx, capture_ts=ts, path=path,
        ))
    for sid in out:
        out[sid].sort(key=lambda f: f.capture_idx)
    return out


# ---------------------------------------------------------------------------
# Subsampling variants
# ---------------------------------------------------------------------------

def subsample_dense(frames: list[LabelledFrame]) -> Iterator[LabelledFrame]:
    yield from frames


def subsample_sparse(frames: list[LabelledFrame], n: int = 5) -> Iterator[LabelledFrame]:
    for i, f in enumerate(frames):
        if i % n == 0:
            yield f


def subsample_production_like(frames: list[LabelledFrame]) -> Iterator[LabelledFrame]:
    """Mimic action-driven polling.

    Heuristic: when current frame's labelled category is "fast"
    (transient / animation), poll the next frame too.  When it's
    "slow" (stable navigation state), skip ahead by ~5 frames.

    The label is the ground truth — the real bot doesn't know what
    category it's in, but the polling regime in production is shaped
    by the bot's own action loop, which IS correlated with screen
    type (after a tap → many quick checks; while sailing → idle).
    Using the label as a proxy is a reasonable approximation.
    """
    i = 0
    while i < len(frames):
        f = frames[i]
        yield f
        step = 1 if f.screen_type in FAST_CATEGORIES else 5
        i += step


VARIANTS = {
    "dense":           subsample_dense,
    "sparse":          subsample_sparse,
    "production_like": subsample_production_like,
}


# ---------------------------------------------------------------------------
# Stage-1 Moondream binary cascade
# ---------------------------------------------------------------------------

@dataclass
class StageOneAnswers:
    is_3d_world:    Optional[bool] = None    # Q1
    is_chromed_ui:  Optional[bool] = None    # Q2
    is_transition:  Optional[bool] = None    # Q3
    is_dialog:      Optional[bool] = None    # Q4

    def family(self) -> str:
        if self.is_transition:  return "transition"
        if self.is_dialog:      return "overlay"
        if self.is_3d_world:    return "overworld"
        if self.is_chromed_ui:  return "chromed"
        return "unknown"


_CASCADE_QUESTIONS = [
    ("is_3d_world",
     "Screenshot from the mobile game Uncharted Waters Origin.  Does this "
     "frame show a 3D-rendered game world — either a port town with a "
     "character standing among buildings, or a ship on open water?  "
     "Answer 'yes' or 'no'."),
    ("is_chromed_ui",
     "Screenshot from the mobile game Uncharted Waters Origin.  Is this a "
     "2D UI screen with menus, lists, buttons or panels filling most of "
     "the view (e.g. market, building interior, sub-menu)?  Answer 'yes' "
     "or 'no'."),
    ("is_transition",
     "Screenshot from the mobile game Uncharted Waters Origin.  Is this a "
     "loading screen, an arrival overlay, or a brief animation between "
     "two real screens?  Answer 'yes' or 'no'."),
    ("is_dialog",
     "Screenshot from the mobile game Uncharted Waters Origin.  Is the "
     "main content a modal dialog box, popup, reward screen, or "
     "announcement that overlays the underlying scene?  Answer 'yes' or "
     "'no'."),
]


def run_stage_one(frame: Image.Image, vision) -> StageOneAnswers:
    thumb = frame.copy()
    thumb.thumbnail((800, 400))
    ans = StageOneAnswers()
    for attr, prompt in _CASCADE_QUESTIONS:
        try:
            raw = vision.ask(prompt, frame=thumb)
        except Exception:
            raw = None
        if not raw:
            continue
        setattr(ans, attr, raw.lower().strip().startswith("y"))
    return ans


# ---------------------------------------------------------------------------
# Stage-2 SceneModel
# ---------------------------------------------------------------------------

def run_stage_two(frame: Image.Image) -> tuple[str, str, Optional[str]]:
    """Returns (scene_kind, confidence, overlay_kind)."""
    try:
        from vision.omniparser  import get_omniparser
        from vision.scene_model import detect_scene
        parser = get_omniparser()
        elements = parser.parse_fast(frame)
        model    = detect_scene(frame.width, frame.height, elements)
        overlay_kind = model.overlay.kind if model.overlay else None
        return model.scene_kind, model.confidence, overlay_kind
    except Exception as e:
        return "unknown", "low", None


# ---------------------------------------------------------------------------
# FSM transition graph (LLM context)
# ---------------------------------------------------------------------------

def load_fsm_graph() -> dict:
    if not FSM_STATES_PATH.exists():
        return {"states": []}
    return {"states": json.loads(FSM_STATES_PATH.read_text())}


def _fsm_summary_for_prompt(graph: dict) -> str:
    """Compact text rendering: 'state -> [legal next states]'."""
    lines = []
    for s in graph["states"]:
        exits = s.get("exits", [])
        next_states = sorted({e["to"] for e in exits if "to" in e})
        lines.append(f"{s['id']} -> {next_states}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Temporal buffer + LLM aggregator
# ---------------------------------------------------------------------------

@dataclass
class BufferEntry:
    t:           int
    family:      str
    scene_kind:  str
    confidence:  str
    raw_q:       dict[str, Optional[bool]] = field(default_factory=dict)


def _buffer_for_prompt(buffer: deque[BufferEntry]) -> str:
    rows = []
    for i, e in enumerate(buffer):
        rows.append(
            f"  t={e.t}  family={e.family:11s}  scene={e.scene_kind:22s}  "
            f"conf={e.confidence}  raw={e.raw_q}"
        )
    return "\n".join(rows)


_AGGREGATOR_SYSTEM = """\
You are the state estimator for an autonomous bot playing Uncharted Waters
Origin.  Each tick the bot captures a frame and runs two stages of
perception.  Stage 1 is a small VLM answering four yes/no questions about
the frame.  Stage 2 is a deterministic SceneModel that proposes a specific
scene kind.

Your job: combine the last N ticks of evidence with the FSM transition
graph and output a single best estimate of the CURRENT state.

Rules:
  - Pick from the set of states present in the FSM graph.
  - You may infer a state that wasn't directly observed if the buffer's
    before/after entries imply it via a legal FSM edge.
  - Never emit a state that is not reachable from the previous state in
    one or two hops on the graph.
  - If the buffer is too short or contradictory, emit "unknown".

Output strict JSON:
  {"state": "<state_id>", "confidence": "high|medium|low", "reason": "<one line>"}
"""


def aggregate_rule_only(buffer: deque[BufferEntry], _graph: dict) -> dict:
    """No-LLM baseline: latest non-unknown Stage-2 verdict wins."""
    for e in reversed(buffer):
        if e.scene_kind not in ("unknown", ""):
            return {"state": e.scene_kind, "confidence": e.confidence,
                    "reason": "latest deterministic verdict"}
    return {"state": "unknown", "confidence": "low", "reason": "empty buffer"}


def aggregate_qwen(buffer: deque[BufferEntry], graph: dict) -> dict:
    """Local Qwen-2 VL or text LLM via vision.local_vision."""
    from vision.local_vision import get_vision
    v = get_vision()
    if not v.check_available():
        return aggregate_rule_only(buffer, graph)
    prompt = (
        _AGGREGATOR_SYSTEM
        + "\n\nFSM transition graph (state -> legal next states):\n"
        + _fsm_summary_for_prompt(graph)
        + "\n\nRecent perception buffer (oldest → newest):\n"
        + _buffer_for_prompt(buffer)
        + "\n\nReply with JSON only.\n"
    )
    try:
        raw = v.ask_json(prompt)
        if isinstance(raw, dict) and "state" in raw:
            return raw
    except Exception:
        pass
    return aggregate_rule_only(buffer, graph)


def aggregate_claude(buffer: deque[BufferEntry], graph: dict) -> dict:
    """Claude Haiku via existing scene-API wrapper if available."""
    try:
        from vision.claude_vision import ask_claude_json
    except Exception:
        return aggregate_rule_only(buffer, graph)
    prompt = (
        _AGGREGATOR_SYSTEM
        + "\n\nFSM transition graph:\n"
        + _fsm_summary_for_prompt(graph)
        + "\n\nRecent perception buffer:\n"
        + _buffer_for_prompt(buffer)
    )
    try:
        return ask_claude_json(prompt)
    except Exception:
        return aggregate_rule_only(buffer, graph)


AGGREGATORS = {
    "rule_only": aggregate_rule_only,
    "qwen":      aggregate_qwen,
    "claude":    aggregate_claude,
}


# ---------------------------------------------------------------------------
# Calibration loop
# ---------------------------------------------------------------------------

def run_one_session(
    session_id:    str,
    frames:        list[LabelledFrame],
    variant_name:  str,
    aggregator_fn,
    fsm_graph:     dict,
    vision,
    out_file,
    max_frames:    Optional[int] = None,
) -> dict[str, int]:
    """Returns counters: {total, correct, illegal, ...}."""
    buffer: deque[BufferEntry] = deque(maxlen=BUFFER_SIZE)
    counters = {"total": 0, "correct": 0, "stage2_correct": 0, "illegal": 0}
    legal_next: dict[str, set[str]] = {}
    for s in fsm_graph["states"]:
        legal_next[s["id"]] = {e["to"] for e in s.get("exits", [])}

    subsampler = VARIANTS[variant_name]
    sequence = list(subsampler(frames))
    if max_frames:
        sequence = sequence[:max_frames]

    prev_state = None
    for f in sequence:
        img = Image.open(f.path)
        s1  = run_stage_one(img, vision)
        kind, conf, overlay_kind = run_stage_two(img)
        buffer.append(BufferEntry(
            t=f.capture_idx, family=s1.family(),
            scene_kind=kind, confidence=conf, raw_q=asdict(s1),
        ))

        result = aggregator_fn(buffer, fsm_graph)
        inferred = result.get("state", "unknown")
        norm_inferred = normalize(inferred)
        norm_kind     = normalize(kind)
        norm_label    = normalize(f.screen_type)
        is_correct        = score_with_overlay(f.screen_type, kind, overlay_kind)
        stage2_is_correct = score_with_overlay(f.screen_type, kind, overlay_kind)
        legal_set  = {normalize(s) for s in legal_next.get(normalize(prev_state) if prev_state else "", set())}
        is_illegal = (
            prev_state is not None
            and norm_inferred not in legal_set
            and norm_inferred != normalize(prev_state)
            and norm_inferred != "unknown"
        )

        counters["total"] += 1
        counters["correct"]        += int(is_correct)
        counters["stage2_correct"] += int(stage2_is_correct)
        counters["illegal"]        += int(is_illegal)

        row = {
            "session": session_id, "variant": variant_name,
            "file": f.file, "label": f.screen_type,
            "stage1": asdict(s1), "stage2_kind": kind,
            "stage2_conf": conf, "overlay": overlay_kind,
            "aggregator": result,
            "is_correct": is_correct, "is_illegal_transition": is_illegal,
        }
        out_file.write(json.dumps(row) + "\n")
        out_file.flush()
        prev_state = inferred
    return counters


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="*", default=None,
                    help="Session IDs to include (default: all in labels.jsonl).")
    ap.add_argument("--variants", nargs="*",
                    default=["dense", "sparse", "production_like"],
                    choices=list(VARIANTS.keys()))
    ap.add_argument("--aggregator", default="rule_only",
                    choices=list(AGGREGATORS.keys()))
    ap.add_argument("--max-frames-per-session", type=int, default=80)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    session_filter = set(args.sessions) if args.sessions else None
    sessions = load_labels(session_filter)
    if not sessions:
        print("No labelled frames found for those sessions.")
        return

    fsm_graph = load_fsm_graph()
    if not fsm_graph["states"]:
        print(f"WARNING: FSM graph empty / missing at {FSM_STATES_PATH}")

    from vision.local_vision import get_vision
    vision = get_vision()
    if not vision.check_available():
        print("WARNING: local vision model unavailable — Stage 1 will be all None")

    aggregator_fn = AGGREGATORS[args.aggregator]
    timestamp = time.strftime("%Y%m%dT%H%M%S")

    for variant in args.variants:
        out_path = OUTPUT_DIR / f"temporal_{variant}_{args.aggregator}_{timestamp}.jsonl"
        with open(out_path, "w") as out_file:
            totals = {"total": 0, "correct": 0, "stage2_correct": 0, "illegal": 0}
            for sid, frames in sessions.items():
                c = run_one_session(
                    sid, frames, variant, aggregator_fn, fsm_graph,
                    vision, out_file, max_frames=args.max_frames_per_session,
                )
                for k in totals:
                    totals[k] += c[k]
                print(f"  [{variant}] {sid}: "
                      f"agg {c['correct']}/{c['total']} "
                      f"stage2 {c['stage2_correct']}/{c['total']} "
                      f"illegal={c['illegal']}", flush=True)
            n = max(1, totals["total"])
            print(f"\n=== {variant} / {args.aggregator} ===")
            print(f"aggregator accuracy: {totals['correct']}/{n} = {totals['correct']/n:.1%}")
            print(f"stage2 accuracy:     {totals['stage2_correct']}/{n} = {totals['stage2_correct']/n:.1%}")
            print(f"illegal transitions: {totals['illegal']}/{n}")
            print(f"trace: {out_path}\n")


if __name__ == "__main__":
    main()
