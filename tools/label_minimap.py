"""Add mini-map content tags to existing sea-frame labels.

The sea-view auto-labeler (`tools/auto_label_sea_capture.py`) describes
the 3D scene.  This separate pass describes the **mini-map** (top-right
radar) which can show things the 3D camera doesn't — that disagreement
is itself the leading-indicator signal we want to train on.

Rather than re-running the full labeler with a bigger prompt, this tool
crops only the mini-map region from each frame, sends just that crop to
Claude, and asks ONLY about radar content.  Cheaper (smaller image,
focused prompt) and cleaner separation of concerns.

Tags emitted (multi-label) follow the `minimap:*` group in
`data/knowledge/screen_tags.json`:

    minimap:land
    minimap:port_anchor
    minimap:village_building
    minimap:settlement_name_text
    minimap:fleet_merchant
    minimap:fleet_pirate_regular
    minimap:fleet_pirate_special
    minimap:player_neutral
    minimap:player_friend
    minimap:player_guild
    minimap:player_hostile
    minimap:undiscovered_marker

Existing rows in `data/labels.jsonl` are NOT modified.  A new row per
frame is appended with the same `tags` plus the new `minimap:*` tags,
and `labeled_by="claude-minimap"`.  The labeler's last-entry-wins
lookup makes the augmented tags effective on the next page load.

Usage:
    python tools/label_minimap.py --session 2026-05-24_calais_south_v3
    python tools/label_minimap.py --session SESSION --dry-run
    python tools/label_minimap.py --session SESSION --limit 5
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


# Mini-map crop bounds in 2400×1080 frame.  Confirmed across multiple
# samples; see memory/project_minimap_as_navigation_radar.md.
# DO NOT point this at the live navigation crop. This is the region the DETECTOR MODEL is
# trained and labelled on: it is written into the checkpoint as `ckpt["minimap_crop"]` and
# read back at inference by `vision/minimap_reader.py`, so it is a contract with the trained
# weights, not a description of where the mini-map currently sits on screen. Changing it to
# follow UI drift would silently feed the model a region it was never trained on.
# The live UI crop is `vision.minimap_navigation_view.get_minimap_crop()`.
_MINIMAP_CROP = (2055, 140, 2400, 360)

# Tag catalogue — keep in sync with data/knowledge/screen_tags.json
# (Mini-map content group).
_MINIMAP_TAGS = {
    "minimap:land":                 "any white/beige land mass visible inside the radar circle",
    "minimap:port_anchor":          "white or outlined anchor icon — port marker (when port within range)",
    "minimap:village_building":     "small building sprite — village marker (when village within range)",
    "minimap:settlement_name_text": "name text rendered next to a port/village marker (e.g. 'Hamburg', 'Berber')",
    "minimap:fleet_merchant":       "white diamond — peaceful NPC merchant fleet",
    "minimap:fleet_pirate_regular": "yellow head, SIDE-FACING profile (smaller, no facial detail) — common hostile NPC pirate",
    "minimap:fleet_pirate_special": "yellow head, FRONT-FACING with mustache (larger, detailed face) — boss-tier named pirate.  ALSO appears on the world map in coloured variants (yellow / red / green / white)",
    "minimap:player_neutral":       "white/default-colour asterisk * — another real human player, unknown faction",
    "minimap:player_friend":        "orange asterisk * — friend (on the friend list)",
    "minimap:player_guild":         "green asterisk * — same guild",
    "minimap:player_hostile":       "purple asterisk * — bad-karma player (hostile / PK)",
    "minimap:undiscovered_marker":  "'???' text overlay marking an unknown location",
}

_SYSTEM_PROMPT = """\
You are inspecting a small CROP of an Uncharted Waters Origin sea-view
mini-map (the top-right radar of the sailing HUD).  Your job is to list
which sprite classes are PRESENT in this radar crop.

Important context:

  - The mini-map shows a 360° area AROUND the player ship.  It is
    rendered as a flat top-down radar with a circular sweep effect.
    Land = light beige / white-ish blobs with CRISP EDGES.  Water =
    darker translucent blue with a wave / stripe texture.
  - The player's own ship is a green ship icon at the centre.  Do NOT
    label that — it's always there.
  - The mini-map's visible area is LARGER than the 3D sea view.  Land,
    ports, fleets, etc. that you see here may not be visible in the
    main camera view; that is expected and not a labelling mistake.

