#!/usr/bin/env python
"""World-map projection calibration probe.

Run this once with the game's world map open.  It:

  1. Captures (or loads) one screenshot.
  2. Runs EasyOCR over the full frame.
  3. For every visible token, tries to match it against the 224-port
     catalogue (`memory/knowledge/world_map/port_coordinates.json`)
     using both fuzzy_contains and token_sim — same matching primitives
     `_find_port_on_world_map` already uses.
  4. Prints a table: `port | game_x | game_y | pixel_cx | pixel_cy`.
  5. From every pair of matched ports, derives `pixels_per_game_x` and
     `pixels_per_game_y` and reports the spread.

If the projection is roughly linear, the per-pair ratios will cluster
tightly (e.g. CV < 5%).  Wider spread → non-linear projection or a
warped region; we'd then need to fit a 2D affine transform instead.

Usage:
    python tools/world_map_calibrate.py                 # live screenshot
    python tools/world_map_calibrate.py path/to/img.png # cached image

The script doesn't tap anything — safe to run while the bot is idle.
"""

from __future__ import annotations

import json
import statistics
import sys
from itertools import combinations
from pathlib import Path

# Ensure the project root is on sys.path so `actions.sail_actions`, `utils.fuzzy`
# and `capture.adb_capture` import correctly regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PIL import Image


def _load_catalogue() -> dict:
    p = Path("memory/knowledge/world_map/port_coordinates.json")
    return json.loads(p.read_text())


def _strip_diacritics(s: str) -> str:
    """Fold characters with diacritics to their ASCII base.  EasyOCR
    consistently misreads 'ø'→'o', 'å'→'a', 'ñ'→'n', etc.; the
    catalogue keeps the proper Unicode names (Vardø, Malmö, São Jorge).
    Folding both sides during matching closes the gap."""
    import unicodedata
    decomposed = unicodedata.normalize("NFD", s)
    # ø and ł don't decompose; handle them explicitly.
    pre = decomposed.replace("ø", "o").replace("Ø", "O").replace("ł", "l").replace("Ł", "L")
    return "".join(c for c in unicodedata.normalize("NFD", pre)
                   if unicodedata.category(c) != "Mn")


# Event-banner phrases.  The world map shows server-wide event tickers
# like 'Maca Boom occurred in Yeongil |' across the top.  These mention
# port names but are not located at the port — they're announcements.
# A label containing any of these phrases must NOT be used for port
# matching even if it contains a recognised port name.
#
# Origin: 2026-05-12 calibration pass — Yeongil in 'Maca Boom occurred
# in Yeongil' was being treated as a 'Yeongil is at this pixel' data
# point, destroying Frame 6/7 calibration with a 4000-game-unit
# false-positive jump.
_EVENT_BANNER_PHRASES = (
    "occurred", "ongoing", "ended",   # event ticker verbs
    "boom",                            # event type names — no port has these
    "festival", "plague", "flood",
    "war ", " war",                     # 'War' alone could be a substring risk
    "sponsor", "development",
    "extravagance",
    "maca",                            # currency / event hint
)


def _is_event_banner(text: str) -> bool:
    """True if the text looks like an event-ticker announcement (not a
    port-on-map label).  Conservative — only fires when phrases that
    cannot appear in a port name itself are present."""
    text_low = text.lower()
    return any(p in text_low for p in _EVENT_BANNER_PHRASES)


# World-map UI chrome tokens that look like port-name fragments but
# are not — these MUST be rejected before port matching.  Found via the
# 2026-04-14 frame OCR (`Port` mode tab at pix_cy=50 was matching
# catalogue `Porto` via token_sim).
_WORLD_MAP_CHROME_TOKENS = frozenset({
    # Mode tabs at top of world map
    "explore", "port", "route", "trade",
    # Title / corner labels
    "world", "map", "world map",
    # Bottom-left controls
    "filter", "my location", "trade event", "trade event schedule",
    "schedule",
    # City-info / sail panel buttons
    "search", "go to city", "go to", "set sail", "depart",
    # Common direction / season labels that drift across the map
    "north", "south", "east", "west",
})


