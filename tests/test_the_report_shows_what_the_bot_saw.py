"""The trace attaches the perception the bot ACTED ON, and the report says when it did not.

Two reasons, and the first is the important one (user, 2026-09-01):

  * RE-PARSING HIDES THE BUG. A second look at a frame can succeed where the live run failed,
    and then the misread the report was opened to find is the one thing it cannot show. Every
    defect chased through the viewer today was a misread — a merged tab label, a port pin read
    as a rail icon, a map read as an open list. A report that quietly re-perceives is a report
    that hides them.
  * It is also ~5s a frame, and there are a hundred of them.

The mechanism existed and almost never fired: perception reached 22 of 122 frames. The gate
was a PIXEL DIFF between the trace's capture and the last perceive —

    if classify_action_outcome(pf, frame).kind != "unchanged": return frame, None

— and this game animates every frame (flags, water, crowds), so two captures of one unchanged
screen compare as CHANGED. CLAUDE.md says so in as many words: "walking across a port animates
every frame, and a pixel test would call that 'changed'".

The repository answers by GENERATION instead. Its observation is valid exactly while nothing
has acted, and `record_tap` runs BEFORE the tap — so a valid observation IS the screen the
action is about to be taken on. No pixels compared, and nothing may cost inference: recording
what happened must not change what happens.
"""
import unittest

from vision.perceive_repository import PerceiveRepository


class _Frame:
    def __init__(self, tag): self.tag = tag
    def crop(self, box): return f"{self.tag}@{box}"


def _repo(parse=None):
    calls = {"parses": 0}

    def _parse(f):
        calls["parses"] += 1
        return (parse or ["el"])

    r = PerceiveRepository(capture_fn=lambda: _Frame("F"), parse_fn=_parse,
                           ocr_fn=lambda i: "", clock=lambda: 0.0, sleep_fn=lambda s: None)
    r._calls = calls
    return r


class WhatIsHeldIsOfferedOnlyWhileItIsCurrent(unittest.TestCase):
    def test_nothing_is_offered_before_a_look(self):
        r = _repo()
        self.assertIsNone(r.current_if_valid())
        self.assertIsNone(r.elements_if_ready())

    def test_the_observation_is_offered_after_a_look(self):
        r = _repo()
        r.get()
        self.assertIsNotNone(r.current_if_valid())

    def test_an_action_withdraws_it(self):
        r = _repo()
        r.get(); r.elements()
        r.invalidate("we did: tap")
        self.assertIsNone(r.current_if_valid(), "the screen has moved; this is not it")
        self.assertIsNone(r.elements_if_ready())

    def test_it_differs_from_for_logging_which_takes_anything(self):
        r = _repo()
        r.get()
        r.invalidate("we did: tap")
        self.assertIsNotNone(r.for_logging(), "a log line is better with an old frame")
        self.assertIsNone(r.current_if_valid(), "the trace needs the CURRENT one")


class RecordingMustNotCostInference(unittest.TestCase):
    def test_an_unparsed_observation_offers_nothing(self):
        r = _repo()
        r.get()
        self.assertIsNone(r.elements_if_ready(),
                          "nobody parsed it, and the trace must not be the one who does")
        self.assertEqual(r._calls["parses"], 0)

    def test_a_parsed_one_is_handed_over_free(self):
        r = _repo()
        r.get()
        r.elements()
        self.assertEqual(r.elements_if_ready(), ["el"])
        self.assertEqual(r._calls["parses"], 1, "the parse the perceive already paid for")

    def test_asking_twice_parses_nothing(self):
        r = _repo()
        r.get(); r.elements()
        r.elements_if_ready(); r.elements_if_ready()
        self.assertEqual(r._calls["parses"], 1)


class TheReportSaysWhoseReadingItIs(unittest.TestCase):
    """`live` is evidence of what the bot had; `rebuilt` is a second look, and the difference
    is the whole reason to read the report."""

    def test_the_marker_is_carried_on_every_frame(self):
        import inspect
        from tools import trace_viewer
        src = inspect.getsource(trace_viewer.build)
        self.assertIn('"perception": "live"', src)
        self.assertIn('data["perception"] = "rebuilt"', src)

    def test_the_header_distinguishes_them(self):
        import inspect
        from tools import trace_viewer
        page = inspect.getsource(trace_viewer)
        self.assertIn("● live", page)
        self.assertIn("○ rebuilt", page)
        self.assertIn("NOT what the bot saw", page,
                      "a rebuilt reading must announce that nobody acted on it")


if __name__ == "__main__":
    unittest.main()


