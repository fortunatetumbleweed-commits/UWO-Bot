"""Auto-label sea-view capture frames via Claude Sonnet.

Reads every frame in a session directory that doesn't yet have a label
record in data/labels.jsonl, sends each to the Anthropic API with the
S/B/R/I tag schema, and writes the resulting tags + a one-line notes
field back into labels.jsonl with labeled_by="claude".

The human reviewer opens the labeller, scans the auto-labelled frames,
corrects mistakes — `labeled_by` then flips to "human" on save.

Usage:
    python tools/auto_label_sea_capture.py \
        --session 2026-05-24_capture_test_run \
        [--model claude-sonnet-4-6] \
        [--dry-run]

The schema mirrors data/knowledge/screen_tags.json's `sea` groups.
Keep them aligned — if the labeller adds a new tag, add it here too.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image

# Add repo root to sys.path so this script runs from anywhere.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


# ── Tag schema (kept in sync with data/knowledge/screen_tags.json) ──────────

SHORELINE_TAGS = {
    "shore:land_ahead":      "bow path blocked by land — would hit if going straight",
    "shore:land_port":       "land off the PORT (left) side of the SHIP — coast you're paralleling on the left",
    "shore:land_starboard":  "land off the STARBOARD (right) side of the SHIP — coast you're paralleling on the right",
    "shore:faint_distant":   "very faint distant land on the horizon — far enough that no steering decision is needed yet",
}

BEAM_TAGS = {
    "yolo:B1_beam_bright_tall": "bright + tall vertical beam — undiscovered port nearby",
    "yolo:B2_beam_dim_short":   "dim or short vertical beam — already-known port nearby",
    "yolo:B3_beam_edge":        "beam at the left or right edge of frame (not centred)",
    "yolo:B4_beam_multiple":    "multiple separate beams visible (port cluster)",
    "yolo:B5_beam_low_vis":     "beam in rain / fog / low visibility",
    "yolo:B6_beam_night":       "beam at night / dusk / very low light scene",
}

REEF_TAGS = {
    "yolo:R1_reef_ahead":       "reef directly on collision path",
    "yolo:R2_reef_side":        "reef to the side, no collision risk",
    "yolo:R3_reef_scattered":   "multiple reefs scattered around the ship",
    "yolo:R4_reef_near_coast":  "reef near the coastline (could be confused with land)",
    "yolo:R5_reef_low_light":   "reef visible at sunset / night",
    "yolo:R6_reef_fog_rain":    "reef visible through fog or rain",
}

ICE_TAGS = {
    "yolo:I1_ice_single":         "single white ice slab on water",
    "yolo:I2_ice_multiple":       "multiple ice slabs",
    "yolo:I3_ice_with_reef":      "ice and reef visible in the same frame",
    "yolo:I4_ice_varied_light":   "ice under unusual lighting (sunset / night / fog)",
}

OCCLUSION_TAGS = {
    "yolo:occ_right_panel_land":  "land partially hidden by translucent right panel",
    "yolo:occ_right_panel_beam":  "beam partially hidden by translucent right panel",
    "yolo:occ_right_panel_reef":  "reef partially hidden by translucent right panel",
    "yolo:occ_other":             "other meaningful occlusion (note in details)",
}

CONDITION_TAGS = {
    "time:day":      "the status bar under the mini-map reads a daytime label (Day / Daylight / Dawn / Dusk)",
    "time:night":    "the status bar under the mini-map reads a night label (Night / Midnight)",
    "weather:rain":  "visible rain streaks or rain particle effect in frame",
    "weather:snow":  "visible snowfall in frame",
}

RIGHT_PANEL_TAGS = {
    "right_panel:tasks":       "right panel shows the quest/task list (mate-exclusive, achievements, progress bars)",
    "right_panel:ports":       "right panel shows nearby ports list (port names with 'Approx. NN.Nkm' distances)",
    "right_panel:fleets":      "right panel shows enemy/NPC fleet list (LV N + distance + skull/flag icons, 'Use Repel' button at top)",
    "right_panel:ship_status": "right panel shows ship/fleet status (HP, supply, cargo, ship roster)",
}

SAIL_MODE_TAGS = {
    "sail:auto":   "auto-sailing — bottom-left shows a destination card with ETA/coords; rudder/anchor wheel is hidden or minimised",
    "sail:manual": "manual steering — bottom-left shows the rudder/anchor wheel and L/R arrow icons",
}


def _schema_block() -> str:
    """Render the schema as a compact prompt-friendly block."""
    def render(group_name: str, tags: dict) -> str:
        lines = [f"  {group_name}:"]
        for tid, desc in tags.items():
            lines.append(f"    {tid:35} = {desc}")
        return "\n".join(lines)

    return "\n\n".join([
        "TAG CATALOGUE — pick from these tag ids only:",
        render("Shoreline (zero or more — ship-relative binaries)", SHORELINE_TAGS),
        render("Beam (zero or more)", BEAM_TAGS),
        render("Reef (zero or more)", REEF_TAGS),
        render("Floating Ice (zero or more)", ICE_TAGS),
        render("Occlusion (zero or more)", OCCLUSION_TAGS),
        render("Right Panel (PICK EXACTLY ONE)", RIGHT_PANEL_TAGS),
        render("Sailing Mode (PICK EXACTLY ONE)", SAIL_MODE_TAGS),
        render("Conditions (one time:* and zero or more weather:*)", CONDITION_TAGS),
    ])


_SYSTEM_PROMPT = """\
You label sea-view screenshots from an Uncharted Waters Origin gameplay bot.
Every screenshot shows the ship at sea with HUD overlays.  Your job is to
assign training tags from a fixed catalogue, then write one short note.

