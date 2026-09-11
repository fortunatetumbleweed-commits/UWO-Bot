"""Perception observes. The dispatcher answers dialogs.

`brain.perceive.dismiss_interruptors` used to press an unanswerable obstruction's own gold
button after seeing it twice (`_answer_it_anyway`, 2026-09-02). It was added for a real
failure at Madeira: a staged cart made Back raise

    "Moving to another menu will empty the cart. Continue?"   [Cancel] [OK]

nothing matched it, and Back — the only thing the bot kept trying — is that dialog's CANCEL.
It raised and cancelled four times and the mission died one OK away from recovering.

WHY IT IS GONE. `perceive`'s own rule, stated in the module: dismissing an interruptor
restores the world that was already there, and that is perception's to do — but PRESSING A
BUTTON IS NOT ALWAYS A DISMISSAL, and a bbox and a colour cannot tell which it will be.

Live 2026-09-05 at London it was not. The port overworld's quest/trend ticker was flagged
`kind='popup'`; its yellow banners scored 0.47-0.67 as gold buttons; the yellowest,
'Major Trend Oc__', was pressed. That NAVIGATED — port overworld to world map with the Trade
Event Schedule open. Perception moved the fleet, the one thing the module forbids itself, and
the mission stalled on a leg that wanted a market.

The Madeira case is not reopened, and these tests are the proof: the dispatcher's own path
requires a real DialogModel and then asks `game_rules.answer_dialog`. On the two frames that
is exactly the discrimination needed — the cart confirm IS a dialog, the ticker is not.
"""
from __future__ import annotations

import pathlib

import pytest

MADEIRA = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-02T18-12-11/frame_0151.png")
LONDON = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-05T00-16-39/frame_0026.png")


def _dialog(path):
    from PIL import Image
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.dialog import detect_dialog
    frame = Image.open(path)
    return detect_dialog(parse_fast_cached(frame), frame.width, frame.height, frame=frame)


class PerceptionHasNoButtonPresser:
    """The unit half — no frames, no device."""


def test_perceive_no_longer_carries_a_blind_answerer():
    """A deletion, asserted. Anything reintroducing it puts perception back in the business
    of causing transitions."""
    import brain.perceive as bp
    for gone in ("_answer_it_anyway", "_ANSWER_IT_ANYWAY_AFTER", "_UNANSWERED_SIGHTINGS",
                 "_BUTTONS_BELOW_BBOX_PX"):
        assert not hasattr(bp, gone), (
            f"{gone} is back — perception must not press buttons; the dispatcher's "
            "_offer_dialog owns unanswered dialogs")


def test_the_game_rules_still_answer_the_madeira_cart_confirm():
    """The decision half of the surviving path, and it needs no frame: OK, not Cancel."""
    from brain import game_rules
    assert game_rules.answer_dialog(
        ["Ok", "Cancel"],
        ["Moving to another menu will empty the cart. Continue?"]) == "Ok"


def test_a_red_gem_dialog_is_still_refused():
    """What the surviving answerer has that the deleted one did not: it knows what it must
    not confirm."""
    from brain import game_rules
    assert game_rules.answer_dialog(
        ["Ok", "Cancel"], ["Spend 50 red gems to refresh?"],
        spends_red_gem=True) != "Ok"


@pytest.mark.skipif(not MADEIRA.exists(), reason="trace frame not present")
def test_the_madeira_confirm_IS_a_dialog():
    """So the dispatcher reaches it, and the case the deleted code existed for is covered."""
    d = _dialog(MADEIRA)
    assert d is not None
    labels = {str(getattr(a, "label", "")).lower() for a in (getattr(d, "actions", None) or [])}
    assert "ok" in labels and "cancel" in labels


@pytest.mark.skipif(not LONDON.exists(), reason="trace frame not present")
def test_the_LONDON_TICKER_is_not_a_dialog():
    """The frame that caused this change. No brown title bar, no card — so the dispatcher's
    answerer never fires on it, however yellow its banners are."""
    assert _dialog(LONDON) is None


@pytest.mark.skipif(not LONDON.exists(), reason="trace frame not present")
def test_the_tickers_yellow_banners_would_still_fool_a_colour_test():
    """Why a DialogModel gate and not a better yellow threshold: these ARE gold, by the same
    measure that made the Madeira OK identifiable. Colour was never the wrong signal — it was
    the wrong QUESTION, asked on a screen with no dialog on it."""
    from PIL import Image
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.commit_button import detect_commit_buttons
    frame = Image.open(LONDON)
    gold = detect_commit_buttons(parse_fast_cached(frame), frame)
    assert gold, "expected the quest ticker's banners to read as gold"
    assert max(getattr(b, "yellow_frac", 0.0) for b in gold) > 0.3
