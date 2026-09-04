"""One observation per tick — the barter panel is read once and shared."""
import types

from PIL import Image


def _reading():
    return types.SimpleNamespace(good="Birch Tree", materials={"Wares": (838, 104)},
                                 output_quantity=511, amity=(75708, 100000))


def test_one_tick_costs_one_parse_of_one_frame():
    """MEASURED AT SVEAR, 2026-08-30: SIX reads of the same panel per barter round, four of
    them before a single tap, and a round took 57 seconds.

    `_panel()` had no cache and five call sites reached it per tick — the goal check,
    `_on_ready`, `_on_blocked`, the shortfall log, and the round-committed log. Nothing had
    been tapped between them, so the panel could not have changed: five of the six reads were
    pure cost, at a capture + OmniParser pass each (~2-3s).

    Two views of the panel exist — the RAW reading (which good is selected) and the DERIVED
    state (rounds and materials). They were separate captures of one screen; now the derived
    view is computed from the raw reading, so they also cannot disagree.
    """
    from unittest.mock import patch

    import brain.village_context as vc
    from brain.activities.village import Barter, VillageActivity

    captures = {"n": 0}

    def counting_capture():
        captures["n"] += 1
        return Image.new("RGB", (2400, 1080))

    with patch("capture.adb_capture.capture_screen", counting_capture), \
         patch("actions.barter_reader.read_barter_panel", lambda f: _reading()):
        act = VillageActivity(context_fn=lambda s: vc.BARTER_PANEL_READY,
                              commit_fn=lambda: {"ok": True, "reason": "committed",
                                                 "before": (1, 2), "after": (3, 4)})
        state = types.SimpleNamespace(state="village",
                                      frame=Image.new("RGB", (2400, 1080)))
        act.work(Barter(good="Birch Tree", village="Svear Village"), state)

    assert captures["n"] == 0, "the dispatcher's frame must serve the whole tick"


def test_a_tap_drops_the_cached_panel():
    """The panel is PANEL-owned data (Guiding Principle #4): it dies the moment anything is
    tapped. Caching it for the TICK is safe; caching it across an exchange is not."""
    import brain.village_context as vc
    from brain.activities.village import VillageActivity

    act = VillageActivity(context_fn=lambda s: vc.BARTER_PANEL_READY)
    act._tick_panel = "stale"
    act._tick_reading = "stale"
    act._panel_changed()
    assert act._tick_panel is None and act._tick_reading is None


def test_the_round_log_uses_what_the_commit_already_read():
    """`barter_commit_verified` reads the panel on BOTH sides to decide whether the round
    progressed, and returns {before, after}. Re-reading for the log line was a capture +
    parse to fill in a message — on the one path where the cache had just been dropped."""
    import inspect

    from brain.activities import village

    src = inspect.getsource(village.VillageActivity._on_ready)
    assert "res.get('after')" in src or 'res.get("after")' in src, \
        "the log must use the commit's own reading, not a fresh one"
    assert "_amity(self._panel())" not in src, "that was the capture-to-log defect"


def test_the_round_log_finds_the_amity_that_was_already_read():
    """`amity None -> None` was printed for every round of every run — and the value was in
    hand both times.

    `barter_commit_verified` reads the panel either side of the tap and returns
    `{"amity": ..., "cargo": ..., "good": ...}`; a `BarterPanelReading` exposes
    `.amity_points`. The accessor knew only the second shape, so the commit's own readings —
    the closest ones to the exchange that exist — went unread.

    Keeping the raw OmniParser read is the point: the log should take good data from what was
    already observed, never pay a capture for it (user, 2026-08-30).
    """
    import types

    from brain.activities.village import _amity

    assert _amity({"amity": (73125, 100000), "cargo": 1612}) == (73125, 100000)
    assert _amity(types.SimpleNamespace(amity_points=(70615, 100000))) == (70615, 100000)
    assert _amity(None) is None
