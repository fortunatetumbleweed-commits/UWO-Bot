"""HUD numeric readers — ducats / cargo / crew off the game HUD.

These are the GROUND-TRUTH signals for the task executor's done-conditions and
progress checks (docs/next_phase_architecture_2026-08-09.md §3a, §4.1): buy done =
cargo up AND ducats down; recruit done = crew up. Read from the OmniParser element
inventory already produced each perceive tick (ducats/cargo), plus OCR for crew
(reusing the proven state_extractor "Fleet Crew Size" anchor).

Return None when the value isn't visible — absence is informative ("couldn't read"
!= "zero"). Fractions are of the 2400x1080 landscape frame.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

_INT_RE = re.compile(r"^\s*[\d,]+\s*$")
_PAIR_RE = re.compile(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)")


def _to_int(s: str) -> Optional[int]:
    s = (s or "").strip().replace(",", "")
    return int(s) if s.isdigit() else None


def _to_int_sep(s: str) -> Optional[int]:
    """Parse an integer treating BOTH ',' and '.' as thousands separators — OCR
    frequently reads '2,275' as '2.275'. Crew/cargo are integers, so this is safe."""
    s = (s or "").strip().replace(",", "").replace(".", "").replace(" ", "")
    return int(s) if s.isdigit() else None


def _pos(e):
    return getattr(e, "cx", None), getattr(e, "cy", None)


def read_ducats(elements, w: int = 2400, h: int = 1080) -> Optional[int]:
    """Ducat balance = leftmost number in the top-right currency cluster.

    The currency icons sit top-right (ducat, blue gem, red gem, energy). The gold
    ducat coin is the LEFTMOST of the cluster, and ducats dwarf the gem counts.
    """
    cluster = []
    for e in elements:
        cx, cy = _pos(e)
        if cx is None or cy is None:
            continue
        if cy < 0.09 * h and cx > 0.60 * w:
            v = _to_int(getattr(e, "label", "") or "")
            if v is not None:
                cluster.append((cx, v))
    if cluster:
        cluster.sort()                   # by cx; leftmost = ducat
        return cluster[0][1]
    # Fallback (e.g. Company Overview menu, where ducats are in a left panel):
    # the number nearest a 'ducat' label.
    return _nearest_int_to_keyword(elements, ("ducat",))


def _find_pairs(elements):
    """Every 'N/M' pair on screen: list of (cur, cap, cx, cy, label)."""
    out = []
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip()
        m = _PAIR_RE.search(lab)
        if not m:
            continue
        cur, cap = _to_int(m.group(1)), _to_int(m.group(2))
        cx, cy = _pos(e)
        if cur is not None and cap is not None and cx is not None:
            out.append((cur, cap, int(cx), int(cy), lab))
    return out


def _nearest_pair_to_keyword(elements, keywords) -> Optional[Tuple[int, int]]:
    """The 'N/M' pair whose element is nearest to any element whose label
    contains one of `keywords`. Returns (cur, cap) or None."""
    kw_pos = []
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip().lower()
        if any(k in lab for k in keywords):
            cx, cy = _pos(e)
            if cx is not None:
                kw_pos.append((cx, cy))
    pairs = _find_pairs(elements)
    if not pairs or not kw_pos:
        return None

    def d2(p):
        _, _, cx, cy, _ = p
        return min((cx - kx) ** 2 + (cy - ky) ** 2 for kx, ky in kw_pos)

    cur, cap = min(pairs, key=d2)[:2]
    return (cur, cap)


def _nearest_int_to_keyword(elements, keywords) -> Optional[int]:
    """The integer-valued element nearest to any element whose label contains one
    of `keywords`. Used for currencies shown as '<label> <number>' pairs."""
    kw_pos = []
    for e in elements:
        lab = (getattr(e, "label", "") or "").strip().lower()
        if any(k in lab for k in keywords):
            cx, cy = _pos(e)
            if cx is not None:
                kw_pos.append((cx, cy))
    if not kw_pos:
        return None
    best, best_d = None, None
    for e in elements:
        v = _to_int(getattr(e, "label", "") or "")
        if v is None:
            continue
        cx, cy = _pos(e)
        if cx is None:
            continue
        d = min((cx - kx) ** 2 + (cy - ky) ** 2 for kx, ky in kw_pos)
        if best_d is None or d < best_d:
            best, best_d = v, d
    return best


def read_cargo(elements, w: int = 2400, h: int = 1080) -> Optional[Tuple[int, int]]:
    """(used, capacity). REQUIRES a 'cargo'/'load' anchor — cargo is context-
    dependent (market / overview), so we do NOT guess from an arbitrary N/M pair
    (that would grab crew '953/2,275' on the recruit screen)."""
    return _nearest_pair_to_keyword(elements, ("cargo", "load"))


_CREW_ANCHORS = ("crew size", "fleet crew")
_TOK_PAIR_RE = re.compile(r"(\d[\d.,]*)\s*/\s*(\d[\d.,]*)")


def read_crew(frame) -> Optional[Tuple[int, int]]:
    """(current, max) FLEET crew. OCR the frame, anchor on 'Crew Size' / 'Fleet
    Crew', and take the 'N/M' token nearest the anchor (separator-robust — OCR
    reads the thousands-comma as a period). Per-ship crew pairs sit by the ship
    rows, far from the anchor, so nearest-to-anchor picks the fleet total."""
    try:
        tokens = _ocr_tokens(frame)     # (text, conf, cx, cy)
    except Exception:
        return None
    has_anchor, pairs = False, []
    for txt, _conf, cx, cy in tokens:
        low = (txt or "").lower()
        if any(a in low for a in _CREW_ANCHORS):
            has_anchor = True
        m = _TOK_PAIR_RE.search(txt or "")
        if m:
            cur, cap = _to_int_sep(m.group(1)), _to_int_sep(m.group(2))
            # guard: a garbled fleet token like '0(+9531/2.275' yields cur>cap;
            # a valid crew reading has current <= capacity.
            if cur is not None and cap is not None and cur <= cap:
                pairs.append((cur, cap))
    if not has_anchor or not pairs:
        return None
    # FLEET total = the largest-capacity crew pair (it's the sum of the ships,
    # so its denominator exceeds any single ship's).
    cur, cap = max(pairs, key=lambda p: p[1])
    return (cur, cap)


def _ocr_tokens(frame):
    """OCR the frame into state_extractor's (text, conf, cx, cy) token tuples."""
    from actions.sail_actions import _ocr_frame
    return _ocr_frame(frame, min_conf=0.3)
