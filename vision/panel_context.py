"""Panel context reader — A2 Phase 3.

For a chromed/panel frame, identify WHICH screen it is from its **left-menu
items** matched against the tiered labeler vocabulary
(`data/knowledge/screen_tags.json`): a left menu containing
Explore/Loot/Gifting/Barter ⇒ Village; Buy/Sell ⇒ Market; Hire/Party ⇒ Inn; and
so on.

Why the menu (not a whole-screen fingerprint): the menu wording is the game's
own vocabulary, so it's stable across UI/pixel drift and game updates — exactly
the robustness the fingerprint approach lacked (a fingerprint miss is what let
village sub-screens leak to port_overworld). See
docs/a2_perceived_state_implementation_plan.md (Phase 3).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_SCREEN_TAGS = (
    Path(__file__).resolve().parents[1] / "data" / "knowledge" / "screen_tags.json"
)
_vocab_cache: Optional[dict] = None


def _norm(s: str) -> str:
    # lowercase, collapse whitespace, drop trailing OCR punctuation noise
    # ('Deposit]', 'Deposit/' → 'deposit')
    s = " ".join((s or "").strip().lower().split())
    return s.strip(" .,:;/|]}[{)(")


def _fuzzy_match(vocab_item: str, labels: set) -> bool:
    """True if any detected label matches this vocab item, tolerant of OCR noise
    and minor wording (Savings vs Saving, Deposit/ vs Deposit/Withdrawal)."""
    from difflib import SequenceMatcher
    for l in labels:
        if vocab_item == l:
            return True
        # substring either way, but only for non-trivial tokens (avoids a short
        # word like 'buy' matching inside an unrelated label)
        if len(vocab_item) >= 4 and (vocab_item in l or l in vocab_item):
            return True
        if SequenceMatcher(None, vocab_item, l).ratio() >= 0.82:
            return True
    return False


def _load_vocab() -> dict:
    """context name → set of normalised menu-item labels.

    Villages come from the `village` screen_type; buildings from the
    `sub_menu` cascade's `menu_by_context` map.
    """
    global _vocab_cache
    if _vocab_cache is not None:
        return _vocab_cache
    vocab: dict[str, set] = {}
    try:
        d = json.loads(_SCREEN_TAGS.read_text(encoding="utf-8"))["screen_tags"]
    except Exception:
        _vocab_cache = {}
        return _vocab_cache
    for grp in d.get("village", []):
        items = {_norm(t["label"]) for t in grp.get("tags", []) if t.get("label")}
        if items:
            vocab["village"] = vocab.get("village", set()) | items
    for grp in d.get("sub_menu", []):
        mbc = grp.get("menu_by_context")
        if not mbc:
            continue
        for bkey, its in mbc.items():
            ctx = bkey.split(":", 1)[-1]  # "building:market" → "market"
            items = {_norm(it["label"]) for it in its if it.get("label")}
            if items:
                vocab[ctx] = vocab.get(ctx, set()) | items
    _vocab_cache = vocab
    return vocab


def _reset_vocab_cache() -> None:
    """Test hook — force a reload after editing screen_tags.json."""
    global _vocab_cache
    _vocab_cache = None


@dataclass
class PanelContext:
    context:      str                       # 'village' | 'market' | 'inn' | …
    score:        int                       # how many menu items matched
    matched:      list = field(default_factory=list)
    village_name: Optional[str] = None      # extracted when context == 'village'
    menu_item:    Optional[str] = None       # the selected menu item (active function)


def identify_context(menu_labels, min_score: int = 2) -> Optional[PanelContext]:
    """Best-matching context for a set of detected left-menu labels.

    Returns None when nothing clears `min_score` matches (≥2 avoids a single
    shared word — e.g. a lone 'Recruit Crew' — from deciding the context).
    """
    labels = {_norm(l) for l in (menu_labels or []) if _norm(l)}
    if not labels:
        return None
    vocab = _load_vocab()
    best_ctx, best_matched = None, set()
    for ctx, vset in vocab.items():
        # count vocab items that fuzzy-match some detected label (OCR-tolerant)
        m = {vi for vi in vset if _fuzzy_match(vi, labels)}
        if len(m) > len(best_matched):
            best_ctx, best_matched = ctx, m
    if best_ctx is None or len(best_matched) < min_score:
        return None

    village_name = None
    if best_ctx == "village":
        try:
            from vision.text_correction import correct_village_name
            for l in (menu_labels or []):
                v, r = correct_village_name(l)
                if v is not None and r >= 0.8:
                    village_name = v
                    break
        except Exception:
            pass
    return PanelContext(
        context=best_ctx, score=len(best_matched),
        matched=sorted(best_matched), village_name=village_name,
    )
