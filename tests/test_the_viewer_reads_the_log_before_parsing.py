# tests/test_the_viewer_reads_the_log_before_parsing.py
#
# THE RUN ALREADY SAID WHERE IT WAS (user, 2026-09-03).
#
# Every perceive logs `[classify] -> village`, with the port when it read one. So a frame's
# state is in the session before anyone re-parses anything — and it is BETTER evidence than a
# rebuild, because it is the state the bot ACTED ON rather than one derived afterwards from
# the same picture. A re-parse can succeed where the run failed, and then the misread the
# report exists to show is the one thing it cannot show.
#
# The viewer was not reading it. Unparsed frames showed '?', which implied the run had failed
# to work out where it was when in truth nobody had looked yet — the same "cannot tell is not
# no" confusion that has cost this bot several missions.
#
# And parsing was triggered by OPENING a frame, which is seconds of CPU inside the process
# serving the very image you are waiting for. It is now a button.

import pathlib

from tools.trace_viewer import _annotate_from_log, _log_timeline, _secs


def _session(tmp_path: pathlib.Path, log: str) -> pathlib.Path:
    (tmp_path / "run.log").write_text(log)
    return tmp_path


_LOG = """2026-09-02 22:21:49.710 | INFO | brain.perceive:_classify_nav_state_inner:3212 - [classify] → idle_lock (omniparser, conf=high)
2026-09-02 22:22:03.624 | INFO | brain.perceive:_classify_nav_state_inner:3212 - [classify] → village (omniparser, conf=high)
2026-09-02 22:24:11.100 | INFO | brain.perceive:_classify_nav_state_inner:3074 - [classify] → port_overworld (family-classifier conf=1.00, port='Faro') - short-circuit
"""


def test_the_states_come_out_of_the_log(tmp_path):
    tl = _log_timeline(_session(tmp_path, _LOG))
    assert [t[1] for t in tl] == ["idle_lock", "village", "port_overworld"]
    assert tl[2][2] == "Faro", "the port is on the line and belongs in the report"


def test_a_frame_gets_the_state_the_run_logged_FOR_it(tmp_path):
    """Each frame is captured a moment BEFORE the line describing it — see
    `test_a_frame_takes_the_classify_that_comes_AFTER_it` for what happens when that is read
    the wrong way round. This version was originally written on the wrong premise: it asserted
    the state from the last line BEFORE each capture, and passed against code with the same
    error in it. A test agreeing with the bug it should have caught."""
    frames = [{"t": "22:21:45"}, {"t": "22:22:00"}, {"t": "22:24:05"}]
    _annotate_from_log(_session(tmp_path, _LOG), frames)
    assert [f["log_state"] for f in frames] == ["idle_lock", "village", "port_overworld"]
    assert frames[2]["log_port"] == "Faro"


def test_a_frame_with_no_classify_after_it_gets_no_state_rather_than_a_guess(tmp_path):
    """The run ended, or the frame was never perceived. Say nothing."""
    frames = [{"t": "22:30:00"}]
    _annotate_from_log(_session(tmp_path, _LOG), frames)
    assert "log_state" not in frames[0]


def test_the_gap_between_frames_is_recorded(tmp_path):
    frames = [{"t": "22:21:45"}, {"t": "22:22:00"}, {"t": "22:24:05"}]
    _annotate_from_log(_session(tmp_path, _LOG), frames)
    assert "gap_s" not in frames[0], "the first frame has nothing to be later than"
    assert frames[1]["gap_s"] == 15
    assert frames[2]["gap_s"] == 125, "long gaps are the interesting ones — a settle, a voyage"


def test_a_session_with_no_log_still_builds(tmp_path):
    """Older sessions have no run.log beside them; the report must still render."""
    frames = [{"t": "22:21:50"}, {"t": "22:22:10"}]
    _annotate_from_log(tmp_path, frames)          # no run.log written
    assert not any("log_state" in f for f in frames)
    assert frames[1]["gap_s"] == 20, "the gap comes from the trace, not the log"


def test_seconds_parse():
    assert _secs("00:00:00") == 0
    assert _secs("22:24:30") == 22 * 3600 + 24 * 60 + 30


def test_the_page_asks_before_parsing_and_never_on_open():
    """The behaviour change, pinned in the emitted page rather than described in prose:
    a Parse button exists, and nothing fires a read from navigation."""
    import inspect
    from tools import trace_viewer
    html = trace_viewer._HTML
    assert "readFrame(cur)" in html, "there must be a Parse button to press"
    assert "maybeRead" not in html, "opening a frame must not start a parse"
    src = inspect.getsource(trace_viewer)
    assert "if(src==='unread'&&SERVED)" not in src, "the auto-read on render is back"


_ORDER_LOG = """2026-09-03 08:40:57.100 | INFO | [classify] → port_overworld (conf=high)
2026-09-03 08:41:11.200 | INFO | [classify] → world_map (conf=high)
2026-09-03 08:42:30.300 | INFO | [classify] → port_overworld (conf=high)
"""


def test_a_frame_takes_the_classify_that_comes_AFTER_it(tmp_path):
    """A FRAME IS CAPTURED AND THEN CLASSIFIED (user, 2026-09-03).

    The describing line comes after the timestamp, never before. Taking the last classify
    at-or-before labelled every frame with the PREVIOUS one's state: frame_0008, captured
    08:41:06 on the world map, read `port_overworld` from a line at 08:40:57 while the line
    about it sat at 08:41:11.

    A wrong state is worse than none — it is the report asserting something about a frame you
    can see is false."""
    frames = [{"t": "08:41:06"}, {"t": "08:42:25"}]
    _annotate_from_log(_session(tmp_path, _ORDER_LOG), frames)
    assert frames[0]["log_state"] == "world_map", "took the line from BEFORE the capture"
    assert frames[1]["log_state"] == "port_overworld"


