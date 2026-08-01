# supervisor/labeler.py
# Web-based labeling UI for tagging captured screenshots with screen types.
#
# Shows one screenshot at a time. You click a button (or press a keyboard
# shortcut) to assign a screen type. Labels are saved to data/labels.jsonl.
# Already-labeled frames are skipped — you can stop and resume at any time.
#
# After selecting a screen type, an optional tag panel appears for sub-state
# details (e.g. which right-panel tab is active). Press Enter to confirm.
# A notes field is always available for free-text observations.
#
# Usage:
#   python -m supervisor.labeler
#   open http://localhost:5050

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

# Project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

DATA_DIR     = Path(__file__).parent.parent / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
LABELS_FILE  = DATA_DIR / "labels.jsonl"

_SCREEN_TYPES_FILE = DATA_DIR / "knowledge" / "screen_types.json"
_SCREEN_TAGS_FILE  = DATA_DIR / "knowledge" / "screen_tags.json"


def _shortcut_key(index: int) -> str:
    if index < 9:
        return str(index + 1)
    if index == 9:
        return "0"
    return chr(ord("a") + (index - 10))


def _load_screen_types() -> list[dict]:
    if _SCREEN_TYPES_FILE.exists():
        with open(_SCREEN_TYPES_FILE) as f:
            raw = json.load(f)
        return [
            {"key": _shortcut_key(i), "id": st["id"], "label": st["label"]}
            for i, st in enumerate(raw.get("screen_types", []))
        ]
    return [
        {"key": "1", "id": "port_overworld",    "label": "Port Overworld"},
        {"key": "2", "id": "port_map",          "label": "Port Map"},
        {"key": "3", "id": "building_interior", "label": "Building Interior"},
        {"key": "4", "id": "sub_menu",          "label": "Sub Menu"},
        {"key": "5", "id": "world_map",         "label": "World Map"},
        {"key": "6", "id": "sea",               "label": "Sea / Sailing"},
        {"key": "7", "id": "loading",           "label": "Loading Screen"},
        {"key": "8", "id": "other",             "label": "Other"},
    ]


def _load_screen_tags() -> dict[str, list[dict]]:
    """Return { screen_type_id: [ {group, tags:[{id,label,key},...]} ] }"""
    if _SCREEN_TAGS_FILE.exists():
        with open(_SCREEN_TAGS_FILE) as f:
            raw = json.load(f)
        return raw.get("screen_tags", {})
    return {}


SCREEN_TYPES: list[dict]          = _load_screen_types()
SCREEN_TAGS:  dict[str, list]     = _load_screen_tags()

app = Flask(__name__, template_folder="templates")


# ── label store ───────────────────────────────────────────────────────────────