LAND vs DECORATIVE CLOUDS — common false positive to avoid:
  - The water area has decorative CLOUD overlays — soft white wisps
    with FUZZY GRADIENTS that fade into the water texture.
  - Real LAND has crisp, sharply-defined edges and a uniform light
    fill.  Clouds have soft edges and fade.
  - If the lighter area lacks crisp edges, it is NOT land.

LAND — common misses to watch for:
  - SMALL ISLANDS in the middle of the radar (small circular or
    irregular light patches with crisp edges, surrounded by water).
  - PARTIAL LAND AT THE EDGE of the radar circle — a thin sliver of
    light along the perimeter is still land.  Check the whole rim.
  - Land partially hidden BEHIND the player ship icon at centre is
    still land — look for matching edges on either side of the ship.

OCCLUSION — common misses to watch for on sprite classes:
  - The PORT ANCHOR, VILLAGE BUILDING, and "???" undiscovered marker
    may be partially behind the player ship icon at the centre.  If
    half of the sprite is visible, label it.
  - The same sprites may be partially clipped at the edge of the
    radar circle.  Half-sprites along the rim still count.

The four fleet categories are visually distinct:

  - MERCHANT (`minimap:fleet_merchant`) — white diamond shape.  Common
    mid-sea traffic.
  - REGULAR PIRATE (`minimap:fleet_pirate_regular`) — small yellow
    head shown in SIDE PROFILE.  Smaller; no facial detail; no
    mustache.  **This is the DEFAULT pirate class — when in doubt,
    pick regular over special.**
  - SPECIAL PIRATE (`minimap:fleet_pirate_special`) — larger yellow
    head, FRONT-FACING, with mustache / visible facial features.
    Only tag when you can clearly see the front-facing mustachioed
    face — otherwise it's a regular pirate.
  - OTHER PLAYER (`minimap:player_*`) — asterisk `*`.  Colour matters:
    white = neutral, orange = friend, green = guild, purple = hostile.

Semantic consistency — ports and villages stand on land:

  - If you tag `minimap:port_anchor` or `minimap:village_building`,
    you MUST also tag `minimap:land`.  Ports and villages physically
    sit on land, so the anchor / building sprite implies a land mass
    is visible somewhere in the same crop.
  - EXCEPTION: `minimap:settlement_name_text` alone does NOT require
    `minimap:land`.  The name text sits BELOW the icon and can
    extend over open water; if only the name has scrolled into view
    and the icon hasn't, the land itself may still be just off-radar.

When in doubt about land vs cloud → it's cloud (skip).
When in doubt about special vs regular pirate → it's regular.
When in doubt about whether a partial sprite at edge/behind-ship
counts → it counts; label it.
"""


_USER_TEMPLATE = """\
Mini-map tag catalogue — pick ONLY from these tag ids:

{schema}

Respond with ONLY a JSON object, no markdown:

{{
  "tags": ["minimap:land", "minimap:port_anchor", "minimap:settlement_name_text"],
  "notes": "short note describing what's in the mini-map"
}}

