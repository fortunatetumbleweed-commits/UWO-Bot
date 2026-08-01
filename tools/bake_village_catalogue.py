"""Bake village_coordinates.json from voyage.tw data.

Fetches three remote files, extracts the 69 entries whose `m == "village"`
from the discovery table, joins their English names from lang_4, and
attaches per-village barter recipes from barter_arr.

Output: memory/knowledge/world_map/village_coordinates.json

The schema mirrors port_coordinates.json (same coord system, same
"villages" dict keyed by lowercased slug → {name, id, x, y, ...}).

Usage:
    python -m tools.bake_village_catalogue          # write the file
    python -m tools.bake_village_catalogue --dry    # print summary only

Re-run when voyage.tw publishes new data (typically a yearly refresh).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

_VOYAGE_JSON_URL = "https://voyage.tw/js/json.js"
_VOYAGE_LANG_URL = "https://voyage.tw/js/lang_4.js"

_OUT_PATH = Path(__file__).resolve().parent.parent \
    / "memory" / "knowledge" / "world_map" / "village_coordinates.json"


def _fetch(url: str, timeout_s: int = 30) -> str:
    """Fetch *url* as UTF-8 text.  Raises on HTTP / network error."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "uwo-bot-village-baker/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8")


def _extract_js_var(js: str, var_name: str) -> dict:
    """Pull `var <name> = {...};` from *js* and parse as JSON.

    Tolerates the file having more vars before/after on the same line —
    we anchor on `var <name> =` then balance braces ourselves.
    """
    m = re.search(rf"\bvar\s+{re.escape(var_name)}\s*=\s*", js)
    if not m:
        raise ValueError(f"var {var_name!r} not found in source")
    start = m.end()
    if js[start] != "{":
        raise ValueError(f"var {var_name!r} is not an object literal")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(js)):
        c = js[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(js[start:i + 1])
    raise ValueError(f"var {var_name!r}: unbalanced braces")


def _extract_lang_obj(js: str) -> dict:
    """`lang_js[4] = {...};` — same trick as _extract_js_var but matches
    the bracketed assignment used by lang_*.js files."""
    m = re.search(r"lang_js\[\d+\]\s*=\s*", js)
    if not m:
        raise ValueError("lang_js[N] assignment not found")
    start = m.end()
    if js[start] != "{":
        raise ValueError("lang_js[N] is not an object literal")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(js)):
        c = js[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(js[start:i + 1])
    raise ValueError("lang_js[N]: unbalanced braces")


def _slug(name: str) -> str:
    """Lowercase, ascii-ish slug used as the dict key.  Matches the
    convention in port_coordinates.json so callers can do uniform lookup
    across both catalogues.  Strips the trailing ' Village' suffix when
    present — "Bermuda Island Village" → "bermuda island"."""
    s = name.lower().strip()
    s = re.sub(r"\s+village\s*$", "", s)
    # FE0E and other variation selectors / odd punctuation in some names
    s = re.sub(r"[^a-z0-9\s_-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def bake() -> dict:
    """Fetch + parse + assemble.  Returns the catalogue dict."""
    print(f"[bake] fetching {_VOYAGE_JSON_URL}", file=sys.stderr)
    json_js = _fetch(_VOYAGE_JSON_URL)
    print(f"[bake] fetching {_VOYAGE_LANG_URL}", file=sys.stderr)
    lang_js = _fetch(_VOYAGE_LANG_URL)

    discovery = _extract_js_var(json_js, "discovery")
    barter_arr = _extract_js_var(json_js, "barter_arr")
    lang_obj = _extract_lang_obj(lang_js)

    # Reverse-lookup table: village_id → list of barter recipe ids.
    # barter_arr entries link via `v` (village discovery id).
    barters_by_village: dict[str, list[str]] = {}
    for bid, b in barter_arr.items():
        if not isinstance(b, dict):
            continue
        vid = b.get("v")
        if vid:
            barters_by_village.setdefault(vid, []).append(bid)

    villages_raw = {
        k: v for k, v in discovery.items()
        if isinstance(v, dict) and v.get("m") == "village"
    }

    catalogue: dict = {}
    seen_slugs: dict[str, str] = {}
    missing_name: list[str] = []
    for vid, v in villages_raw.items():
        name = lang_obj.get(vid)
        if not name:
            missing_name.append(vid)
            continue
        slug = _slug(name)
        if not slug:
            slug = vid.lower()
        # Disambiguate slug collisions (two villages with similar names)
        # by appending a counter suffix.
        if slug in catalogue:
            seen_slugs.setdefault(slug, slug)
            n = 2
            while f"{slug}-{n}" in catalogue:
                n += 1
            slug = f"{slug}-{n}"
        catalogue[slug] = {
            "name": name,
            "id":   vid,
            "x":    int(v["x"]),
            "y":    int(v["y"]),
            "rank": v.get("r"),
            "culture_tag": v.get("t2"),     # disfav* — affects barter
            "barters":     sorted(barters_by_village.get(vid, [])),
        }

    if missing_name:
        print(f"[bake] WARN: {len(missing_name)} village(s) have no English name",
              file=sys.stderr)

    xs = [vv["x"] for vv in catalogue.values()]
    ys = [vv["y"] for vv in catalogue.values()]
    payload = {
        "source": (
            f"voyage.tw — js/json.js (var discovery, filtered m=='village') "
            f"+ js/lang_4.js (English names), fetched "
            f"{time.strftime('%Y-%m-%d')}"
        ),
        "license_note": (
            "Data extracted from fan-made UWO reference site; "
            "for personal bot use only."
        ),
        "coord_system": {
            "description": (
                "Game-internal world coordinates, same as ports.  "
                "Higher x = east, higher y = south."
            ),
            "x_range": [min(xs), max(xs)],
            "y_range": [min(ys), max(ys)],
        },
        "schema": {
            "name":        "English display name",
            "id":          "voyage.tw discovery id (discov*)",
            "x":           "game x",
            "y":           "game y",
            "rank":        "discovery rank (string '1'-'4')",
            "culture_tag": "disfav* — culture / faction tag affecting barter",
            "barters":     "list of barter recipe ids (cross-ref barter_arr)",
        },
        "villages": dict(sorted(catalogue.items())),
    }
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry", action="store_true",
                    help="Print summary, don't write the file")
    ap.add_argument("--out", type=Path, default=_OUT_PATH,
                    help=f"Output path (default: {_OUT_PATH})")
    args = ap.parse_args()

    payload = bake()
    villages = payload["villages"]
    print(f"[bake] {len(villages)} villages baked", file=sys.stderr)
    print(f"[bake] x range: {payload['coord_system']['x_range']}", file=sys.stderr)
    print(f"[bake] y range: {payload['coord_system']['y_range']}", file=sys.stderr)
    print(f"[bake] sample: {next(iter(villages.items()))}", file=sys.stderr)

    if args.dry:
        print(json.dumps(payload, indent=2, ensure_ascii=False)[:2000])
        print("... (--dry; not written)")
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[bake] wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