def _load_labels() -> dict[str, dict]:
    """
    Return { 'session_id/filename': record } for all saved labels.
    record has at minimum: screen_type, and optionally tags (list), notes (str).
    Last entry in the file wins (supports re-labeling by appending).
    """
    labels: dict[str, dict] = {}
    if not LABELS_FILE.exists():
        return labels
    with open(LABELS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                key = f"{rec['session_id']}/{rec['file']}"
                labels[key] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return labels


def _save_label(session_id: str, filename: str, screen_type: str,
                tags: list[str] | None = None,
                notes: str = "") -> None:
    """Append one label record to labels.jsonl."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record: dict = {
        "session_id":  session_id,
        "file":        filename,
        "screen_type": screen_type,
        "labeled_at":  datetime.now().isoformat(),
        "labeled_by":  "human",
    }
    if tags:
        record["tags"] = tags
    if notes:
        record["notes"] = notes.strip()
    with open(LABELS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── session helpers ───────────────────────────────────────────────────────────

def _list_sessions() -> list[dict]:
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    labels = _load_labels()
    for session_dir in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        meta_path = session_dir / "metadata.json"
        if not meta_path.exists():
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        frames = meta.get("frames", [])
        session_id = meta["session_id"]
        labeled = sum(
            1 for fr in frames
            if f"{session_id}/{fr['file']}" in labels
        )
        sessions.append({
            "session_id": session_id,
            "started_at": meta.get("started_at", "")[:16].replace("T", " "),
            "total":      len(frames),
            "labeled":    labeled,
            "pct":        int(100 * labeled / len(frames)) if frames else 0,
        })
    return sessions


def _session_frames(session_id: str) -> list[dict]:
    meta_path = SESSIONS_DIR / session_id / "metadata.json"
    if not meta_path.exists():
        return []
    with open(meta_path) as f:
        meta = json.load(f)
    labels = _load_labels()
    frames = []
    for fr in meta.get("frames", []):
        key = f"{session_id}/{fr['file']}"
        rec = labels.get(key)
        frames.append({
            "file":        fr["file"],
            "timestamp":   fr.get("timestamp", "")[:19].replace("T", " "),
            "screen_type": rec["screen_type"] if rec else None,
            "tags":        rec.get("tags", []) if rec else [],
            "notes":       rec.get("notes", "") if rec else "",
        })
    return frames


# ── type-filter helpers ───────────────────────────────────────────────────────

def _known_type_ids() -> set[str]:
    return {st["id"] for st in SCREEN_TYPES}


def _frames_by_type(screen_type: str) -> list[dict]:
    labels = _load_labels()
    result = []
    if not SESSIONS_DIR.exists():
        return result
    for session_dir in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        meta_path = session_dir / "metadata.json"
        if not meta_path.exists():
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        sid = meta["session_id"]
        for fr in meta.get("frames", []):
            key = f"{sid}/{fr['file']}"
            rec = labels.get(key)
            if rec and rec.get("screen_type") == screen_type:
                result.append({
                    "session_id":  sid,
                    "file":        fr["file"],
                    "screen_type": screen_type,
                    "tags":        rec.get("tags", []),
                    "notes":       rec.get("notes", ""),
                    "timestamp":   fr.get("timestamp", "")[:19].replace("T", " "),
                })
    return result


def _label_stats() -> list[dict]:
    from collections import Counter
    labels = _load_labels()
    counts: Counter = Counter(
        rec.get("screen_type") for rec in labels.values() if rec.get("screen_type")
    )
    total = sum(counts.values()) or 1
    known_ids = {st["id"] for st in SCREEN_TYPES}
    rows = []
    for st in SCREEN_TYPES:
        c = counts.get(st["id"], 0)
        rows.append({"id": st["id"], "label": st["label"], "count": c,
                     "pct": round(100 * c / total)})
    for type_id, c in counts.items():
        if type_id not in known_ids:
            rows.append({"id": type_id, "label": type_id, "count": c,
                         "pct": round(100 * c / total)})
    return rows


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    sessions   = _list_sessions()
    stats      = _label_stats()
    total_labels = sum(s["count"] for s in stats)
    known_ids  = _known_type_ids()
    return render_template("index.html", sessions=sessions,
                           stats=stats, total_labels=total_labels,
                           known_ids=known_ids)


@app.route("/session/<session_id>")
def session_view(session_id: str):
    frames = _session_frames(session_id)
    if not frames:
        return f"Session {session_id!r} not found or empty", 404
    start_index = next(
        (i for i, f in enumerate(frames) if f["screen_type"] is None), 0
    )
    return redirect(url_for("label_frame", session_id=session_id, index=start_index))


@app.route("/session/<session_id>/frame/<int:index>")
def label_frame(session_id: str, index: int):
    frames = _session_frames(session_id)
    if not frames:
        return f"Session {session_id!r} not found", 404
    index = max(0, min(index, len(frames) - 1))
    frame = frames[index]
    labeled_count = sum(1 for f in frames if f["screen_type"] is not None)
    return render_template(
        "label.html",
        session_id=session_id,
        frame=frame,
        index=index,
        total=len(frames),
        labeled=labeled_count,
        screen_types=SCREEN_TYPES,
        screen_tags=SCREEN_TAGS,
        prev_index=max(0, index - 1),
        next_index=min(len(frames) - 1, index + 1),
        nav_base=f"/session/{session_id}",
        filter_type=None,
        filter_label=None,
    )


@app.route("/type/<screen_type>")
def type_view(screen_type: str):
    frames = _frames_by_type(screen_type)
    if not frames:
        return redirect(url_for("index"))
    return redirect(url_for("type_frame", screen_type=screen_type, index=0))


@app.route("/type/<screen_type>/frame/<int:index>")
def type_frame(screen_type: str, index: int):
    frames = _frames_by_type(screen_type)
    if not frames:
        return redirect(url_for("index"))
    index = max(0, min(index, len(frames) - 1))
    frame = frames[index]
    filter_label = next(
        (st["label"] for st in SCREEN_TYPES if st["id"] == screen_type),
        screen_type,
    )
    return render_template(
        "label.html",
        session_id=frame["session_id"],
        frame=frame,
        index=index,
        total=len(frames),
        labeled=0,
        screen_types=SCREEN_TYPES,
        screen_tags=SCREEN_TAGS,
        prev_index=max(0, index - 1),
        next_index=min(len(frames) - 1, index + 1),
        nav_base=f"/type/{screen_type}",
        filter_type=screen_type,
        filter_label=filter_label,
    )


@app.route("/label", methods=["POST"])
def save_label():
    data        = request.get_json()
    session_id  = data["session_id"]
    filename    = data["file"]
    screen_type = data["screen_type"]
    tags        = data.get("tags", [])
    notes       = data.get("notes", "").strip()
    filter_type = data.get("filter_type")

    if screen_type != "skip":
        _save_label(session_id, filename, screen_type, tags=tags or None, notes=notes)

    if filter_type:
        current_index = data.get("filter_index", 0)
        remaining = _frames_by_type(filter_type)
        next_index = min(current_index, len(remaining) - 1) if remaining else None
        return jsonify({
            "saved":       screen_type != "skip",
            "next_index":  next_index,
            "labeled":     0,
            "total":       len(remaining),
            "done":        not remaining,
            "filter_type": filter_type,
        })

    frames = _session_frames(session_id)
    current_index = next(
        (i for i, f in enumerate(frames) if f["file"] == filename), 0
    )
    next_unlabeled = next(
        (i for i in range(current_index + 1, len(frames))
         if frames[i]["screen_type"] is None),
        None,
    )
    labeled_count = sum(1 for f in frames if f["screen_type"] is not None)
    return jsonify({
        "saved":      screen_type != "skip",
        "next_index": next_unlabeled,
        "labeled":    labeled_count,
        "total":      len(frames),
        "done":       next_unlabeled is None,
    })


@app.route("/frames/<session_id>/<filename>")
def serve_frame(session_id: str, filename: str):
    path = SESSIONS_DIR / session_id / "frames" / filename
    if not path.exists():
        return "Not found", 404
    return send_file(path, mimetype="image/png")


# ── training report ───────────────────────────────────────────────────────────

@app.route("/training")
def training_report():
    from training.collector import load_stats
    stats = load_stats()
    return render_template("training.html", stats=stats)


@app.route("/training/<category>")
def training_category(category: str):
    from training.collector import load_examples, load_stats
    page   = request.args.get("page", 1, type=int)
    limit  = 20
    offset = (page - 1) * limit
    examples = load_examples(category, limit=limit, offset=offset)
    total    = next((s["total"] for s in load_stats() if s["category"] == category), 0)
    pages    = max(1, (total + limit - 1) // limit)
    return render_template(
        "training.html",
        stats=None,
        category=category,
        examples=examples,
        page=page,
        pages=pages,
        total=total,
    )


@app.route("/training/<category>/crop/<filename>")
def serve_training_crop(category: str, filename: str):
    path = DATA_DIR / "training" / category / "crops" / filename
    if not path.exists():
        return "Not found", 404
    return send_file(path, mimetype="image/png")


if __name__ == "__main__":
    print(f"\n  UWO Labeler")
    print(f"  Open: http://localhost:5050\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
