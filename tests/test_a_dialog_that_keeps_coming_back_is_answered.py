# tests/test_a_dialog_that_keeps_coming_back_is_answered.py
#
# A DIALOG THAT KEEPS COMING BACK GETS ITS OWN BUTTON PRESSED (user, 2026-09-02).
#
# Leaving an unrecognised obstruction alone is right for a popup sitting harmlessly over a
# world. It is wrong for a MODAL, which answers nothing until it is answered.
#
# Live 2026-09-02 at Madeira: a staged cart made Back raise
#
#     "Moving to another menu will empty the cart. Continue?"   [Cancel] [OK]
#
# No interruptor matched, the Claude consult could not run (no ANTHROPIC_API_KEY), so nothing
# answered it — and Back, the only thing the bot kept trying, is that dialog's CANCEL. Frames
# 151-158 show it raised and cancelled four times before the stall guard ended the mission.
# The market top menu was one OK away, and from there the buy could have been retried.
#
# Gold is what makes the answer identifiable, and the measurement is the whole argument:
# on that dialog OK is 0.321 yellow and Cancel is 0.000. Wording is not consulted — matching
# POSITIVE_LABELS on words is what once tapped 'Trade Info' and a panel title.

import pathlib

import pytest

import brain.perceive as bp

_TRACE = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-02T18-12-11")
_DIALOG = _TRACE / "frame_0151.png"          # the cart confirm, Cancel + OK
_OBSTRUCTION_BBOX = (792, 122, 1667, 676)    # what the detector reported that run


def _frame():
    if not _DIALOG.exists():
        pytest.skip("real-frame fixture not in the repo")
    from PIL import Image
    return Image.open(_DIALOG)


@pytest.mark.functional
def test_it_presses_ok_and_not_cancel():
    """The gold background decides, and only one of the two has it."""
    taps = []
    with pytest.MonkeyPatch.context() as m:
        import actions.ui as ui
        m.setattr(ui, "tap_at", lambda x, y, **kw: taps.append((x, y)))
        assert bp._answer_it_anyway(_frame(), _OBSTRUCTION_BBOX) is True
    assert taps, "nothing was pressed"
    x, y = taps[0]
    assert abs(x - 1307) < 40 and abs(y - 826) < 40, f"pressed {taps[0]}, expected OK @(1307,826)"
    assert x > 1200, "Cancel is at x=1091 and must never be chosen — it is 0.000 yellow"


@pytest.mark.functional
def test_the_buttons_sit_BELOW_the_reported_bbox():
    """Why the search is not 'strictly inside the bbox'.

    The detector's box covers title and body and stops above the button row: it ended at
    y=676 while OK is at y=826. A strictly-inside search finds nothing to press, which is
    also why the existing bbox-scoped `_dismiss_tap_ok_or_x` could not have answered this."""
    _, _, _, y2 = _OBSTRUCTION_BBOX
    assert y2 < 826, "the fixture no longer demonstrates the gap this margin exists for"
    assert 826 <= y2 + bp._BUTTONS_BELOW_BBOX_PX, "the margin no longer reaches the buttons"


@pytest.mark.functional
def test_it_will_not_reach_the_markets_own_purchase_button():
    """Scoped, not frame-wide. The market's gold Purchase button is on screen behind this
    dialog, and pressing it spends money the task never asked to spend."""
    taps = []
    with pytest.MonkeyPatch.context() as m:
        import actions.ui as ui
        m.setattr(ui, "tap_at", lambda x, y, **kw: taps.append((x, y)))
        bp._answer_it_anyway(_frame(), _OBSTRUCTION_BBOX)
    # the commit strip lives at y~996; nothing there may be chosen
    assert all(y < 950 for _, y in taps), f"reached the commit strip: {taps}"


def test_it_holds_off_until_the_thing_has_come_back():
    """A popup that would have cleared itself must never reach the escalation."""
    assert bp._ANSWER_IT_ANYWAY_AFTER >= 2


def test_a_dialog_with_no_gold_button_is_left_alone():
    """Reporting beats tapping blind: with nothing gold in range there is no answer to give,
    and guessing at a grey button is how a 'positive' search taps a panel title."""
    from PIL import Image
    blank = Image.new("RGB", (2400, 1080), (20, 20, 20))
    taps = []
    with pytest.MonkeyPatch.context() as m:
        import actions.ui as ui
        m.setattr(ui, "tap_at", lambda x, y, **kw: taps.append((x, y)))
        assert bp._answer_it_anyway(blank, _OBSTRUCTION_BBOX) is False
    assert not taps


def test_the_call_site_itself_runs():
    """THE GAP THAT SHIPPED A CRASH.

    Every test above calls `_answer_it_anyway` directly. Nothing drove `dismiss_interruptors`,
    which is the function that calls it — and where the escalation was wired using `KIND_NONE`
    without importing it. A 4,540-test suite passed over a NameError on the path every tick
    takes, and the live run died on its first perceive.

    A helper tested in isolation says nothing about the caller that was edited to use it."""
    from PIL import Image
    import brain.perceive as bp
    # a clean frame: no obstruction, so the escalation branch is reached and must not raise
    bp.dismiss_interruptors(Image.new("RGB", (2400, 1080), (10, 10, 10)))


@pytest.mark.functional
def test_the_call_site_runs_with_a_real_obstruction_on_screen():
    """The branch itself, not just the path around it."""
    import brain.perceive as bp
    bp._UNANSWERED_SIGHTINGS = 0
    with pytest.MonkeyPatch.context() as m:
        import actions.ui as ui
        m.setattr(ui, "tap_at", lambda x, y, **kw: None)
        bp.dismiss_interruptors(_frame())
