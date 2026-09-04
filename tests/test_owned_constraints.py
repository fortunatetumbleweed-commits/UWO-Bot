"""Arithmetic vetoes a misreading the readers' confidence could not.

The live case: the badge specialist read Candle as 2148 at confidence 0.6519 while
full-frame OCR read 148 at 0.9987 — and the specialist's confidence carried no signal
at all (it scored 0.6539 on a value it got RIGHT). What settles it is the hold.
"""
from vision.owned_constraints import reconcile_owned


CAP, USED, SUPPLIES = 4952, 3040, 793          # Barcelona, 2026-08-27 (water 397 + food 396)


def test_the_sum_rejects_the_candle_misread():
    """2,148 is under the 3,040 held, so the PER-GOOD bound passes it. Only the sum
    is impossible: 2,099 + 2,148 = 4,247."""
    v = reconcile_owned({"Iron": [2099], "Candle": [2148, 148]}, USED, CAP)
    assert v.values["Candle"] == 148


def test_the_exact_total_is_proof_not_preference():
    v = reconcile_owned({"Iron": [2099], "Candle": [2148, 148]}, USED, CAP,
                        supplies=SUPPLIES)
    assert v.values == {"Iron": 2099, "Candle": 148}
    assert v.exact and not v.unresolved


def test_a_per_good_bound_drops_the_impossible():
    """No single good can exceed the whole hold."""
    v = reconcile_owned({"Iron": [9999, 2099]}, USED, CAP)
    assert v.values["Iron"] == 2099


def test_the_first_candidate_wins_when_the_constraint_does_not_bind():
    """Two small goods both fit either way — arithmetic cannot separate them, so the
    caller's preferred reading survives and the good is reported unresolved."""
    v = reconcile_owned({"Candle": [148, 149]}, USED, CAP)
    assert v.values["Candle"] == 148
    assert "Candle" in v.unresolved and not v.exact


def test_nothing_fitting_is_reported_not_hidden():
    v = reconcile_owned({"Iron": [3000], "Candle": [2000]}, USED, CAP)
    assert set(v.unresolved) == {"Iron", "Candle"}
    assert "every reading is suspect" in v.reason


def test_an_unreadable_cargo_bar_applies_no_constraint():
    """A constraint may only veto when its own input is sound — otherwise it just moves
    the single point of failure."""
    v = reconcile_owned({"Candle": [2148, 148]}, 0, 0)
    assert v.values["Candle"] == 2148          # untouched; the caller still decides
    assert "no constraint applied" in v.reason


def test_the_live_frame_reads_candle_as_148():
    """End to end on the capture that produced the bug: the readers disagree, and the
    hold settles it without anyone comparing confidences."""
    from pathlib import Path

    from PIL import Image

    from vision.market_reader import read_market_page_omni

    frame = Image.open(Path(__file__).parent / "stage_suite" / "frames"
                       / "sell_grid_candle_wax_misread.png").convert("RGB")
    owned = {g.name: g.owned_qty for g in read_market_page_omni(frame, tab="sell") or []}
    assert owned["Candle"] == 148, "the wax curl was read as a leading digit again"
    assert owned["Iron"] == 2099
