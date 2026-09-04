"""A row's quantity is recovered from ITS OWN TILE when the whole-frame parse loses it.

Live 2026-08-27 at Svear: the panel plainly showed Birch Tree 689 <- Iron 88, Matchlock Gun
44, Candle 88. The whole-frame parse read 689, 88 and 88 but never proposed the 44 AS TEXT
— OmniParser returned a bare `icon` for that tile. The row parser drops a material with no
quantity, so the recipe read as two of three; `_target_complete` then certified that partial
read as complete and it was planned from for five runs. This is CLAUDE.md's rule with
consequences attached: whole-frame reads answer WHICH SCREEN, never HOW MANY.
"""
from pathlib import Path

from PIL import Image

from actions.village_check import parse_trade_list, trade_list_elements

FRAME = Path(__file__).parent / "stage_suite" / "frames" / "village_trade_list_badge_lost.png"


def _birch(frame):
    return next(t for t in parse_trade_list(trade_list_elements(frame))
                if t.good == "Birch Tree")


def test_all_three_materials_are_read():
    trade = _birch(Image.open(FRAME).convert("RGB"))
    assert trade.materials == {"Iron": 88, "Matchlock Gun": 44, "Candle": 88}
    assert trade.obtain == 689


def test_the_whole_frame_parse_alone_still_loses_it():
    """Pins the defect, not just the fix: if OmniParser ever reads the 44 on its own this
    test fails and the recovery can be reconsidered."""
    from vision.omniparser import parse_fast_cached
    frame = Image.open(FRAME).convert("RGB")
    assert not any("44" in (getattr(e, "label", "") or "")
                   for e in parse_fast_cached(frame)), \
        "the whole-frame parse now reads the badge — re-evaluate the tile crop"


def test_an_unreadable_tile_is_left_incomplete_not_guessed():
    """Naverslojd is cut off at the panel's bottom edge. A row that cannot be read must
    stay missing — inventing a quantity is what a partial recipe is made of."""
    frame = Image.open(FRAME).convert("RGB")
    goods = {t.good for t in parse_trade_list(trade_list_elements(frame))}
    assert "Naverslojd" not in goods or not any(
        t.materials for t in parse_trade_list(trade_list_elements(frame))
        if t.good == "Naverslojd")