Be CONSERVATIVE.  Only assign a tag when the corresponding feature is clearly
visible.  When in doubt, skip the tag.

Shoreline tags are SHIP-RELATIVE binaries — not screen-position labels.
A single frame can have any combination, including NONE (open sea):

    shore:land_ahead       bow is blocked — going straight would hit land
    shore:land_port        land off the PORT (left) BEAM of the ship —
                           coast you are sailing PAST on the left
    shore:land_starboard   land off the STARBOARD (right) BEAM — coast
                           you are sailing PAST on the right
    shore:faint_distant    very faint land on the horizon (far ahead) —
                           no steering decision yet

  CRITICAL distinction — screen-position is NOT the same as ship-relative:
    - A frontal wall of land that FILLS the horizon left-to-right is
      `shore:land_ahead` ONLY.  The land touches the left and right
      EDGES OF THE SCREEN but it is NOT on the ship's port or starboard
      beams — it is all directly in front of you.
    - A river / strait has land on the ship's left AND right beams with
      CLEAR WATER AHEAD — tag both `shore:land_port` and
      `shore:land_starboard`, do NOT tag `shore:land_ahead`.
    - A river that CURVES so the bow now points at the bank — that's
      all three: port + starboard + ahead.
    - A bay where land wraps from one side around the bow — that's
      `shore:land_ahead` + whichever side has the wrap.
    - A coastline you are paralleling on one side only — that single
      side tag, nothing else.
    - Open sea = no shoreline tags at all.

  Mental model: imagine the ship at the centre of a compass.  Ahead is
  12 o'clock; port is 9 o'clock; starboard is 3 o'clock.  Tag a side
  only when land sits roughly between 7-10 (port) or 2-5 (starboard).
  Land between 10-2 is ahead.

Player ship identification:
  - The label "Tumbler" (or whatever is shown directly above the
    centered ship sprite with a flag icon) is the PLAYER's own fleet
    name.  It is NOT a port, settlement, location, NPC fleet, or
    enemy ship.  Treat it as identifying the player's vessel and do
    NOT describe it as anything else in the notes.

Distinguishing notes:
  - A BEAM is a thin vertical light column rising from a port or village.
    The wide yellow rotating sweep on some frames is the lighthouse animation;
    IGNORE IT — it is not a beam-class trigger.  Also IGNORE the small
    glowing lanterns at the stern of the player's own ship — those are
    ship lights, not beams.  A beam rises from land/water, not from the ship.
  - A REEF is dark jagged rocks above the waterline; ICE is white slabs.
    They look similar at distance — only call reef when you can see dark
    rock shapes.
  - The translucent right panel can hide things behind it; flag with the
    appropriate occ_right_panel_* tag if you suspect important content
    is occluded.