def _match_token_to_port(text: str, ports: dict, port_aliases: dict) -> str | None:
    """Return the canonical lowercased port key best matching *text*, or None.

    Matching tactics (tightened after smoke test caught 'Port Roy' ≈ 'porto'):
      - Exact substring → score 1.0
      - token_sim ≥ 0.88 with length sanity → score = sim
      - Otherwise reject

    Higher token_sim threshold than the runtime port-search (0.85) because
    the calibration phase needs precision; one wrong match poisons the
    pixels-per-game-unit estimate.  False negatives just reduce the
    number of calibration pairs, which is fine — we usually have 5-15
    visible labels per frame.

    Always checks the alias table (e.g. 'Lisboa' for Lisbon) so the same
    local-language names that _find_port_on_world_map recognises work
    here too.
    """
    from utils.fuzzy import token_sim
    text_low = text.lower().strip()
    if len(text_low) < 3:
        return None
    if text_low in _WORLD_MAP_CHROME_TOKENS:
        return None
    text_fold = _strip_diacritics(text_low)
    SIM_THRESHOLD = 0.88
    best_key, best_score = None, 0.0
    for key, info in ports.items():
        names = [info["name"].lower(), key]
        for canon, variants in port_aliases.items():
            if canon == key or key in variants:
                for v in variants:
                    if v not in names:
                        names.append(v)
        for n in names:
            n_fold = _strip_diacritics(n)
            # Exact substring match — candidate must be ≥4 chars and the
            # text shouldn't run far beyond the name (rejects 'port' ⊂
            # 'porto' situations because of the length guard below).
            if len(n_fold) >= 4 and n_fold in text_fold and len(text_fold) <= len(n_fold) + 4:
                score = 1.0
            else:
                sim = token_sim(text_fold, n_fold)
                if sim < SIM_THRESHOLD:
                    continue
                if len(text_fold) < len(n_fold) * 0.6:
                    continue   # text too short — likely partial
                # Reject text strictly shorter than name when name itself
                # is short: 'port' (4) ≈ 'porto' (5) at sim ≈ 0.89 would
                # otherwise pass.  For names ≤ 6 chars, require text to
                # be at least as long as the name.
                if len(n_fold) <= 6 and len(text_fold) < len(n_fold):
                    continue
                score = sim
            if score > best_score:
                best_score, best_key = score, key
    return best_key


def _ocr_full_frame(frame: Image.Image) -> list[tuple[str, float, int, int]]:
    """Returns (text, conf, cx, cy) tuples from EasyOCR over the full frame.
    Kept for the legacy --ocr mode; the new default path uses OmniParser."""
    from actions.sail_actions import _ocr_frame
    return _ocr_frame(frame, min_conf=0.30)


def _omniparser_parse_frame(frame: Image.Image) -> list:
    """Returns the full list of OmniParser DetectedElement objects for the
    frame.  Same cache the runtime uses, so this is cheap on warm calls."""
    from vision.omniparser import get_omniparser, parse_fast_cached
    if not get_omniparser().yolo_available():
        raise RuntimeError("OmniParser YOLO not available — cannot run calibrator")
    return parse_fast_cached(frame)


def _nearest_icon(label_cx: int, label_cy: int, icons: list,
                   max_dist: int = 80) -> object | None:
    """Find the [icon] element closest to (label_cx, label_cy) within
    max_dist pixels.  Port markers sit AT or BELOW the label — most
    typically below — so we slightly prefer icons in the downward half.

    Returns the DetectedElement or None.
    """
    best, best_score = None, float("inf")
    for ic in icons:
        dx = ic.cx - label_cx
        dy = ic.cy - label_cy
        # Down-bias: subtract a small reward for icons that sit below the label.
        # Empirically port markers in UWO are placed slightly below their name.
        bias = -5 if dy > 0 else 0
        dist = (dx * dx + dy * dy) ** 0.5 + bias
        if dist > max_dist:
            continue
        if dist < best_score:
            best_score, best = dist, ic
    return best


