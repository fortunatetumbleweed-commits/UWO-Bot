"""The Exchange button decides whether another barter round is possible.

A grey Exchange and a live one are different PIXELS. The panel classifier asks the button
directly and never infers from amity or an arithmetic round count — which is right, and is
exactly why the button has to be READABLE.
"""
import pathlib

import pytest

FRAME = pathlib.Path("data/reference/village/exchange_active.png")


def _frame():
    if not FRAME.exists():
        pytest.skip("reference frame not present")
    from PIL import Image
    return Image.open(FRAME).convert("RGB")


def test_a_gold_exchange_with_no_price_is_still_live():
    """LIVE 2026-08-29, Svear Village. One round committed, four Trade Count slots still
    open, every material plentiful — and the barter stopped.

    `_exchange_still_live` asked `_yellow_commit_button`, which recognises the game's
    `<cost> VERB` commit pill (a button with a PRICE, like '205,848 Recruit'). A barter is
    paid in MATERIALS, so Exchange carries no cost and that detector returned ZERO commits
    while the button sat there in plain gold. The panel classified as BLOCKED.

    The tap path was never affected — `commit_via_positive_taps` uses the colour test and
    had pressed the same button seconds earlier. Only the liveness check was blind.
    """
    from vision.omniparser import parse_fast_cached

    from actions.barter_panel import _exchange_still_live

    frame = _frame()
    assert _exchange_still_live(frame, elements=parse_fast_cached(frame)) is True


def test_the_detector_that_was_used_before_still_cannot_see_it():
    """Named so the reason survives: this is not a regression in the commit detector, it is
    the wrong detector for a button with no price."""
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.commit_button import detect_commit_buttons

    frame = _frame()
    assert not detect_commit_buttons(parse_fast_cached(frame), frame)


def test_the_panel_reads_ready_from_the_frame_it_was_given():
    """And without re-capturing: the classifier must answer about the frame in hand."""
    import brain.village_context as vc

    assert vc.classify(_frame()) == vc.BARTER_PANEL_READY