Right panel — pick the one tab currently shown:
  - tasks: vertically stacked quest entries with progress bars / counters
    like "(0/3)" or "Move to <X>".
  - ports: list of port names each with "Approx. NN.Nkm".
  - fleets: list of fleet names with "LV N" and distance in km; usually a
    "Use Repel" / "Repel Support" button at the top.
  - ship_status: cargo/HP/supply readout for the player's fleet.

Sail mode — pick exactly one:
  - auto: the bottom-left HUD shows a destination card with coords or ETA
    (e.g. "58.88,10.91  ETA 1.0").  The steering wheel and L/R arrows are
    absent or de-emphasised.
  - manual: the bottom-left HUD shows the round rudder/anchor wheel with
    the left and right arrow icons next to it.

Time of day — READ THE TEXT, DON'T GUESS FROM PIXELS:
  Underneath the mini-map (top-right) there is a thin status bar reading
  "<lat>,<lon>  <Sea/Waters name>  <Time-of-day>" — the time-of-day word
  is the LAST token on the right side of that bar.  It will literally
  say "Day" / "Daylight" / "Dawn" / "Dusk" / "Night" / "Midnight".
    - If that word is Night or Midnight → tag time:night.
    - Otherwise → tag time:day.
  Do NOT infer time from sky brightness alone — sunset/dusk scenes look
  dark but are still daytime in-game.  When in doubt, read the bar.

Night-frame failure modes (the model has historically over-claimed
"time:day" and over-claimed land on the wrong side at night):
  - Dark blue regions at the edges of a night frame are usually JUST
    WATER + SKY at midnight, NOT land.  Land in a night frame is still
    a CRISP DARKER SHAPE with a distinct silhouette against the
    horizon — not a smooth gradient.
  - If you can't see a distinct silhouette, do NOT tag a shore:* side.
    "Open sea at night with vague horizon" = no shoreline tags.
  - Conversely, if there IS a silhouetted coast at night, label its
    side properly — many night frames legitimately have
    shore:land_starboard or shore:land_port and were missed previously.

Poor-weather failure modes (rain / fog):
  - Land in rain or fog still shows as a SOLID DARKER MASS at the
    horizon — not just darker water.  Look for a horizontal edge
    where the texture changes (water vs land) even if the contrast
    is low.
  - If land is partially hidden by rain/fog AND by the translucent
    right panel, ALSO add the appropriate yolo:occ_right_panel_land
    tag.
"""


_USER_TEMPLATE = """\
{schema}

Respond with ONLY a JSON object, no markdown, with this shape:

{{
  "tags": ["shore:land_port", "right_panel:ports", "sail:manual", "time:day"],
  "notes": "one short sentence describing the scene"
}}