def _split_merged_label(
    element,
    frame: Image.Image,
    ports: dict,
    port_aliases: dict,
) -> list[tuple[str, int, int, str]]:
    """When an OmniParser element's label matches multiple catalogue
    ports (e.g. 'ICape Verde Bathurst Bissau Sierra Leone' on Frame 6
    fused four labels into one button), fall back to EasyOCR on a tight
    crop of the element's bbox.  EasyOCR returns each label as a
    separate row with its own bbox, which we can then match individually.

    Returns list of (port_key, sub_cx, sub_cy, sub_text) — pixel
    positions are in full-frame coordinates.  Empty list when the
    split didn't yield additional matches.

    Origin: 2026-05-12 empirical pass on 7 labelled world-map frames.
    OmniParser's YOLO + Florence-2 fusion absorbs nearby labels into
    one element when they're spatially close; this hybrid OmniParser→
    OCR pattern is the architectural principle 'OmniParser on whole
    screen, OCR on crops only'.
    """
    # Quick check: does this label contain >=2 catalogue port names?
    hits_in_label = _ports_named_in_text(element.label, ports, port_aliases)
    if len(hits_in_label) < 2:
        return []

    # Pad the crop slightly so EasyOCR sees enough context.
    PAD = 12
    x1 = max(0, element.x1 - PAD)
    y1 = max(0, element.y1 - PAD)
    x2 = min(frame.width,  element.x2 + PAD)
    y2 = min(frame.height, element.y2 + PAD)
    crop = frame.crop((x1, y1, x2, y2))

    # Run EasyOCR on the crop only — the original frame's full-frame
    # OCR would have collapsed the labels into the same merged regions
    # OmniParser saw.  A tighter crop gives EasyOCR cleaner spacing.
    from actions.sail_actions import _ocr_frame
    sub_tokens = _ocr_frame(crop, min_conf=0.25)

    found: list[tuple[str, int, int, str]] = []
    for text, conf, sub_cx, sub_cy in sub_tokens:
        key = _match_token_to_port(text, ports, port_aliases)
        if not key:
            continue
        # Translate crop coords back to full-frame coords
        abs_cx = sub_cx + x1
        abs_cy = sub_cy + y1
        found.append((key, abs_cx, abs_cy, text))
    return found


def _ports_named_in_text(text: str, ports: dict, port_aliases: dict) -> list[str]:
    """Return list of catalogue port keys whose name appears in *text*.
    Used to detect merged-label elements (multiple ports in one OmniParser
    label).  Treats each port name independently; the matching here is
    deliberately permissive (substring + alias) because we're just
    detecting a fusion, not picking a tap target.

    Skips event-banner text — 'Maca Boom occurred in Yeongil' must NOT
    be treated as 'Yeongil is on screen here'."""
    if _is_event_banner(text):
        return []
    text_low = _strip_diacritics(text.lower())
    hits: list[str] = []
    for key, info in ports.items():
        names = [info["name"].lower(), key]
        for canon, variants in port_aliases.items():
            if canon == key or key in variants:
                for v in variants:
                    if v not in names:
                        names.append(v)
        for n in names:
            n_fold = _strip_diacritics(n)
            if len(n_fold) >= 4 and n_fold in text_low and key not in hits:
                hits.append(key)
                break
    return hits


def _print_table(matches: list[dict]) -> None:
    if not matches:
        print("  (no port labels matched)")
        return
    matches.sort(key=lambda m: (m["game_x"], m["game_y"]))
    print(f"  {'port':<18} {'game_x':>7} {'game_y':>7}    "
          f"{'label_xy':<13}  {'icon_xy':<13}  {'tag':<7}  {'label_text':<22}")
    print(f"  {'-'*18} {'-'*7} {'-'*7}    {'-'*13}  {'-'*13}  {'-'*7}  {'-'*22}")
    for m in matches:
        label_str = f"({m['label_cx']:>5},{m['label_cy']:>5})"
        icon_str  = (f"({m['icon_cx']:>5},{m['icon_cy']:>5})"
                     if m.get("icon_cx") is not None else "  -- none --")
        print(f"  {m['port']:<18} {m['game_x']:>7} {m['game_y']:>7}    "
              f"{label_str:<13}  {icon_str:<13}  {m['etype']:<7}  {m['ocr_text']!r:<22}")


