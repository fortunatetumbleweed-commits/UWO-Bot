#!/usr/bin/env python
"""Seed a `scene_layouts/<...>.md` file by asking Claude to describe a
labelled frame, given frame + OmniParser elements + role-tagged version
+ format reference.

Origin: 2026-05-13.  Frame-level seeding for the layout-context KB so
the bot has rich per-scene descriptions for Qwen without manual writing
for every building / sub-menu.

Workflow:

  # One labelled frame
  python tools/seed_layout_from_frame.py \\
      data/sessions/2026-04-14_21-49-12/frames/0007_2149462165.png

  # Dry run — print the prompt without calling Claude
  python tools/seed_layout_from_frame.py <frame> --dry-run

  # Save Claude's output directly to the target .md file
  python tools/seed_layout_from_frame.py <frame> --write

  # Batch over a list (one frame path per line, # for comments)
  python tools/seed_layout_from_frame.py --batch frames.txt --write

  # Skip frames whose target file already exists as non-stub content
  python tools/seed_layout_from_frame.py --batch frames.txt --write \\
      --skip-existing

The tool looks up the latest label for each frame in
`data/labels.jsonl`, determines the target file path from
`(screen_type, building:X tag, [action tag | notes | title-bar OCR])`,
runs `parse_screen()` for the role-tagged element list, and asks
Claude (sonnet-4-6) to write a layout file following the convention
described in `scene_layouts/README.md`.

Supports both `building_interior` and `sub_menu` frames.  For sub_menu
frames, the sub-menu name is resolved in this priority order:
  1. action:X tag on the label
  2. label.notes (when not just the parent building name)
  3. Top-left title-bar text from the parsed inventory
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PIL import Image


_LABELS_FILE = Path("data/labels.jsonl")
_LAYOUT_ROOT = Path("memory/knowledge/scene_layouts")
_FORMAT_REF  = _LAYOUT_ROOT / "buildings" / "harbor" / "_building.md"


# ── Label lookup ─────────────────────────────────────────────────────────────

def _parse_frame_path(frame_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Extract (session_id, file) from a data/sessions/<sid>/frames/<file>
    path.  Returns (None, None) for paths in unexpected shapes."""
    parts = frame_path.parts
    if "sessions" in parts:
        idx = parts.index("sessions")
        if idx + 1 < len(parts):
            return parts[idx + 1], frame_path.name
    return None, None


def _load_latest_label(session_id: str, file: str) -> Optional[dict]:
    """Return the last (most-recent) label entry for (session_id, file).
    Matches the labeler's append-then-last-wins read semantics."""
    if not _LABELS_FILE.exists():
        return None
    latest = None
    for line in _LABELS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("session_id") == session_id and d.get("file") == file:
            latest = d
    return latest


def _building_from_tags(entry: dict) -> Optional[str]:
    for t in entry.get("tags") or []:
        if isinstance(t, str) and t.startswith("building:"):
            return t[len("building:"):]
    return None


def _action_from_tags(entry: dict) -> Optional[str]:
    """Return the action:X tag value (e.g. 'purchase') when present."""
    for t in entry.get("tags") or []:
        if isinstance(t, str) and t.startswith("action:"):
            return t[len("action:"):]
    return None


def _extract_top_left_title(inventory) -> Optional[str]:
    """Find the top-left title-bar text in a parsed inventory.

    On building / sub_menu screens the title text sits immediately right
    of the back arrow at roughly (cx 200-600, cy < 100).  Returns the
    cleanest single-line label found there, or None.

    Filters out chrome (icons, currency, phone OS, etc.) — only text-like
    elements with proper-noun content survive.
    """
    from vision.element_postprocess import (
        ROLE_PHONE_OS, ROLE_BUILD_INFO, ROLE_EVENT_BANNER,
        ROLE_CHROME_ICON, ROLE_CURRENCY_LABEL,
    )
    excluded_roles = {ROLE_PHONE_OS, ROLE_BUILD_INFO, ROLE_EVENT_BANNER,
                      ROLE_CHROME_ICON, ROLE_CURRENCY_LABEL}
    candidates = []
    for t in inventory.tagged:
        if t.cy > 100 or t.cx < 100 or t.cx > 800:
            continue
        if t.omni_type not in ("text", "button"):
            continue
        if t.role in excluded_roles:
            continue
        s = (t.label or "").strip()
        if not s or len(s) > 30:
            continue
        # Title-case heuristic: at least one letter, not pure digits.
        if not any(c.isalpha() for c in s):
            continue
        candidates.append((t.cx, s))
    if not candidates:
        return None
    candidates.sort()   # leftmost wins
    return candidates[0][1]


