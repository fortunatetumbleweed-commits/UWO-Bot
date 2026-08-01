"""Auto-classify the screen_type of every unlabelled frame in a session.

Reads each frame, sends it to Claude Sonnet with the canonical screen-
type catalogue from data/knowledge/screen_types.json, and writes a row
into data/labels.jsonl with just `screen_type` and a one-line `notes`
field.  Tags are intentionally left empty — the human reviewer (or a
follow-up pass like auto_label_sea_capture.py for sea frames) fills
those in.

Skips frames that ALREADY have any label record — including ones from
prior runs of this script and from the human labeller.

Usage:
    python tools/auto_classify_screen_type.py \\
        --session 2026-05-26_09-02-11 \\
        [--model claude-sonnet-4-5] \\
        [--dry-run] [--limit N] [--overwrite]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

_TYPES_PATH = _REPO / "data" / "knowledge" / "screen_types.json"
_LABELS_PATH = _REPO / "data" / "labels.jsonl"


# ── Type catalogue ─────────────────────────────────────────────────────────

def _load_types() -> list[dict]:
    with open(_TYPES_PATH) as f:
        return json.load(f)["screen_types"]


def _catalogue_block(types: list[dict]) -> str:
    """Compact, prompt-friendly description per type."""
    lines = []
    for t in types:
        lines.append(f"- id: {t['id']}")
        lines.append(f"  label: {t.get('label', '')}")
        desc = (t.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 320:
            desc = desc[:317] + "…"
        lines.append(f"  description: {desc}")
        kd = (t.get("key_differentiator") or "").strip().replace("\n", " ")
        if kd:
            if len(kd) > 240:
                kd = kd[:237] + "…"
            lines.append(f"  key_differentiator: {kd}")
        lines.append("")
    return "\n".join(lines)


_SYSTEM_PROMPT = """You are an image classifier for a mobile game called Uncharted Waters Origin.
Your job is to identify the screen_type of a single game frame.
Pick exactly ONE id from the catalogue.  Use the `key_differentiator`
field as the primary signal.  When two types look similar, prefer the
one whose chrome (top-left, top-right, right panel, bottom controls)
matches the frame exactly.

CRITICAL — background ≠ screen_type.  Many overlay/sub-menu screens
render with the 3D world (water + ships, or a port street) STILL
VISIBLE behind their panels.  Do NOT classify on background alone.

A frame is "sea" ONLY if you can see BOTH:
  (1) the supplies-remaining indicator in the TOP-LEFT
      (NOT a back arrow, NOT a screen title like "Barter"), AND
  (2) the navigation controls (rudder / steering wheel / anchor /
      directional pad) in the BOTTOM-LEFT.
If a back arrow + screen title is in the top-left and there are no
steering controls in the bottom-left, the frame is NOT "sea" — it
is almost always a sub_menu (e.g. Barter, Supply, Purchase) drawn
on top of a sea-or-port backdrop.

Same rule applies to "port_overworld": needs the port name top-left
(no back arrow) AND character model bottom-centre.  A back arrow +
named title in the top-left disqualifies it.

Output strict JSON — no prose, no code fences.
"""


_USER_TEMPLATE = """Catalogue of screen types:

{catalogue}

Classify the attached frame.  Return JSON with this shape:

{{
  "screen_type": "<one id from the catalogue>",
  "notes": "<one-sentence reason citing the visible chrome / differentiator>"
}}

If the frame matches none of the catalogue types, use "other".
"""


# ── Anthropic client ───────────────────────────────────────────────────────

def _make_client():
    try:
        import anthropic
    except ImportError:
        raise SystemExit("anthropic package not installed.  run: pip install anthropic")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY environment variable before running.")
    return anthropic.Anthropic(api_key=api_key)


def _image_to_base64(path: Path, max_dim: int = 1568) -> str:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _classify_one(client, model: str, frame_path: Path, catalogue: str,
                  valid_ids: set[str]) -> dict:
    img_b64 = _image_to_base64(frame_path)
    user_prompt = _USER_TEMPLATE.format(catalogue=catalogue)
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": img_b64,
                }},
                {"type": "text", "text": user_prompt},
            ],
        }],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    result = json.loads(raw)
    st = (result.get("screen_type") or "").strip()
    if st not in valid_ids:
        # Coerce unknown to "other" but record what Claude said in notes.
        result["notes"] = (
            f"[invalid id {st!r} coerced to other] "
            + (result.get("notes") or "")
        ).strip()
        st = "other"
    return {"screen_type": st, "notes": (result.get("notes") or "").strip()}


# ── labels.jsonl I/O ───────────────────────────────────────────────────────

def _existing_labels() -> dict:
    out: dict = {}
    if not _LABELS_PATH.exists():
        return out
    with open(_LABELS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            out[(r.get("session_id"), r.get("file"))] = r
    return out


def _append_label(record: dict) -> None:
    with open(_LABELS_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True,
                    help="session id under data/sessions/")
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--overwrite", action="store_true",
                    help="re-classify frames that already have a label")
    args = ap.parse_args()

    session_dir = _REPO / "data" / "sessions" / args.session / "frames"
    if not session_dir.exists():
        raise SystemExit(f"no such session frames dir: {session_dir}")

    types = _load_types()
    valid_ids = {t["id"] for t in types}
    catalogue = _catalogue_block(types)

    frames = sorted(session_dir.glob("*.png"))
    if args.limit is not None:
        frames = frames[: args.limit]
    existing = _existing_labels()

    print(f"session: {args.session}")
    print(f"frames found:     {len(frames)}")
    print(f"already labelled: "
          f"{sum(1 for f in frames if (args.session, f.name) in existing)}")
    print(f"types in catalogue: {len(valid_ids)}")
    print(f"model: {args.model}  (dry-run={args.dry_run})")
    print()

    client = None if args.dry_run else _make_client()
    n_done = 0
    n_skipped = 0
    n_failed = 0
    start = time.monotonic()

    for f in frames:
        key = (args.session, f.name)
        prev = existing.get(key)
        if prev and not args.overwrite:
            # Any prior label — human, claude-screen-type, claude (sea-view),
            # claude-minimap, etc. — means screen_type is already set
            # somewhere in history, so skip.
            n_skipped += 1
            continue
        try:
            t0 = time.monotonic()
            if args.dry_run:
                result = {"screen_type": "(dry-run)", "notes": "(dry-run)"}
            else:
                result = _classify_one(client, args.model, f, catalogue, valid_ids)
            dt = time.monotonic() - t0
            print(f"  {f.name:30}  {dt:4.1f}s  -> {result['screen_type']}")
            if result["notes"]:
                print(f"    notes: {result['notes'][:140]}")
            if not args.dry_run:
                record = {
                    "session_id":  args.session,
                    "file":        f.name,
                    "screen_type": result["screen_type"],
                    "labeled_at":  datetime.now(timezone.utc).isoformat(),
                    "labeled_by":  "claude-screen-type",
                    "tags":        [],
                }
                if result["notes"]:
                    record["notes"] = result["notes"]
                _append_label(record)
            n_done += 1
        except Exception as e:
            n_failed += 1
            print(f"  {f.name}: FAILED  {type(e).__name__}: {e}")

    elapsed = time.monotonic() - start
    print()
    print(f"done.  classified {n_done}, skipped {n_skipped}, failed {n_failed} "
          f"in {elapsed:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
