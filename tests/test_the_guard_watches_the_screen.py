"""An intent has landed when the SCREEN changed, not when the state label did."""
import types

from PIL import Image

S = "data/sessions/trace_barter_cmd_2026-08-30T11-55-40"


def _sig(name):
    from brain.dispatcher import Dispatcher
    import pathlib as _p

    path = _p.Path(f"{S}/{name}")
    if not path.exists():
        import pytest
        pytest.skip("reference frames not present")
    return Dispatcher._screen_signature(
        types.SimpleNamespace(frame=Image.open(path).convert("RGB")))


def test_a_tooltip_opening_counts_as_the_screen_changing():
    """LIVE 2026-08-30 at Svear, the run's last act.

    The bot pressed Back to leave the village. A `Ducat` tooltip was open — the bot had
    opened it itself, moments earlier, by dismissing a FALSE daily-news detection — so Back
    closed the TOOLTIP. The screen changed; the state label `village` did not.

    The guard compared the label, concluded "my press did nothing", and refused the second
    press that would actually have left. Six ticks later the run stopped one leg from home,
    with the Birch Tree aboard.
    """
    assert _sig("frame_0140.png") != _sig("frame_0139.png")
    only_140 = set(_sig("frame_0140.png")) - set(_sig("frame_0139.png"))
    assert "ducat" in only_140


def test_the_clock_does_not_count_as_a_change():
    """A signature that drifts on its own is a lie about cause and effect: a genuinely stuck
    intent would earn a free re-dispatch every minute. The status-bar clock, in-game times
    and countdowns are excluded."""
    from brain.dispatcher import _TICKS_ON_ITS_OWN

    for ticking in ("13.56", "02:11", "28d 19.00", "11:11:47", "3,427"):
        assert _TICKS_ON_ITS_OWN.match(ticking), ticking
    for real in ("ducat", "barter", "exchange", "move to city", "village"):
        assert not _TICKS_ON_ITS_OWN.match(real), real


def test_no_frame_means_the_guard_behaves_as_before():
    """Fail safe: with nothing to read the signature is empty, so the guard is exactly the
    state-label comparison it always was."""
    from brain.dispatcher import Dispatcher

    assert Dispatcher._screen_signature(types.SimpleNamespace(frame=None)) == ()
    assert Dispatcher._screen_signature(types.SimpleNamespace()) == ()
