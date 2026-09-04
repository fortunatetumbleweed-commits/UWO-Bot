# tests/test_a_dialog_on_a_dialog_is_seen.py
#
# "IT DID NOT TAP THE OK THIS TIME, BUT IT HAS HANDLED THE SAME DIALOG MULTIPLE TIMES DURING
# THIS BARTER" (user, 2026-09-03).
#
# A `Notice — Complete the trade?` sat over an `Insufficient Empty Space` dialog at San
# Village for two minutes while DialogModel reported the frame CLEAN on every single look.
# The barter executor had tapped its OK three times through its own gold-button path and the
# fourth tap was swallowed; nothing retried, because the one component whose job is to say
# "a dialog is up" could not see it. The mission was stopped by the stall guard.
#
# WHY IT COULD NOT SEE IT: the dialog's extent was inferred from where ELEMENTS sat — every
# element within ±480px of the action row, top of frame to bottom. That vertical stripe has
# no bottom, so it ran from the front dialog's title (y=115) through the barter panel behind
# it to the footer at y=1047. Height 932 > the 864 side-panel guard, and a modal was rejected
# as an info panel. Two more anchors came from the wrong layer at the same time: the side
# panel's close-X at (2173,336) beat the dialog's own at (1616,238), and the panel's
# `Receive` button was counted among the dialog's actions.
#
# THE FIX IS THE SCREEN'S OWN LAYER MARKER (user): every dialog here has a brown title bar,
# and the game composites a flat ~50% scrim over whatever a dialog covers — so a bar's
# brightness says which layer it is on. Colour identifies the bar (the ratios survive the
# dim); luminance ranks the layers; the card is segmented downward from the frontmost bar.
#
# These run against the real frames from that run and skip when they are absent.

import pathlib
import unittest

import pytest

pytestmark = pytest.mark.functional

_SESSION = pathlib.Path("data/sessions/trace_barter_cmd_2026-09-03T12-29-23")


def _frame(n: int):
    p = _SESSION / f"frame_{n:04d}.png"
    if not p.exists():
        pytest.skip("the San Village session is not in this checkout")
    from PIL import Image
    return Image.open(p)


# frame 334 — `Insufficient Empty Space` ALONE, undimmed.
# frame 335 — the same dialog with a `Notice` stacked on top of it.
# frame 192 — a market Purchase screen: no dialog at all.


class TheBrownBarSaysWhichLayerItIsOn(unittest.TestCase):
    """The measurement the fix rests on. Same dialog, in front and then behind."""

    def test_a_front_bar_and_a_dimmed_one_are_two_to_one(self):
        from vision.region_detectors.dialog import find_title_bars
        alone = find_title_bars(_frame(334))
        stacked = find_title_bars(_frame(335))
        assert len(alone) == 1, "one dialog, one bar"
        assert len(stacked) == 2, "a dialog on a dialog is two bars"
        front, behind = stacked
        assert front.luminance == pytest.approx(alone[0].luminance, abs=2.0), \
            "the same bar reads the same when it is the frontmost one"
        assert front.luminance / behind.luminance == pytest.approx(2.0, abs=0.15), \
            "the scrim is a flat half; that ratio is the whole signal"

    def test_the_brightest_bar_is_the_one_in_front(self):
        """Ranking, not thresholding — so no constant has to survive a palette change."""
        from vision.region_detectors.dialog import find_title_bars
        bars = find_title_bars(_frame(335))
        assert bars == sorted(bars, key=lambda b: -b.luminance)
        # The Notice is drawn lower on the screen than the dialog it covers, so "frontmost"
        # cannot be read off position — only off brightness.
        assert bars[0].bbox[1] > bars[1].bbox[1]

    def test_scenery_and_price_strips_are_not_bars(self):
        """Both false positives found by sweeping 400 frames of 243 sessions: warm market
        scenery (same hue, but textured — std 12.8-23.0 against a real bar's 2.1-5.0) and the
        market's tan price strips (R/B 1.82, G/B 1.48 against 2.00 and 1.33)."""
        from vision.region_detectors.dialog import find_title_bars
        bars = find_title_bars(_frame(192))
        assert all(b.flatness <= 8.0 for b in bars)
        assert not any(b.luminance > 100 for b in bars), \
            "a tan price strip was taken for a title bar"