Do not invent tag ids; use only those in the catalogue.  If unsure about
beam brightness (B1 vs B2), prefer the cautious tag and explain in notes.
"""


# ── Anthropic client ────────────────────────────────────────────────────────


def _make_client():
    try:
        import anthropic
    except ImportError:
        raise SystemExit(
            "anthropic package not installed.  run: pip install anthropic"
        )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit(
            "Set ANTHROPIC_API_KEY environment variable before running."
        )
    return anthropic.Anthropic(api_key=api_key)


def _image_to_base64(path: Path, max_dim: int = 1568) -> str:
    """Load and downscale a frame to keep token cost reasonable."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _label_one_frame(client, model: str, frame_path: Path) -> dict:
    """Single API call.  Returns {tags, notes}.  Raises on hard failure."""
    img_b64 = _image_to_base64(frame_path)
    user_prompt = _USER_TEMPLATE.format(schema=_schema_block())
    response = client.messages.create(
        model=model,
        max_tokens=400,
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
    raw = response.content[0].text.strip()
    # Strip code fences if Claude adds them despite instructions.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    result = json.loads(raw)
    # Validate that all tags come from the catalogue.
    valid = set(SHORELINE_TAGS) | set(BEAM_TAGS) | set(REEF_TAGS) \
            | set(ICE_TAGS) | set(OCCLUSION_TAGS) | set(CONDITION_TAGS) \
            | set(RIGHT_PANEL_TAGS) | set(SAIL_MODE_TAGS)
    cleaned = [t for t in result.get("tags", []) if t in valid]
    return {"tags": cleaned, "notes": result.get("notes", "").strip()}


# ── labels.jsonl I/O ────────────────────────────────────────────────────────


def _existing_labels(labels_path: Path) -> dict:
    """Map (session_id, file) → record so we don't double-label."""
    out = {}
    if not labels_path.exists():
        return out
    with open(labels_path) as f:
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


def _append_label(labels_path: Path, record: dict) -> None:
    with open(labels_path, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True,
                    help="session id under data/sessions/")
    ap.add_argument("--model", default="claude-sonnet-4-5",
                    help="anthropic model name")
    ap.add_argument("--dry-run", action="store_true",
                    help="print labels but don't write to labels.jsonl")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap on frames to label this run")
    ap.add_argument("--overwrite", action="store_true",
                    help="re-label frames that already have a claude record")
    args = ap.parse_args()

    session_dir = _REPO / "data" / "sessions" / args.session / "frames"
    if not session_dir.exists():
        raise SystemExit(f"no such session frames dir: {session_dir}")
    labels_path = _REPO / "data" / "labels.jsonl"

    frames = sorted(session_dir.glob("*.png"))
    if args.limit is not None:
        frames = frames[: args.limit]
    existing = _existing_labels(labels_path)

    print(f"session: {args.session}")
    print(f"frames found:    {len(frames)}")
    print(f"already labelled: "
          f"{sum(1 for f in frames if (args.session, f.name) in existing)}")
    print(f"model:           {args.model}  (dry-run={args.dry_run})")
    print()

    client = None if args.dry_run else _make_client()

    n_done = 0
    n_skipped = 0
    n_failed = 0
    start = time.monotonic()

    # Tag prefixes this script authors.  Used to detect "already done".
    sea_view_prefixes = ("yolo:", "time:", "weather:", "sail:", "right_panel:")

    for f in frames:
        key = (args.session, f.name)
        prev = existing.get(key)
        if prev and not args.overwrite:
            lb = prev.get("labeled_by") or ""
            prev_tags = prev.get("tags") or []
            prev_st = prev.get("screen_type")
            # 1. Human-authored is authoritative — never override.
            if lb == "human":
                n_skipped += 1
                continue
            # 2. Not a sea frame — leave alone.  A row with screen_type
            #    set by the screen-type classifier (or any earlier pass)
            #    that isn't "sea" should NOT get sea-view tags.
            if prev_st and prev_st != "sea":
                n_skipped += 1
                continue
            # 3. Already has sea-view tags from a prior run of this
            #    script — skip.  (minimap:* tags are NOT a signal we
            #    own; ignore them when deciding.)
            if any(t.startswith(sea_view_prefixes) for t in prev_tags):
                n_skipped += 1
                continue
        try:
            t0 = time.monotonic()
            if args.dry_run:
                result = {"tags": ["(dry-run)"], "notes": "(dry-run)"}
            else:
                result = _label_one_frame(client, args.model, f)
            dt = time.monotonic() - t0
            tag_str = " ".join(result["tags"])
            print(f"  {f.name:30}  {dt:4.1f}s  tags=[{tag_str}]")
            if result["notes"]:
                print(f"    notes: {result['notes']}")
            if not args.dry_run:
                # Preserve minimap:* tags from any prior row — those come
                # from the focused mini-map labeller and are out of this
                # script's scope, so overwriting them would discard work.
                prev_minimap = [
                    t for t in (prev.get("tags") if prev else []) or []
                    if t.startswith("minimap:")
                ]
                merged_tags = sorted(set(result["tags"]) | set(prev_minimap))
                record = {
                    "session_id":  args.session,
                    "file":        f.name,
                    "screen_type": "sea",
                    "labeled_at":  datetime.now(timezone.utc).isoformat(),
                    "labeled_by":  "claude",
                    "tags":        merged_tags,
                }
                if result["notes"]:
                    record["notes"] = result["notes"]
                _append_label(labels_path, record)
            n_done += 1
        except Exception as e:
            n_failed += 1
            print(f"  {f.name}: FAILED  {type(e).__name__}: {e}")

    elapsed = time.monotonic() - start
    print()
    print(f"done.  labelled {n_done}, skipped {n_skipped}, failed {n_failed} "
          f"in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
