"""Remote village readers (barter-nav gaps A + B) — read a village's barter recipe
and a material's source ports from the WORLD MAP, without sailing there.

Validated live 2026-08-15 from Malé (project_village_info_readable_remotely):

  A. read_barter_ratios — the Village Info → Barter → Trade List (with the
     "View by Min. Exchange Unit" toggle OFF) shows each good with its per-round
     number: the output good's yield + each material's amount needed for one round
     (e.g. Box of Nutmeg 591 ← 136 Ebony + 204 Coral + 180 Textiles).

  B. read_material_sources — tapping a material's location 📍 icon opens a "Source"
     panel listing the Market ports that sell it (Coral → Malé, Atuona, Guam, …),
     which we record into RecipeInput.source_ports so next time it's a KB lookup.

Both readers consume OMNIPARSER elements (never raw crop+OCR) — these are chromed
panels, and OmniParser gives clean, layout-robust elements (it even keeps
"Santo Domingo" as one element where raw OCR split it). This follows the standing
rule: chromed screen ⇒ OmniParser. The pure parsers are unit-testable on element
data; the *_frame wrappers run OmniParser on a live frame.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

_INT_RE = re.compile(r"^\d[\d,]*$")
# Words that appear in the Trade List / Source panels but are not a good or a port.
_BARTER_CHROME = {"spices", "wares", "jewelry", "fabrics", "food", "textile", "metal",
                  "trade list", "closeout", "barter", "explore", "base", "village info",
                  "view by min. exchange unit", "market", "current location", "source"}


def _to_int(s: str) -> Optional[int]:
    s = (s or "").strip().replace(",", "")
    return int(s) if s.isdigit() else None


def _pos(e):
    if isinstance(e, (tuple, list)):
        return (e[-2], e[-1]) if len(e) >= 2 else (None, None)
    return getattr(e, "cx", None), getattr(e, "cy", None)


def _label(e):
    if isinstance(e, (tuple, list)):
        return str(e[0])
    return getattr(e, "label", "") or ""


def read_barter_ratios(elements, num_x_max: int = 1830, row_tol: int = 45) -> dict:
    """From the Village Info Barter Trade List: {good_name: per-round number}.

    Each row has a NUMBER in the left column (cx < num_x_max) and the good NAME to
    its right on the same row. Returns {good: number} for every row (the output good
    + each material) — the caller knows which name is the recipe's output good."""
    numbers, names = [], []
    for e in elements:
        cx, cy = _pos(e)
        lab = _label(e).strip()
        if cx is None or cy is None or not lab:
            continue
        if _INT_RE.match(lab) and cx < num_x_max:
            numbers.append((cx, cy, _to_int(lab)))
        elif lab.lower() not in _BARTER_CHROME and any(c.isalpha() for c in lab) and cx >= num_x_max:
            names.append((cx, cy, lab))
    out = {}
    for _ncx, ncy, val in numbers:
        row = [(nm, abs(cy - ncy)) for _cx, cy, nm in names if abs(cy - ncy) <= row_tol]
        if row and val is not None:
            name = min(row, key=lambda z: z[1])[0]
            out[name] = val
    return out


def read_material_sources(elements, list_x_max: int = 700,
                          top_y: int = 430, bot_y: int = 860) -> list:
    """From the material 'Source' panel (OmniParser elements): the Market port names
    that sell it, top→bottom. The Market list is the left column (cx < list_x_max)
    below the 'Market' header; chrome / 'Current Location' excluded."""
    ports = []
    for e in elements:
        cx, cy = _pos(e)
        lab = _label(e).strip()
        if cx is None or cy is None or not lab:
            continue
        if cx >= list_x_max or not (top_y <= cy <= bot_y):
            continue
        low = lab.lower()
        if low in _BARTER_CHROME or low == "icon" or "current" in low or "locati" in low:
            continue
        if not any(c.isalpha() for c in lab):
            continue
        ports.append((cy, lab))
    return [lab for _cy, lab in sorted(ports)]


# ── OmniParser frame wrappers (chromed screen ⇒ OmniParser, never raw OCR) ──────

def _omni(frame):
    from vision.omniparser import parse_fast_cached
    return parse_fast_cached(frame)


def read_barter_ratios_frame(frame) -> dict:
    """Read the village barter recipe {good: per-round number} from a live Village
    Info → Barter Trade List frame (OmniParser)."""
    return read_barter_ratios(_omni(frame))


def read_material_sources_frame(frame) -> list:
    """Read a material's Source-panel Market ports from a live frame (OmniParser)."""
    return read_material_sources(_omni(frame))
