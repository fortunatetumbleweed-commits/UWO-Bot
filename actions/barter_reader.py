# actions/barter_reader.py
# Village barter-panel reader (#12).
#
# Parses the village Barter panel into the live barter state + a partial recipe:
#   • Village  — amity grade + points (from 'Amity(Grade)' + the N/M points bar)
#   • Recipe   — the SELECTED good's output quantity, amity change, and required
#     materials (have/need), for the barter quantity solver (#19).
#
# Reliably OCR-able fields only.  The round-slot row and the locked-vs-eligible
# goods grid are icon/badge driven and belong to the badge detector (#15); this
# reader deliberately leaves rounds_remaining / locked_goods to that layer.
#
# Stateless: parses whatever barter panel is shown; returns None if not on it.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger
from PIL import Image

_AMITY_RE = re.compile(r"Amity\s*\(([A-Za-z]+)\)")
_PAIR_RE = re.compile(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)")
# THOUSANDS SEPARATORS. `\d+` stopped at the comma, so every yield of 1,000 or more was read
# as its leading digit: San Village 2026-09-04 reported `out=1` for a panel showing "Trade
# Quantity 1,122(+522)". Its neighbours here already allowed commas, which is what made the
# odd one out easy to miss.
_TRADE_QTY_RE = re.compile(r"Trade Quantity\s*(\d[\d,]*)")
_AMITY_CHANGE_RE = re.compile(r"Total Amity Change\s*([+-][\d,]*\d)")

# Category words that appear as material tile labels but aren't a good name.
_BARTER_CHROME = {"food", "crafts", "liquor", "luxuries", "metal", "trade",
                  "material", "goods", "barter", "amity", "negotiation"}


def _to_int(s: str) -> Optional[int]:
    s = (s or "").replace(",", "").replace(" ", "")
    return int(s) if s.lstrip("+-").isdigit() else None


@dataclass
class BarterMaterial:
    """One required input material shown on the panel: how much we HAVE vs NEED."""
    label: str            # OCR category/name under the tile (may be a category)
    have: int
    need: int


@dataclass
class BarterPanelReading:
    amity_grade: Optional[str] = None
    amity_points: Optional[tuple] = None          # (current, max)
    selected_good: Optional[str] = None
    output_quantity: Optional[int] = None         # units yielded per round
    total_amity_change: Optional[int] = None       # signed
    materials: list = field(default_factory=list)  # list[BarterMaterial]

    def to_village(self, name: str, **overrides):
        """Build a barter_kb.Village from the live amity state (rounds/goods left
        to #15's badge detector)."""
        from memory.barter_kb import Village
        pts = self.amity_points or (None, None)
        return Village(name=name, amity=self.amity_grade, amity_points=pts[0],
                       **overrides)

    def to_recipe(self, villages=None, season: str = ""):
        """Build a PARTIAL barter_kb.BarterRecipe for the selected good — output
        yield + material needs from the panel.  Material NAMES may be categories
        (tile OCR); source_ports come from the ingestion compiler (#28), not here."""
        from memory.barter_kb import BarterRecipe, RecipeInput
        if not self.selected_good:
            return None
        inputs = [RecipeInput(material=m.label, ratio=m.need) for m in self.materials]
        out = {}
        if self.amity_grade and self.output_quantity is not None:
            out[self.amity_grade] = self.output_quantity
        return BarterRecipe(good=self.selected_good, season=season, inputs=inputs,
                            villages=list(villages or []), output_per_round=out)