def _slugify(s: str) -> str:
    """'Recruit Crew' → 'recruit_crew', 'Deposit/Withdrawal' →
    'deposit_withdrawal'.  Filesystem-safe: no slashes, colons,
    backslashes, or leading/trailing punctuation."""
    import re as _re
    out = s.strip().lower()
    out = _re.sub(r"[\s/\\:|,;\.]+", "_", out)
    out = _re.sub(r"_+", "_", out).strip("_")
    return out


def _resolve_target_path(label: dict, inventory=None) -> Optional[Path]:
    """Derive the .md file path this label should populate.

    For sub_menu, the sub-menu name is resolved in priority order:
      1. action:X tag on the label (most explicit when present)
      2. label.notes (when populated)
      3. Top-left title-bar text from the parsed inventory (fallback)

    Returns None when no source yields a usable sub-menu slug.
    """
    st = label.get("screen_type")
    bldg = _building_from_tags(label)

    if st == "building_interior" and bldg:
        return _LAYOUT_ROOT / "buildings" / bldg / "_building.md"

    if st == "sub_menu" and bldg:
        # Source 1: action tag (most explicit when present)
        action = _action_from_tags(label)
        if action:
            return _LAYOUT_ROOT / "buildings" / bldg / f"{action}.md"
        # Source 2: notes — but ONLY when notes contains information
        # beyond just the parent building name.  Some entries have
        # notes='Union' on a building:union frame; that's the building,
        # not the sub-menu, and is useless for naming the file.
        notes = (label.get("notes") or "").strip()
        notes_slug = _slugify(notes) if notes else ""
        if notes_slug and notes_slug != bldg:
            return _LAYOUT_ROOT / "buildings" / bldg / f"{notes_slug}.md"
        # Source 3: title-bar OCR (requires inventory)
        if inventory is not None:
            title = _extract_top_left_title(inventory)
            if title:
                title_slug = _slugify(title)
                # Same filter: skip if title is identical to building.
                if title_slug and title_slug != bldg:
                    return _LAYOUT_ROOT / "buildings" / bldg / f"{title_slug}.md"
    return None


# ── Element table for prompt ─────────────────────────────────────────────────

def _format_element_table(inventory) -> str:
    """Render TaggedElement list as a fixed-width text table.  Marks
    noise roles with a leading '·' so Claude knows to ignore them."""
    lines = [
        f"  {'role':<22} {'omni':<6} {'tap_x':>5} {'tap_y':>5}  label",
        f"  {'-'*22} {'-'*6} {'-'*5} {'-'*5}  {'-'*40}",
    ]
    for t in sorted(inventory.tagged, key=lambda x: (x.cy, x.cx)):
        marker = "·" if t.is_noise else " "
        lines.append(
            f"{marker} {t.role:<22} {t.omni_type:<6} {t.cx:>5} {t.cy:>5}  {t.label!r}"
        )
    return "\n".join(lines)


# ── Prompt template ──────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """\
You are seeding a layout-description markdown file for the UWO bot's
scene_layouts knowledge base.  These files describe the structure of
in-game scenes so the bot's perception layer can reason about WHERE
things are on screen.

You are given:
- A screenshot of an in-game scene (attached as image)
- The OmniParser-detected UI elements with their bounding boxes
- The same elements with semantic roles applied by the bot's role
  classifier (port_name / chrome_icon / right_panel_row / appellation /
  npc_bubble / etc.).  Elements marked with `·` are NOISE (NPC chatter,
  phone OS bar, build info, event banners) and should be ignored when
  describing the scene.
- An existing layout file as a format reference

Your job: produce a markdown layout description following the
convention.

==== EDITING CONVENTION ====

Required section headers:
  # <Scene name>
  ## Layout       — what's where on screen (zones + observed details)
  ## Element roles — which roles apply on this scene
  ## Hints         — non-obvious rules (e.g. "info labels are not tappable")
  ## Source frames — the labelled frame path(s) used as evidence

Anchor descriptions in what the screenshot ACTUALLY SHOWS.  If something
is unclear, write a `*(TODO: ...)*` marker — do not invent details.