def test_a_classify_after_the_next_capture_belongs_to_that_frame(tmp_path):
    """Two captures in quick succession: the line logged after the second is about the
    second, and the first must not claim it."""
    frames = [{"t": "08:41:06"}, {"t": "08:41:08"}]
    _annotate_from_log(_session(tmp_path, _ORDER_LOG), frames)
    assert "log_state" not in frames[0], "claimed the next frame's classification"
    assert frames[1]["log_state"] == "world_map"


def test_a_classify_far_later_is_not_claimed(tmp_path):
    """Perception takes seconds; a line a minute later is about a frame the trace never
    recorded. Say nothing rather than guess."""
    frames = [{"t": "08:41:20"}]
    _annotate_from_log(_session(tmp_path, _ORDER_LOG), frames)
    assert "log_state" not in frames[0]


# ─────────────────────────────────────────────────────────────────────────────────────────
# A PERCEIVE LOGS A CHAIN, NOT A LINE (user, 2026-09-03).
#
# "since the bot in the village, many frames are shown as Port overworld, like 344, 342, 340,
# 338, 339 — why is that?"
#
# Because one look emits several `[classify]` lines and the viewer took the first. The first
# is the CASCADE'S PROPOSAL; the gates that overrule it come after, and the verdict last. So a
# fleet standing in a village was reported as `port_overworld` — the very guess the run had
# rejected one line later.
#
# This is a different axis from the direction bug above, and the test for that one could not
# have caught it: `_LOG` has exactly one classify line per perceive, so first and last are the
# same line. A fixture simple enough to make the old code look right.
#
# These use the real line shapes from the San Village run, em-dashes and all.

_CHAINED = """2026-09-03 13:37:33.178 | INFO | brain.perceive:_classify_nav_state_inner:3212 - [classify] \u2192 port_overworld (omniparser, conf=high, signals=['right_edge_panel=count(2/2)'])
2026-09-03 13:37:33.178 | INFO | brain.perceive:_classify_nav_state:2757 - [classify] STRUCTURE GATE: cascade said 'port_overworld' but the left menu is a VILLAGE's (barter+gifting) \u2014 that is a chromed screen, not an overworld
2026-09-03 13:37:33.178 | INFO | brain.perceive:_classify_nav_state:2774 - [classify] family=chromed@0.99 GATE: cascade said 'port_overworld' but a chromed frame is never an overworld \u2014 \u2192 unrecognized_chromed_screen
2026-09-03 13:37:33.202 | INFO | brain.perceive:_classify_nav_state:2854 - [classify] \u2192 village (left-menu vocab match ['barter', 'explore', 'gifting', 'loot', 'recruit crew']; name=None)
"""

_GATE_ONLY = """2026-09-03 13:36:49.243 | INFO | brain.perceive:_classify_nav_state_inner:3212 - [classify] \u2192 port_overworld (omniparser, conf=high, signals=['right_edge_panel=count(2/2)'])
2026-09-03 13:36:49.243 | INFO | brain.perceive:_classify_nav_state:2814 - [classify] family=transient@1.00 GATE: cascade said 'port_overworld' but a transient overlay is never an overworld \u2014 \u2192 unknown
"""


def test_the_verdict_wins_over_the_proposal(tmp_path):
    """Frame 340 of the San run. The bot was in a village and knew it."""
    frames = [{"t": "13:37:24"}]
    _annotate_from_log(_session(tmp_path, _CHAINED), frames)
    assert frames[0]["log_state"] == "village", "reported the guess the run threw away"


def test_a_gate_override_at_the_END_of_its_line_is_seen(tmp_path):
    """Frames 338/339. The transient gate writes its answer after an em-dash rather than
    on a line of its own, so anchoring the arrow to just after `[classify]` missed it
    entirely — and the frame showed a state the run had explicitly rejected."""
    frames = [{"t": "13:36:38"}]
    _annotate_from_log(_session(tmp_path, _GATE_ONLY), frames)
    assert frames[0]["log_state"] == "unknown", "took the proposal over the gate's override"


def test_every_line_of_the_chain_is_collected(tmp_path):
    tl = _log_timeline(_session(tmp_path, _CHAINED))
    assert [t[1] for t in tl] == \
        ["port_overworld", "unrecognized_chromed_screen", "village"], \
        "the STRUCTURE GATE line carries no verdict and must not contribute one"


def test_a_line_with_no_arrow_contributes_nothing(tmp_path):
    noise = ("2026-09-03 13:38:21.367 | INFO | brain.perceive:_classify_nav_state_inner:3229 "
             "- [classify] chrome: home=False back=False hamburger=False right_panel=False\n")
    assert _log_timeline(_session(tmp_path, noise)) == []


def test_an_arrow_to_something_that_is_not_a_state_is_ignored(tmp_path):
    """`read_port_name \u2192 'Barter'` is a reading, not a classification, and the quotes are
    what tell them apart."""
    noise = ("2026-09-03 13:38:36.817 | INFO | brain.perceive:_classify_nav_state_inner:3334 "
             "- [classify] read_port_name \u2192 'Barter'\n")
    assert _log_timeline(_session(tmp_path, noise)) == []


def test_the_single_line_case_still_works(tmp_path):
    """A short-circuit logs one line and no gates fire — first and last are the same, and the
    port on it is still the port."""
    frames = [{"t": "22:24:05"}]
    _annotate_from_log(_session(tmp_path, _LOG), frames)
    assert frames[0]["log_state"] == "port_overworld"
    assert frames[0]["log_port"] == "Faro"