def _analyse_pairs(matches: list[dict], pos_field: str = "label") -> None:
    """For every pair, derive pixels_per_unit on each axis and report spread.

    pos_field: 'label' uses label_cx/cy; 'icon' uses icon_cx/cy.  Matches
    without an icon are excluded automatically when pos_field='icon'.
    """
    cx_key = f"{pos_field}_cx"
    cy_key = f"{pos_field}_cy"
    matches = [m for m in matches if m.get(cx_key) is not None]
    print()
    print(f"Pairwise calibration (using {pos_field}_cx/{pos_field}_cy, "
          f"{len(matches)} ports):")
    if len(matches) < 2:
        print("  Need at least 2 matched ports to compute a scale.")
        return

    ratios_x, ratios_y = [], []
    crosstalk_x_from_y, crosstalk_y_from_x = [], []
    sample_lines = []
    for a, b in combinations(matches, 2):
        dgx = b["game_x"] - a["game_x"]
        dgy = b["game_y"] - a["game_y"]
        dpx = b[cx_key] - a[cx_key]
        dpy = b[cy_key] - a[cy_key]
        if abs(dgx) >= 50:
            rx = dpx / dgx
            ratios_x.append(rx)
            cross_y = dpy / dgx  # how much does pix_y move per Δgame_x?
            crosstalk_y_from_x.append(cross_y)
        else:
            rx, cross_y = None, None
        if abs(dgy) >= 50:
            ry = dpy / dgy
            ratios_y.append(ry)
            cross_x = dpx / dgy
            crosstalk_x_from_y.append(cross_x)
        else:
            ry, cross_x = None, None
        sample_lines.append(
            f"  {a['port']:<14} → {b['port']:<14}  "
            f"Δgame=({dgx:+5d},{dgy:+5d})  Δpix=({dpx:+5d},{dpy:+5d})  "
            f"px/x={rx if rx is None else f'{rx:+.3f}'}  "
            f"px/y={ry if ry is None else f'{ry:+.3f}'}"
        )

    for line in sample_lines[:25]:
        print(line)
    if len(sample_lines) > 25:
        print(f"  ... ({len(sample_lines)-25} more pairs not shown)")

    def _summarise(name: str, vals: list[float]) -> None:
        if not vals:
            print(f"    {name}: (no pairs)")
            return
        mean   = statistics.fmean(vals)
        stdev  = statistics.pstdev(vals) if len(vals) >= 2 else 0.0
        cv     = (stdev / abs(mean) * 100) if mean else float("inf")
        print(f"    {name}: n={len(vals):>2}  mean={mean:+.4f}  stdev={stdev:.4f}  "
              f"CV={cv:.1f}%  min={min(vals):+.4f}  max={max(vals):+.4f}")

    print()
    print("Aggregate (pixels per game-coord unit):")
    _summarise("Δpix_x / Δgame_x", ratios_x)
    _summarise("Δpix_y / Δgame_y", ratios_y)

    print()
    print("Cross-axis crosstalk (should be near zero for a flat-rectangular map):")
    _summarise("Δpix_y / Δgame_x", crosstalk_y_from_x)
    _summarise("Δpix_x / Δgame_y", crosstalk_x_from_y)

    print()
    if ratios_x and ratios_y:
        cv_x = statistics.pstdev(ratios_x) / abs(statistics.fmean(ratios_x)) * 100 if len(ratios_x) >= 2 else 0
        cv_y = statistics.pstdev(ratios_y) / abs(statistics.fmean(ratios_y)) * 100 if len(ratios_y) >= 2 else 0
        verdict = (
            "LINEAR — single scale per axis will work"
            if cv_x < 5.0 and cv_y < 5.0
            else "NOT linear — fit a 2D affine transform (or per-region)"
        )
        print(f"Linearity verdict (CV<5%): x={cv_x:.1f}%  y={cv_y:.1f}%  →  {verdict}")