Prefer qualitative positions ("top-left", "right edge", "below the
sub-menu list") over pixel coordinates.  The device is 2400×1080 so
absolute pixels would be deterministic but they're brittle to future
device changes and add little value for the LLM consumer of these files.
Use concrete coordinates only when the position is genuinely the
distinguishing signal (e.g. the appellation always at ~(1196, 375)).

For building / sub-menu screens, the chrome shared with all buildings
(back arrow top-left, building title immediately right of the back
arrow, home top-right, info labels matching "<words> N/M" are never
tappable, building-owner NPC sprite + speech bubble in centre as
ambient idle content) is documented separately in
`buildings/_default.md` — DO NOT REPEAT IT.  Focus this file on what
is unique to this specific scene.

**NPC speech bubbles and NPC sprites are NOISE.**  Note their PRESENCE
in at most ONE SENTENCE (e.g. "A building-owner NPC sprite is visible
centre-screen as ambient idle content").  Do NOT transcribe bubble
text, describe NPC appearance / clothing / gender / ethnicity, or
speculate about what the NPC is "saying".  NPC visual appearance
varies per port (different cultures, different costumes) — describing
appearance is actively misleading because the same building looks
different elsewhere.

**Speculative meanings — wrap in TODOs.**  When you see a numeric
breakdown or a labelled value whose meaning isn't explained on screen,
describe what's there without interpreting what it MEANS.  For example,
if a panel shows "722/1,847" without a row label, document the format
but mark the meaning as `*(TODO: confirm what this represents)*`.
Confident inferences about state (a button being "disabled" or "active"
based on visual style) are OK; speculative inferences about meaning
(what a numeric badge "stands for") are not.

**Use established role names** from `vision/element_postprocess.py`
when possible: `port_name`, `mode_tab`, `chrome_icon`, `currency_label`,
`right_panel_row`, `right_panel_tab`, `date_time`, `appellation`,
`player_nameplate`, `building_nameplate`, `npc_bubble`, `npc_sprite`,
`phone_os`, `build_info`, `event_banner`, `submenu_item`,
`action_button`, `info_label`, `status_badge`, `status_message`,
`panel_title`, `tappable_row`, `button`, `text`, `icon`.  If you
observe an element that doesn't fit any of these, you may propose a
new role name (descriptive snake_case) inline.

==== FORMAT REFERENCE: `buildings/harbor/_building.md` ====

The structure of the following file is the template to mirror — same
section headers, same style of bullet lists, same level of detail.
Pay attention to what it includes (sub-menu list with TODOs for
unobserved ones, element role list, hints, source frame paths).

```markdown
{format_example}
```

==== TARGET ====

Target file path:  {target_path}
nav_state:         {nav_state}
Building:          {building}
Sub-menu:          {sub_menu}
Source frame:      {source_frame}

{parent_context_note}

==== DETECTED ELEMENTS ====

OmniParser found {raw_count} raw elements; after role tagging,
{tagged_count} total ({noise_count} marked as noise).

{element_table}

==== TASK ====

Write the complete markdown file for `{target_path}`.

Output ONLY the markdown content.  Begin with the `#` heading.  Do not
wrap in code fences.  Do not include explanation or commentary outside
the markdown body.
"""


def _build_prompt(
    label: dict,
    target: Path,
    inventory,
    source_frame: str,
) -> str:
    raw_count    = len(inventory.raw_elements)
    tagged_count = len(inventory.tagged)
    noise_count  = sum(1 for t in inventory.tagged if t.is_noise)
    bldg = _building_from_tags(label) or "(unknown)"

    # For sub_menu, derive the sub-menu slug from the target file name
    # and tell Claude what to ALSO not repeat (parent building's main-view
    # content is in <bldg>/_building.md and gets prepended at runtime).
    st = label.get("screen_type")
    sub_menu_name = "(n/a — this is a building main view)"
    parent_context_note = ""
    if st == "sub_menu":
        sub_menu_name = target.stem    # e.g. 'invest', 'purchase'
        parent_context_note = (
            f"\nIMPORTANT — this is a SUB-MENU under the `{bldg}` building.  "
            f"The parent building's main-view layout is in "
            f"`buildings/{bldg}/_building.md` and is prepended at runtime "
            f"by the loader.  Focus this file on what is UNIQUE to the "
            f"`{sub_menu_name}` sub-menu — the action area, form widgets, "
            f"action buttons, info labels, etc.  Do NOT re-describe the "
            f"parent building's chrome or sub-menu list — those live in "
            f"the parent file.\n"
        )

    return _PROMPT_TEMPLATE.format(
        target_path         = str(target),
        nav_state           = st or "(unknown)",
        building            = bldg,
        sub_menu            = sub_menu_name,
        source_frame        = source_frame,
        format_example      = _FORMAT_REF.read_text(),
        raw_count           = raw_count,
        tagged_count        = tagged_count,
        noise_count         = noise_count,
        element_table       = _format_element_table(inventory),
        parent_context_note = parent_context_note,
    )


# ── Claude call ──────────────────────────────────────────────────────────────

def _call_claude(prompt: str, frame: Image.Image,
                  model: str = "claude-sonnet-4-6") -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed")

    thumb = frame.copy()
    thumb.thumbnail((1200, 540))
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return response.content[0].text


# ── Per-frame seeding (used by both single-frame and --batch modes) ──────────

def _is_stub(text: str) -> bool:
    """Heuristic: short file with a TODO marker = unfilled stub."""
    return "TODO" in text and len(text) < 200


def _process_frame(
    frame_path: Path,
    *,
    dry_run: bool,
    write: bool,
    skip_existing: bool,
) -> str:
    """Process one frame.  Returns a short status code:
        'ok'           — Claude was called, output printed (and maybe written)
        'dry'          — dry-run completed
        'skip:existing' — target exists as non-stub content, --skip-existing
        'err:<reason>' — failure (label missing, target unresolvable, etc.)
    """
    if not frame_path.exists():
        print(f"  ✗ frame not found: {frame_path}")
        return "err:not_found"

    sid, file = _parse_frame_path(frame_path)
    if not sid:
        print(f"  ✗ could not parse session id from path: {frame_path}")
        return "err:bad_path"
    label = _load_latest_label(sid, file)
    if not label:
        print(f"  ✗ no label for {sid}/{file}")
        return "err:no_label"

    print(f"Frame:    {frame_path}")
    print(f"Label:    {label.get('screen_type')!r}  tags={label.get('tags')}  "
          f"notes={label.get('notes')!r}")

    img = Image.open(frame_path).convert("RGB")
    nav_for_perceive = label.get("screen_type")
    if nav_for_perceive == "building_interior":
        nav_for_perceive = "building"

    from vision.screen_perception import parse_screen
    inventory = parse_screen(img, nav_state=nav_for_perceive)
    n_noise = sum(1 for t in inventory.tagged if t.is_noise)
    print(f"Parsed:   {len(inventory.raw_elements)} raw → "
          f"{len(inventory.tagged)} tagged "
          f"({n_noise} noise, {len(inventory.tagged) - n_noise} non-noise)")

    target = _resolve_target_path(label, inventory=inventory)
    if not target:
        print(f"  ✗ could not resolve target file from label")
        return "err:no_target"
    title_from_ocr = _extract_top_left_title(inventory)
    if title_from_ocr:
        print(f"TitleOCR: top-left text in inventory = {title_from_ocr!r}")
    print(f"Target:   {target}")
    existing_status = "new"
    if target.exists():
        existing = target.read_text()
        if _is_stub(existing):
            existing_status = "stub"
            print(f"Existing: {target} stub ({len(existing)} chars)")
        else:
            existing_status = "filled"
            print(f"Existing: {target} hand-written / filled ({len(existing)} chars)")
            if skip_existing:
                print(f"  ↷ skipping (--skip-existing)")
                return "skip:existing"

    try:
        source_frame_rel = str(frame_path.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        source_frame_rel = str(frame_path)
    prompt = _build_prompt(label, target, inventory, source_frame_rel)

    if dry_run:
        print()
        print("=" * 78)
        print(f"DRY RUN — prompt ({len(prompt)} chars):")
        print("=" * 78)
        print(prompt)
        return "dry"

    print()
    print(f"Calling Claude (prompt {len(prompt)} chars + image thumbnail) ...")
    try:
        result = _call_claude(prompt, img)
    except Exception as e:
        print(f"  ✗ Claude call failed: {type(e).__name__}: {e}")
        return f"err:claude:{type(e).__name__}"

    print()
    print("=" * 78)
    print(f"CLAUDE OUTPUT for {target}")
    print("=" * 78)
    print(result)
    print()
    print("=" * 78)

    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result if result.endswith("\n") else result + "\n")
        print(f"✓ Wrote {target}")
    else:
        print("Not writing (use --write to save).")
    return "ok"


# ── Batch mode ────────────────────────────────────────────────────────────────

def _read_batch_list(list_path: Path) -> list[Path]:
    """Read a batch file: one frame path per line.  Lines starting with
    # are full-line comments; trailing inline ` # ...` is stripped.
    Blank lines ignored.  Relative paths resolved against project root."""
    out: list[Path] = []
    if not list_path.exists():
        return out
    for raw in list_path.read_text().splitlines():
        # Strip inline comments (require whitespace before # so paths
        # containing '#' in their name aren't broken).
        if "#" in raw:
            hash_idx = raw.find("#")
            # Allow leading-whitespace comments; for inline strip on
            # whitespace-prefixed `#`.
            stripped = raw.lstrip()
            if stripped.startswith("#"):
                continue
            # Inline `<path><whitespace>#<comment>` — find `<space>#`
            ws_hash = raw.find(" #")
            tab_hash = raw.find("\t#")
            cut = min(x for x in (ws_hash, tab_hash) if x >= 0) \
                  if (ws_hash >= 0 or tab_hash >= 0) else -1
            if cut >= 0:
                raw = raw[:cut]
        line = raw.strip()
        if not line:
            continue
        p = Path(line)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        out.append(p)
    return out


def _run_batch(
    batch_path: Path,
    *,
    dry_run: bool,
    write: bool,
    skip_existing: bool,
) -> int:
    frames = _read_batch_list(batch_path)
    if not frames:
        print(f"error: no frames found in {batch_path}", file=sys.stderr)
        return 1

    print(f"Batch:   {len(frames)} frames from {batch_path}")
    print(f"Mode:    "
          + ("dry-run" if dry_run else "live Claude calls")
          + (", --write" if write and not dry_run else "")
          + (", --skip-existing" if skip_existing else ""))
    print()

    results: list[tuple[Path, str]] = []
    for i, frame_path in enumerate(frames, 1):
        print()
        print(f"########## [{i}/{len(frames)}] {frame_path.name} ##########")
        print()
        status = _process_frame(
            frame_path,
            dry_run=dry_run, write=write, skip_existing=skip_existing,
        )
        results.append((frame_path, status))

    # Summary
    print()
    print("=" * 78)
    print(f"BATCH SUMMARY  ({len(results)} frames)")
    print("=" * 78)
    from collections import Counter
    by_status = Counter(s for _, s in results)
    for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {status:<24} {n}")
    print()
    # List failures
    failures = [(p, s) for p, s in results if s.startswith("err")]
    if failures:
        print("Failures:")
        for p, s in failures:
            print(f"  {s:<28} {p}")
    return 0 if not failures else 2


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args or "--help" in args or "-h" in args:
        print(__doc__, file=sys.stderr)
        return 1

    flags = {a for a in args if a.startswith("--") and "=" not in a}
    kvflags = {a.split("=", 1)[0]: a.split("=", 1)[1]
               for a in args if a.startswith("--") and "=" in a}
    positional = [a for a in args if not a.startswith("--")]

    dry_run       = "--dry-run" in flags
    write         = "--write" in flags
    skip_existing = "--skip-existing" in flags

    # --batch path
    batch_path: Optional[Path] = None
    if "--batch" in flags:
        idx = args.index("--batch")
        if idx + 1 < len(args):
            batch_path = Path(args[idx + 1])
    elif "--batch" in kvflags:
        batch_path = Path(kvflags["--batch"])

    if batch_path is not None:
        if not batch_path.exists():
            print(f"error: batch file not found: {batch_path}",
                  file=sys.stderr)
            return 1
        return _run_batch(batch_path,
                           dry_run=dry_run, write=write,
                           skip_existing=skip_existing)

    # Single-frame mode
    # Filter out the batch path argument from positionals if present
    positional = [p for p in positional
                  if not (batch_path and Path(p) == batch_path)]
    if not positional:
        print("error: frame path required (or use --batch <list-file>)",
              file=sys.stderr)
        return 1

    status = _process_frame(
        Path(positional[0]),
        dry_run=dry_run, write=write, skip_existing=skip_existing,
    )
    return 0 if status in ("ok", "dry", "skip:existing") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