def _parse_barter_panel(tokens) -> Optional[BarterPanelReading]:
    """Pure parser over (text, conf, cx, cy).  Returns None if the panel's
    signature ('Amity(...)' or 'Trade Material') isn't present."""
    texts = [(t or "").strip() for t, _c, _x, _y in tokens]
    joined = " ".join(texts)
    if "Amity(" not in joined.replace(" ", "") and "Trade Material" not in joined:
        return None

    r = BarterPanelReading()

    # Amity grade + points (left column, cx < ~1500).
    for text, _c, cx, cy in tokens:
        m = _AMITY_RE.search(text or "")
        if m:
            r.amity_grade = m.group(1)
            break
    for text, _c, cx, cy in tokens:
        if cx < 1500 and cy < 200:
            m = _PAIR_RE.search(text or "")
            if m:
                cur, cap = _to_int(m.group(1)), _to_int(m.group(2))
                if cur is not None and cap is not None:
                    r.amity_points = (cur, cap)
                    break

    # Right panel: selected good (top), output quantity, amity change.
    for text, _c, cx, cy in tokens:
        if cx > 1900 and cy < 185:
            clean = (text or "").strip()
            if (clean and clean.lower() not in _BARTER_CHROME
                    and any(ch.isalpha() for ch in clean)
                    and not _PAIR_RE.search(clean)):
                r.selected_good = clean
                break
    for text, *_ in tokens:
        m = _TRADE_QTY_RE.search(text or "")
        if m:
            r.output_quantity = _to_int(m.group(1))
        m = _AMITY_CHANGE_RE.search(text or "")
        if m:
            r.total_amity_change = _to_int(m.group(1))

    # Trade Material tiles: have/need pairs (cy ~790-830) + label below (cy ~840-875).
    mat_pairs, mat_labels = [], []
    for text, _c, cx, cy in tokens:
        if cx > 1700 and 780 <= cy <= 835:
            m = _PAIR_RE.search(text or "")
            if m:
                have, need = _to_int(m.group(1)), _to_int(m.group(2))
                if have is not None and need is not None:
                    mat_pairs.append((cx, have, need))
        elif cx > 1700 and 838 <= cy <= 880:
            clean = (text or "").strip()
            if clean and any(ch.isalpha() for ch in clean):
                mat_labels.append((cx, clean))
    for pcx, have, need in mat_pairs:
        label = ""
        if mat_labels:
            label = min(mat_labels, key=lambda z: abs(z[0] - pcx))[1]
        r.materials.append(BarterMaterial(label=label, have=have, need=need))

    return r


def read_barter_panel(frame: Image.Image) -> Optional[BarterPanelReading]:
    """Read the village Barter panel.  Returns None if not on a barter panel."""
    from actions.sail_actions import _ocr_frame
    result = _parse_barter_panel(_ocr_frame(frame, min_conf=0.3))
    if result is None:
        logger.debug("Barter panel not detected")
    else:
        logger.info(f"Barter panel: amity={result.amity_grade}{result.amity_points} "
                    f"good={result.selected_good} out={result.output_quantity} "
                    f"materials={[(m.label, m.have, m.need) for m in result.materials]}")
    return result

# ── the Trade Count strip ──────────────────────────────────────────────────────
_TRADE_COUNT_BAND = (20, 180)      # how far below the label the slot row sits


def read_trade_count(frame=None, *, elements=None) -> list:
    """The day's rounds, read off the strip along the bottom of the barter panel.

    Each SPENT round leaves a tile carrying what that round produced, so the strip is a
    ledger: `[859, 859, 898, 898]` is four rounds and their yields. Empty and locked slots
    carry no number. Measured live 2026-08-30 at Hutu Village, where the strip went
    [] -> [859, 859] -> [859, 859, 898] -> [859, 859, 898, 898] across the run.

    THIS IS THE ONLY DIRECT STATEMENT OF HOW MANY ROUNDS ARE LEFT. Everything else the bot
    used — a locked ribbon, a notice, a panel that vanished, an amity delta — is an inference
    from a side effect, and each one has been wrong at least once. That night the bot reported
    3 rounds committed and left while this strip read 4 of 6 spent, with two still available.

    Anchored on the 'Trade Count' label rather than a fixed y: the row moves with the panel.
    Returns the yields left to right, or [] when the strip is not on screen.
    """
    if elements is None:
        if frame is None:
            return []
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
    anchor = next((e for e in elements or []
                   if "trade count" in (getattr(e, "label", "") or "").strip().lower()), None)
    if anchor is None:
        return []
    lo = anchor.cy + _TRADE_COUNT_BAND[0]
    hi = anchor.cy + _TRADE_COUNT_BAND[1]
    slots = []
    for e in elements or []:
        text = (getattr(e, "label", "") or "").strip().replace(",", "")
        if text.isdigit() and lo <= getattr(e, "cy", 0) <= hi:
            slots.append((e.cx, int(text)))
    return [v for _cx, v in sorted(slots)]

