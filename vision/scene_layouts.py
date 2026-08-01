"""Loader for `memory/knowledge/scene_layouts/*.md` — markdown
descriptions of game scenes used as layout context in Qwen prompts.

See `memory/knowledge/scene_layouts/README.md` for the file structure,
the lookup chain, and editing conventions.

This module is intentionally small: it just resolves `(nav_state,
detail, parent_building)` to a concatenated markdown string.  Improving
prompt quality happens by editing the .md files, NOT by editing this
loader.

Origin: 2026-05-13 — refactor of Qwen perception so layout context is
KB-stored and per-scene rather than hardcoded in the prompt template.
The earlier design embedded scene descriptions in `_build_prompt` as
Python f-strings; that made it hard to iterate without code changes.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from loguru import logger


_LAYOUT_ROOT = Path("memory/knowledge/scene_layouts")
_ALIASES_FILE = Path("memory/knowledge/building_aliases.json")
_SEPARATOR = "\n\n---\n\n"   # between concatenated sections in the final prompt


_aliases_cache: Optional[dict] = None


def _load_aliases() -> dict:
    """Building-name aliases — maps in-game variants to canonical
    layout slugs (e.g. 'palace' → 'castle', 'temple' → 'church').
    Loaded once; the file is small and rarely changes."""
    global _aliases_cache
    if _aliases_cache is None:
        try:
            import json
            data = json.loads(_ALIASES_FILE.read_text())
            _aliases_cache = data.get("aliases", {})
        except Exception as e:
            logger.debug(f"[scene_layouts] aliases unavailable: {e}")
            _aliases_cache = {}
    return _aliases_cache


def _canonical_building(name: Optional[str]) -> Optional[str]:
    """Resolve a building slug through the alias map, recursively but
    bounded (so a malformed self-cycle doesn't loop forever)."""
    if not name:
        return name
    aliases = _load_aliases()
    seen: set[str] = set()
    cur = name
    for _ in range(8):
        if cur in seen:
            break
        seen.add(cur)
        nxt = aliases.get(cur)
        if nxt is None or nxt == cur:
            return cur
        cur = nxt
    return cur


# ── Helpers ──────────────────────────────────────────────────────────────────


def _slug(s: Optional[str]) -> Optional[str]:
    """Lowercase, replace whitespace with `_`.  Used to map building/
    sub-menu names from detail strings to filenames."""
    if not s:
        return None
    return re.sub(r"\s+", "_", s.strip().lower())


def _parse_detail(nav_state: str, detail: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Extract (building_slug, submenu_slug) from a perceive detail string.

    Conventions seen in the codebase:
      - building:       'building: harbor', 'building: inn — ...'
      - sub_menu:       'sub_menu: recruit crew'

    Returns (None, None) when the detail isn't parseable in those forms.
    """
    if not detail or ":" not in detail:
        return None, None
    # 'building: harbor — extra notes'  →  'harbor'
    # 'sub_menu: recruit crew'          →  'recruit crew'
    head, _, rest = detail.partition(":")
    head = head.strip().lower()
    name_part = rest.split("—", 1)[0].strip()  # drop any free-text suffix
    name = _slug(name_part)
    if head == "building":
        return name, None
    if head == "sub_menu":
        return None, name
    return None, None


def _read(path: Path) -> Optional[str]:
    """Read a markdown file.  Returns its content or None on miss."""
    try:
        if path.exists() and path.is_file():
            return path.read_text()
    except Exception as e:
        logger.debug(f"[scene_layouts] read {path} failed: {e}")
    return None


# ── Public entry point ───────────────────────────────────────────────────────


def load_layout(
    nav_state: str,
    detail: Optional[str] = None,
    parent_building: Optional[str] = None,
) -> str:
    """Build the layout-context prompt for `(nav_state, detail)`.

    Lookup chain (most-general first; missing layers are skipped):

      1. `buildings/_default.md` — shared chrome (for building / sub_menu only)
      2. Scene-type file:
           - `<nav_state>.md` for non-building scenes (port_overworld,
             sea, sea_cinematic, world_map, dialog, loading, pending,
             main_menu)
           - `buildings/<bldg>/_building.md` for `nav_state="building"`
             with a known building slug in `detail`
      3. Sub-menu file (only for `nav_state="sub_menu"`):
           - `buildings/<parent_building>/<submenu>.md` when the parent
             building can be determined

    Returns an empty string when nothing matched — callers can use that
    as a "no layout context available" signal.

    Args:
        nav_state:       the coarse nav state from perceive.
        detail:          the detail string from perceive (e.g.
                         `"building: harbor"`).  Used to extract the
                         building or sub-menu slug.
        parent_building: when `nav_state="sub_menu"`, the parent
                         building's slug.  If None, the loader falls
                         back to the building default only.
    """
    parts: list[str] = []
    bldg_from_detail, submenu_from_detail = _parse_detail(nav_state, detail)

    # Layer 0: universal chrome (visible on EVERY screen — phone OS bar
    # at bottom-left, player UID + server at bottom-right).  Always
    # prepend so Qwen knows to ignore these as noise regardless of
    # which scene it's reasoning about.
    universal = _read(_LAYOUT_ROOT / "_universal.md")
    if universal:
        parts.append(universal)

    # Layer 1: default building chrome
    if nav_state in ("building", "sub_menu"):
        chrome = _read(_LAYOUT_ROOT / "buildings" / "_default.md")
        if chrome:
            parts.append(chrome)

    # Layer 2: scene-type file.  Building names are resolved through
    # the alias map first (e.g. 'palace' → 'castle', 'temple' → 'church')
    # so different in-game names that share function read the same
    # canonical layout file.  See memory/knowledge/building_aliases.json.
    if nav_state == "building":
        bldg = _canonical_building(bldg_from_detail)
        if bldg:
            bldg_file = _read(_LAYOUT_ROOT / "buildings" / bldg / "_building.md")
            if bldg_file:
                parts.append(bldg_file)
    elif nav_state == "sub_menu":
        bldg = _canonical_building(parent_building and _slug(parent_building))
        if bldg:
            bldg_file = _read(_LAYOUT_ROOT / "buildings" / bldg / "_building.md")
            if bldg_file:
                parts.append(bldg_file)
    else:
        scene_file = _read(_LAYOUT_ROOT / f"{nav_state}.md")
        if scene_file:
            parts.append(scene_file)

    # Layer 3: sub-menu specific file (canonical parent + sub-menu slug)
    if nav_state == "sub_menu":
        bldg = _canonical_building(parent_building and _slug(parent_building))
        if bldg and submenu_from_detail:
            sm_file = _read(_LAYOUT_ROOT / "buildings" / bldg
                            / f"{submenu_from_detail}.md")
            if sm_file:
                parts.append(sm_file)

    if not parts:
        return ""
    return _SEPARATOR.join(parts)


def list_layouts() -> dict[str, str]:
    """Return a mapping `relative_path -> file_content` of every .md in
    the layout tree.  Used by diagnostic scripts and tests."""
    if not _LAYOUT_ROOT.exists():
        return {}
    return {
        str(p.relative_to(_LAYOUT_ROOT)): p.read_text()
        for p in sorted(_LAYOUT_ROOT.rglob("*.md"))
    }