Do not invent tag ids.  If the crop doesn't show anything notable
beyond the player ship and empty water, return an empty `tags` list.
"""


def _schema_block() -> str:
    return "\n".join(f"  {tid:35} = {desc}" for tid, desc in _MINIMAP_TAGS.items())


def _client():
    try:
        import anthropic
    except ImportError:
        raise SystemExit("anthropic package not installed.  pip install anthropic")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("Set ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=key)


def _image_b64(crop: Image.Image) -> str:
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _label_one(client, model: str, frame_path: Path) -> dict:
    img = Image.open(frame_path).convert("RGB")
    crop = img.crop(_MINIMAP_CROP)
    img_b64 = _image_b64(crop)
    user_prompt = _USER_TEMPLATE.format(schema=_schema_block())
    response = client.messages.create(
        model=model, max_tokens=350, system=_SYSTEM_PROMPT,
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
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    result = json.loads(raw)
    cleaned = [t for t in result.get("tags", []) if t in _MINIMAP_TAGS]
    return {"tags": cleaned, "notes": result.get("notes", "").strip()}


def _load_latest(labels_path: Path) -> dict:
    """Return latest record per (session_id, file)."""
    out: dict = {}
    if not labels_path.exists():
        return out
    with open(labels_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                out[(r["session_id"], r["file"])] = r
            except Exception:
                continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True,
                    help="session id under data/sessions/")
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--overwrite", action="store_true",
                    help="re-label frames that already have minimap tags")
    args = ap.parse_args()

    session_dir = _REPO / "data" / "sessions" / args.session / "frames"
    if not session_dir.exists():
        raise SystemExit(f"no such session frames dir: {session_dir}")
    labels_path = _REPO / "data" / "labels.jsonl"
    latest = _load_latest(labels_path)

    frames = sorted(session_dir.glob("*.png"))
    if args.limit is not None:
        frames = frames[: args.limit]

    targets = []
    n_nonsea_skipped = 0
    for f in frames:
        rec = latest.get((args.session, f.name))
        if rec is None:
            continue   # never labeled at all — skip; only enrich existing
        # The mini-map tag group only applies to sea-view frames.  Any
        # other screen_type (sub_menu, dialog, building_interior, etc.)
        # has no mini-map at all — sending those to Claude wastes API
        # calls and pollutes labels.jsonl with empty-tag rows.
        if rec.get("screen_type") != "sea":
            n_nonsea_skipped += 1
            continue
        existing = set(rec.get("tags", []))
        already = any(t.startswith("minimap:") for t in existing)
        if already and not args.overwrite:
            continue
        targets.append((f, rec))

    print(f"session: {args.session}")
    print(f"frames found:                {len(frames)}")
    print(f"non-sea frames skipped:      {n_nonsea_skipped}")
    print(f"already have minimap tags:   "
          f"{sum(1 for f in frames if any(t.startswith('minimap:') for t in latest.get((args.session, f.name), {}).get('tags', [])))}")
    print(f"to process this run:         {len(targets)}")
    print(f"model: {args.model}  dry-run={args.dry_run}")
    print()

    client = None if args.dry_run else _client()
    n_done = n_failed = 0
    start = time.monotonic()

    with open(labels_path, "a") as out:
        for frame_path, rec in targets:
            t0 = time.monotonic()
            try:
                if args.dry_run:
                    result = {"tags": ["(dry-run)"], "notes": ""}
                else:
                    result = _label_one(client, args.model, frame_path)
                dt = time.monotonic() - t0
                # If Claude returned no minimap tags, skip the append —
                # don't pollute labels.jsonl with empty-tag rows that
                # overwrite the original labeled_by provenance.
                if not result["tags"] and not args.overwrite:
                    tag_str = " ".join(result["tags"])
                    print(f"  {frame_path.name:30}  {dt:4.1f}s  +[{tag_str}]  (no tags — skipping write)")
                    if result.get("notes"):
                        print(f"    note: {result['notes'][:120]}")
                    n_done += 1
                    continue
                # Merge: existing tags + new minimap tags (dedup)
                merged = sorted(set(rec.get("tags", [])) | set(result["tags"]))
                # If a previous run had minimap: tags, drop them so the
                # new pass replaces them cleanly.
                if args.overwrite:
                    merged = sorted(
                        set(t for t in rec.get("tags", []) if not t.startswith("minimap:"))
                        | set(result["tags"])
                    )
                new_row = dict(rec)
                new_row["tags"] = merged
                old_note = (rec.get("notes") or "").rstrip(".")
                add = result.get("notes", "").strip().rstrip(".")
                if add:
                    new_row["notes"] = (old_note + " | minimap: " + add).strip(" |")
                new_row["labeled_at"] = datetime.now(timezone.utc).isoformat()
                new_row["labeled_by"] = "claude-minimap"
                tag_str = " ".join(result["tags"])
                print(f"  {frame_path.name:30}  {dt:4.1f}s  +[{tag_str}]")
                if result.get("notes"):
                    print(f"    note: {result['notes'][:120]}")
                if not args.dry_run:
                    out.write(json.dumps(new_row) + "\n")
                n_done += 1
            except Exception as e:
                n_failed += 1
                print(f"  {frame_path.name}: FAILED {type(e).__name__}: {e}")

    elapsed = time.monotonic() - start
    print()
    print(f"done.  labelled {n_done}, failed {n_failed} in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