class NothingIsReadUntilSomebodyAsks(unittest.TestCase):
    """A report is ~120 frames and you open two of them.

    Parsing the rest costs about five seconds each — measured 10s on one frame while the
    suite was competing for the GPU — and answers a question nobody put. Building the
    122-frame report went from ~14 minutes to 0.14 seconds by not doing it.

    The build still carries everything the RUN recorded, and the image, the tap and the
    action are in the trace, so browsing needs no perception at all. What is missing is
    marked `unread` rather than filled in.
    """

    def _build(self, tmp, *, eager):
        import json
        from pathlib import Path
        from tools.trace_viewer import build
        d = Path(tmp)
        (d / "actions.jsonl").write_text(json.dumps(
            {"idx": 0, "kind": "tap", "x": 1, "y": 2, "label": None,
             "t": "00:00:00", "frame": "frame_0000.png"}) + "\n")
        from PIL import Image
        Image.new("RGB", (240, 108)).save(d / "frame_0000.png")
        return build(d, qwen=False, limit=0, rebuild=False, eager=eager)

    def test_a_frame_nobody_asked_for_is_left_unread(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            got = self._build(tmp, eager=False)
        self.assertEqual(got[0]["perception"], "unread")
        self.assertNotIn("omni", got[0], "nothing was parsed, so nothing is claimed")

    def test_the_frame_and_the_action_are_still_there(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            got = self._build(tmp, eager=False)
        self.assertEqual(got[0]["frame"], "frame_0000.png")
        self.assertEqual((got[0]["x"], got[0]["y"]), (1, 2), "browsing needs no perception")

    def test_eager_still_reads_everything(self):
        """The old behaviour is a flag, not a deletion — `--qwen` still implies it."""
        import inspect
        from tools import trace_viewer
        src = inspect.getsource(trace_viewer.main)
        self.assertIn("--eager", src)
        self.assertIn("eager = args.eager or args.qwen", src)


class PressingParseIsTheRequest(unittest.TestCase):
    """SUPERSEDES "opening a frame is the request" (user, 2026-09-03).

    Opening a frame used to start a parse. Most looks at a report are "what did the bot SEE" —
    the picture and the line from the log — and a parse is seconds of CPU inside the process
    serving the very image you are waiting for, so frames appeared to load one at a time.

    A frame now arrives with what the run recorded and nothing else, and the button is the
    request."""

    def test_the_page_offers_a_button_when_served(self):
        import inspect
        from tools import trace_viewer
        page = inspect.getsource(trace_viewer)
        self.assertIn("const SERVED = location.protocol.startsWith('http')", page)
        self.assertIn("readFrame(cur)", page, "there must be a Parse button to press")

    def test_opening_a_frame_starts_nothing(self):
        import inspect
        from tools import trace_viewer
        page = inspect.getsource(trace_viewer)
        self.assertNotIn("if(src==='unread'&&SERVED)", page,
                         "render must not fire a parse")
        self.assertNotIn("maybeRead", page, "nothing fires from navigation any more")

    def test_a_read_frame_is_never_re_read(self):
        import inspect
        from tools import trace_viewer
        page = inspect.getsource(trace_viewer)
        self.assertIn("if(f.perception!=='unread') return;", page)

    def test_the_frame_name_from_the_page_is_not_trusted(self):
        """It is our own page asking, but the server must not read outside the session."""
        import inspect
        from tools import trace_viewer
        src = inspect.getsource(trace_viewer.serve)
        self.assertIn('if "/" in name', src)
        self.assertIn('not name.endswith(".png")', src)
        self.assertIn('127.0.0.1', src, "a tool for this machine, not a service")


class AReadMustNotStallTheReport(unittest.TestCase):
    """Live 2026-09-01: served, the report appeared to load one frame at a time.

    The server was `socketserver.TCPServer` — single-threaded — so a five-second parse also
    stalled every image and asset request queued behind it. And `render` fired a read for any
    unread frame it landed on, so arrowing down the list built a queue of blocking parses
    behind wherever you actually stopped.

    Threaded now, so assets are served while a read runs (measured: a frame PNG came back in
    16ms during a parse). The MODELS are still serialised by a lock — they share one GPU and
    running several passes at once would only thrash.
    """

    def test_the_server_is_threaded(self):
        import inspect
        from tools import trace_viewer
        src = inspect.getsource(trace_viewer.serve)
        self.assertIn("ThreadingHTTPServer", src)
        self.assertNotIn("socketserver.TCPServer", src,
                         "single-threaded, a parse blocks every image behind it")

    def test_the_models_are_still_serialised(self):
        import inspect
        from tools import trace_viewer
        src = inspect.getsource(trace_viewer.serve)
        self.assertIn("read_lock = threading.Lock()", src)
        self.assertIn("with read_lock:", src, "one GPU; parallel passes only thrash")

    def test_passing_through_a_frame_does_not_read_it(self):
        """Still true, and now true by construction rather than by debounce.

        This used to assert a 350ms settle before an auto-read fired. Nothing fires from
        navigation at all now, so there is no queue to build and nothing to tame — the
        debounce went with the auto-read."""
        import inspect
        from tools import trace_viewer
        page = inspect.getsource(trace_viewer)
        self.assertNotIn("maybeRead", page)
        self.assertNotIn("setTimeout(()=>{ if(i===cur) readFrame(i); }", page)

    def test_a_stale_answer_does_not_redraw_another_frame(self):
        import inspect
        from tools import trace_viewer
        page = inspect.getsource(trace_viewer)
        self.assertIn("if(i===cur) render();", page)
