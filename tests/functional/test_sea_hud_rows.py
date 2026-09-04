"""The sea HUD is read ROW BY ROW — a badge on the line above is not part of the number."""
import pathlib

import pytest

FRAME = pathlib.Path("data/sessions/trace_barter_cmd_2026-08-29T18-18-27/frame_0019.png")


def test_a_badge_above_the_line_is_not_part_of_the_supply():
    """LIVE 2026-08-29. One voyage reported 15, 115, 415 and 415 days of supply.

    15 was always right — the HUD says '15 Days of Sailing Left'. The supply crop reaches
    into the badge row above it, and joining the whole crop into one string put the shield's
    '20' immediately in front of the supply digits: '20 15 days of sailing left'. The regex
    takes every digit before 'days', so it swallowed both.

    Tightening the crop was tried and rejected — it clips the glyph tops and OCR fragments
    the line into '15 Days of =' / 'Sailing "' / 'Left', which matches nothing at all.
    Tokens carry positions, and the badge is simply on a different row.
    """
    if not FRAME.exists():
        pytest.skip("reference frame not present")
    from PIL import Image

    from actions.sail_actions import _HUD_SUPPLY_CROP, _hud_rows, _ocr_frame, read_sea_hud

    frame = Image.open(FRAME).convert("RGB")
    rows = _hud_rows(_ocr_frame(frame.crop(_HUD_SUPPLY_CROP), min_conf=0.25))

    assert "20" in rows, "the badge is still in the crop — it just must not join the line"
    assert any("days of sailing" in r for r in rows)
    assert not any("20 15" in r for r in rows), "the badge merged into the supply number"

    hud = read_sea_hud(frame)
    assert hud["supply_days"] == 15

    # ...and reading in POSITION order fixes 'Day N' too, which the old whole-crop join
    # returned as None because OCR emitted the tokens as '1' then 'Day'.
    assert hud["day_at_sea"] == 1