def main(argv: list[str]) -> int:
    cat = _load_catalogue()
    ports = cat["ports"]
    print(f"Loaded {len(ports)} ports from catalogue.")

    # Aliases used by _find_port_on_world_map — keep in sync so the same
    # local-language names (Lisboa, Las Palmas) match here too.
    try:
        from actions.sail_actions import _PORT_ALIASES as port_aliases
    except Exception:
        port_aliases = {}

    if len(argv) >= 2:
        img_path = Path(argv[1])
        print(f"Loading screenshot from {img_path}")
        frame = Image.open(img_path).convert("RGB")
    else:
        from capture.adb_capture import capture_screen
        print("Capturing fresh screenshot via ADB…")
        frame = capture_screen()
    print(f"Frame size: {frame.width}×{frame.height}")
    print()

    print("Parsing frame via OmniParser …")
    elements = _omniparser_parse_frame(frame)
    print(f"  {len(elements)} OmniParser elements total.")
    type_counts: dict[str, int] = {}
    for el in elements:
        type_counts[el.element_type] = type_counts.get(el.element_type, 0) + 1
    print(f"  By type: {type_counts}")

    # Map-area crop bounds (empirically derived from the labelled frames).
    # Excludes top chrome (mode tabs), bottom controls, right-side panels
    # and the bottom-right legend.
    #
    # Y_MIN=80 captures port labels sitting just below the mode-tab row
    # (e.g. Frame 5's Amsterdam at y=87, Frame 7's Bathurst at y=54).
    # The mode tabs themselves are at y≈30-70.
    X_MIN, Y_MIN, X_MAX, Y_MAX = 50, 80, 1860, 950
    map_elements = [
        el for el in elements
        if X_MIN <= el.cx <= X_MAX and Y_MIN <= el.cy <= Y_MAX
    ]
    print(f"  After map-area crop ({X_MIN},{Y_MIN})..({X_MAX},{Y_MAX}): "
          f"{len(map_elements)} elements remain.")

    # Separate by type.  'text' and 'button' carry OCR-read labels; 'icon'
    # are graphical-only.
    text_like  = [el for el in map_elements
                  if el.element_type in ("text", "button")]
    icon_like  = [el for el in map_elements if el.element_type == "icon"]
    print(f"  Text-like (text/button): {len(text_like)}  Icons: {len(icon_like)}")
    print()

    # Dump every text-like label so we can see what OmniParser read.
    print("  Text-like elements (sorted by cy, cx):")
    for el in sorted(text_like, key=lambda e: (e.cy, e.cx)):
        print(f"    [{el.element_type:6s}] {el.cx:>5},{el.cy:>5}  {el.label!r}")
    print(f"  Icons in map area: {len(icon_like)}  positions: "
          + ", ".join(f"({e.cx},{e.cy})" for e in icon_like[:30]))
    print()

    # Match each text-like element to a catalogue port.
    #
    # Two paths:
    #   A. The element's label matches exactly one catalogue port → use
    #      OmniParser's element center directly (fast path).
    #   B. The label matches MULTIPLE catalogue ports (Frame-6 fusion
    #      style: 'ICape Verde Bathurst Bissau Sierra Leone' merged into
    #      one button) → re-OCR the element's bbox crop via EasyOCR to
    #      get per-port sub-positions (slow path, called only when
    #      needed).  This is the "OmniParser on whole-screen, OCR on
    #      crops" pattern.
    best_per_port: dict[str, dict] = {}
    split_recoveries = 0
    for el in text_like:
        named = _ports_named_in_text(el.label, ports, port_aliases)
        if not named:
            continue

        if len(named) == 1:
            # Fast path: single port, use OmniParser bbox center directly.
            key = named[0]
            icon = _nearest_icon(el.cx, el.cy, icon_like, max_dist=80)
            rec = {
                "port":      ports[key]["name"],
                "key":       key,
                "game_x":    ports[key]["x"],
                "game_y":    ports[key]["y"],
                "label_cx":  el.cx,
                "label_cy":  el.cy,
                "icon_cx":   icon.cx if icon else None,
                "icon_cy":   icon.cy if icon else None,
                "ocr_text":  el.label,
                "etype":     el.element_type,
            }
            if key not in best_per_port:
                best_per_port[key] = rec
            elif rec["icon_cx"] is not None and best_per_port[key]["icon_cx"] is None:
                best_per_port[key] = rec
        else:
            # Slow path: merged element with ≥2 port names.  Re-OCR the
            # element's bbox crop to recover individual label positions.
            sub_matches = _split_merged_label(el, frame, ports, port_aliases)
            if sub_matches:
                split_recoveries += 1
                print(f"  [split] '{el.label}' @({el.cx},{el.cy}) "
                      f"→ {len(sub_matches)} sub-labels via EasyOCR crop")
            for sub_key, sub_cx, sub_cy, sub_text in sub_matches:
                icon = _nearest_icon(sub_cx, sub_cy, icon_like, max_dist=80)
                rec = {
                    "port":      ports[sub_key]["name"],
                    "key":       sub_key,
                    "game_x":    ports[sub_key]["x"],
                    "game_y":    ports[sub_key]["y"],
                    "label_cx":  sub_cx,
                    "label_cy":  sub_cy,
                    "icon_cx":   icon.cx if icon else None,
                    "icon_cy":   icon.cy if icon else None,
                    "ocr_text":  sub_text,
                    "etype":     "split",
                }
                if sub_key not in best_per_port:
                    best_per_port[sub_key] = rec

    if split_recoveries:
        print(f"  Merged-label splits recovered: {split_recoveries}")

    matches = list(best_per_port.values())
    icon_count = sum(1 for m in matches if m["icon_cx"] is not None)
    print(f"Matched {len(matches)} catalogue ports "
          f"({icon_count} with paired icon):")
    print()
    _print_table(matches)
    # Calibrate using LABEL positions first (always available), then ICON
    # positions (subset with paired icons) and compare CV.
    _analyse_pairs(matches, pos_field="label")
    _analyse_pairs(matches, pos_field="icon")
    print()

    if matches:
        print("Hints:")
        print("  • If CV is low on both axes, you can use a single scale factor:")
        print("      swipe_px_dx = (target.x - center.x) * mean(Δpix_x / Δgame_x)")
        print("      swipe_px_dy = (target.y - center.y) * mean(Δpix_y / Δgame_y)")
        print("  • Re-run the script after a pan to confirm the scale is stable")
        print("    across views.  If it drifts, the map zoom changed mid-session.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
