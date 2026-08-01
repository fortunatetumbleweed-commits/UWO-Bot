#!/usr/bin/env python
"""Show the tagged output of vision/screen_perception.parse_screen on
labelled frames.

Usage:
    python tools/screen_tagger_demo.py                # default test set
    python tools/screen_tagger_demo.py path/to/img.png nav_state
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PIL import Image
from vision.screen_perception import parse_screen


# Labelled frames used in the 2026-05-13 design discussion.
_DEFAULT_FRAMES = [
    ("data/sessions/2026-04-14_21-52-17/frames/0005_2152411228.png",
     "port_overworld", "Bergen — appellation + player nameplate visible"),
    ("data/sessions/2026-04-16_20-35-45/frames/0010_2105411488.png",
     "port_overworld", "Socotra — NPC speech bubbles + building name plates"),
    ("data/sessions/2026-04-14_21-49-12/frames/0004_2149341971.png",
     "world_map", "WM #1 — Kokkola / Vardo visible"),
    ("data/sessions/2026-04-15_16-05-29/frames/0068_1614542131.png",
     "world_map", "WM #2 — NW Europe 5 ports"),
    ("data/sessions/2026-04-15_21-54-26/frames/0008_2157043344.png",
     "world_map", "WM #4 — City Info panel open on London"),
    ("data/sessions/2026-04-16_18-41-45/frames/0011_1844113328.png",
     "world_map", "WM #7 — West Africa, City Info on Abidjan"),
]


def _show_frame(path: str, nav_state: str, note: str) -> None:
    print()
    print("=" * 88)
    print(f"FRAME: {path}")
    print(f"NAV_STATE: {nav_state}")
    print(f"NOTE: {note}")
    print("=" * 88)
    img = Image.open(path).convert("RGB")
    inv = parse_screen(img, nav_state=nav_state)
    print(f"  {len(inv.raw_elements)} raw OmniParser elements → "
          f"{len(inv.tagged)} tagged")
    print(f"  By role: {inv.summary_line()}")
    print()

    # Group by role, then print elements per role
    role_order = [
        "port_name", "title_bar", "back_arrow", "hamburger",
        "mode_tab", "chrome_icon",
        "right_panel_tab", "right_panel_row", "date_time",
        "appellation", "player_nameplate", "building_nameplate",
        "event_banner", "npc_bubble", "proximity_button",
        "button", "text", "icon",
        "phone_os", "build_info",
    ]
    for role in role_order:
        items = inv.by_role.get(role, [])
        if not items:
            continue
        flag = " [NOISE]" if role in ("npc_bubble", "phone_os",
                                        "build_info", "event_banner") else ""
        print(f"  ── {role}{flag}  ({len(items)})")
        for t in sorted(items, key=lambda x: (x.cy, x.cx)):
            signals = ", ".join(t.signals)
            print(f"      {t.cx:>5},{t.cy:>5}  {t.width:>4}x{t.height:>4}  "
                  f"omni={t.omni_type:<6}  {t.label!r:<40}  [{signals}]")


def main(argv):
    if len(argv) >= 3:
        _show_frame(argv[1], argv[2],
                     note=argv[3] if len(argv) > 3 else "(custom)")
    else:
        for path, ns, note in _DEFAULT_FRAMES:
            if not Path(path).exists():
                print(f"SKIP (not found): {path}")
                continue
            _show_frame(path, ns, note)


if __name__ == "__main__":
    main(sys.argv)