class TheCardIsTheDialogNotTheScreenBehindIt(unittest.TestCase):
    def test_the_stacked_dialog_is_detected_at_all(self):
        """The regression itself: CLEAN on every look for two minutes."""
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        f = _frame(335)
        d = detect_dialog(list(parse_fast_cached(f)), f.width, f.height, frame=f)
        assert d is not None, "a modal covering the screen was reported as no dialog"

    def test_it_is_the_FRONT_dialog_that_is_described(self):
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        f = _frame(335)
        d = detect_dialog(list(parse_fast_cached(f)), f.width, f.height, frame=f)
        labels = {a.label.strip().lower() for a in d.actions}
        assert labels == {"ok", "cancel"}, \
            "took an action button off the dialog BEHIND this one"
        assert "receive" not in labels
        assert d.kind() == "confirmation"

    def test_the_bbox_is_the_card_and_clears_the_side_panel_guard(self):
        """566px, not the 932px stripe that tripped the guard written for info panels."""
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        f = _frame(335)
        d = detect_dialog(list(parse_fast_cached(f)), f.width, f.height, frame=f)
        assert d.bbox[3] - d.bbox[1] < 0.80 * f.height

    def test_a_single_dialog_still_works(self):
        """The stacked case must not be bought with the ordinary one."""
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        f = _frame(334)
        d = detect_dialog(list(parse_fast_cached(f)), f.width, f.height, frame=f)
        assert d is not None
        assert {a.label.strip().lower() for a in d.actions} == {"receive"}

    def test_a_screen_with_no_dialog_stays_clean(self):
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        f = _frame(192)
        assert detect_dialog(list(parse_fast_cached(f)), f.width, f.height, frame=f) is None

    def test_without_a_frame_the_old_path_is_unchanged(self):
        """The frame is optional: every existing caller keeps working, and a caller that
        cannot supply pixels is never worse off than before."""
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        f = _frame(335)
        assert detect_dialog(list(parse_fast_cached(f)), f.width, f.height) is None


class ThePixelsReachTheDetector(unittest.TestCase):
    """A working detector nobody hands a frame to is the KIND_NONE crash again — 4,540 tests
    passed over a NameError because every one called the helper and none drove the call
    site."""

    def test_perceive_passes_the_frame(self):
        import inspect
        from brain import perceive
        src = inspect.getsource(perceive._detect_dialog_on_frame)
        assert "frame=frame" in src

    def test_the_inventory_carries_the_frame_for_the_obstruction_classifier(self):
        from PIL import Image
        from vision.screen_perception import parse_screen
        inv = parse_screen(Image.new("RGB", (2400, 1080), (20, 20, 20)))
        assert hasattr(inv, "frame"), "ScreenInventory dropped the pixels"
        import inspect
        from vision import obstruction_classifier
        src = inspect.getsource(obstruction_classifier._detect_dialog)
        assert 'frame=getattr(inventory, "frame", None)' in src


class TheCoveredScreenIsNotActedOn(unittest.TestCase):
    """"Although indeed in this case Barter IS the active submenu, it is right now dimmed by
    the dialog, so it is not actionable — we need a way to let the bot know that" (user,
    2026-09-03).

    This is the third and last thing that had to line up for the mission to wedge. Perceive
    ran every tick and the obstruction gate said clean (fixed above); the barter executor,
    which had been tapping the OK, had finished and stopped; and what replaced it —
    `_open_barter_panel` — returned True WITHOUT TAPPING on every tick, because it opens with

        if on_submenu("barter", frame): return True   # already there - do not tap again

    and the dialogs are drawn over the Barter screen, whose title still reads 'Barter'. Hence
    'opened the barter panel' logged three times with no tap in the trace, and the stall
    guard. Nothing was broken about the reading; the question was."""

    def test_the_title_still_truthfully_says_Barter(self):
        from actions.ui import active_submenu
        assert active_submenu(_frame(336)) == "Barter"

    def test_but_the_screen_is_known_to_be_covered(self):
        from actions.ui import chrome_is_dimmed
        assert chrome_is_dimmed(_frame(336)) is True

    def test_so_the_precondition_refuses(self):
        """The exact call `_open_barter_panel` makes, on the exact frame it made it on."""
        from actions.ui import on_submenu
        assert on_submenu("barter", _frame(336)) is False

    def test_an_uncovered_barter_screen_still_passes(self):
        """Bought at no cost to the ordinary case — frames 253 and 262 are the same screen
        with nothing over it."""
        from actions.ui import on_submenu
        assert on_submenu("barter", _frame(253)) is True
        assert on_submenu("barter", _frame(262)) is True

    def test_a_side_panel_header_does_not_count_as_covering(self):
        """Frame 192 carries a brown bar — the Purchase side panel's header — but the panel
        covers nothing, and the title reads a full 255. This is why actionability is read off
        the TITLE rather than off "is there a dialog-ish bar somewhere on screen"."""
        from actions.ui import chrome_is_dimmed
        assert chrome_is_dimmed(_frame(192)) is False
