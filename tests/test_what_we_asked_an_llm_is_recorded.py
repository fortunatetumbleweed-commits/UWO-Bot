"""An LLM's INPUT is the evidence. A report that omits it can only show the symptom.

On 2026-09-08 Qwen answered "sailing in the Atlantic Ocean" twenty times about a bot standing
in a market, and finding out why meant rebuilding the prompt by hand, offline, days later.
The run log had said `prompt=14516 chars` and truncated the answer at 200 characters — enough
to know a consult happened, not enough to know what it was told. It turned out to have been
handed the account watermark off the bottom of the screen.

So: the question and the whole answer go to the log, the full prompt goes to the trace, and
the viewer grows an LLM tab that shows both.
"""

from __future__ import annotations

import json
import unittest
import unittest.mock as mock


class TheSinkIsSilentUntilSomeoneListens(unittest.TestCase):
    """Recording must cost nothing, and must never be able to break a run."""

    def tearDown(self):
        from vision.llm_trace import set_sink
        set_sink(None)

    def test_no_sink_is_a_no_op(self):
        from vision.llm_trace import record, set_sink
        set_sink(None)
        record("m", "q", "p", "r")            # must not raise

    def test_a_sink_that_throws_cannot_break_the_caller(self):
        from vision.llm_trace import record, set_sink
        def boom(_):
            raise RuntimeError("disk full")
        set_sink(boom)
        record("m", "q", "p", "r")            # must not raise

    def test_the_consult_arrives_whole(self):
        from vision.llm_trace import record, set_sink
        seen = []
        set_sink(seen.append)
        record("qwen", "What is the current state?", "PROMPT", '{"detail": "x"}',
               elapsed_s=9.53, nav_state="sub_menu")
        self.assertEqual(1, len(seen))
        self.assertEqual("What is the current state?", seen[0]["question"])
        self.assertEqual("PROMPT", seen[0]["prompt"])
        self.assertEqual('{"detail": "x"}', seen[0]["response"])
        self.assertEqual(9.53, seen[0]["elapsed_s"])
        self.assertEqual("sub_menu", seen[0]["nav_state"])


class TheTraceKeepsThemBesideTheFrame(unittest.TestCase):

    def test_a_consult_is_tagged_with_the_frame_it_asked_about(self):
        import actions.action_trace as at
        with mock.patch.object(at, "_dir", at.Path(self.tmp)), \
             mock.patch.object(at, "_idx", 41):
            at.record_llm({"model": "qwen", "question": "q", "prompt": "p", "response": "r"})
        rows = [json.loads(l) for l in
                (at.Path(self.tmp) / "llm.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual(1, len(rows))
        self.assertEqual(40, rows[0]["frame_idx"],
                         "the consult is about the frame already captured, not the next one")

    def test_recording_without_a_session_is_a_no_op(self):
        import actions.action_trace as at
        with mock.patch.object(at, "_dir", None):
            at.record_llm({"model": "qwen"})   # must not raise

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()


class TheViewerShowsThem(unittest.TestCase):

    def test_consults_are_attached_to_their_frames(self):
        from tools.trace_viewer import _attach_consults
        frames = [{"idx": 0}, {"idx": 1}]
        _attach_consults(frames, {1: [{"model": "qwen", "question": "q"}]})
        self.assertNotIn("llm", frames[0], "a frame with no consult carries nothing")
        self.assertEqual("qwen", frames[1]["llm"][0]["model"])

    def test_the_tab_exists_and_is_reachable_by_key(self):
        import tools.trace_viewer as tv
        self.assertIn('"LLM"', tv._HTML)
        self.assertIn("function llmView", tv._HTML)
        self.assertIn("llmView][tab](f)", tv._HTML)
        self.assertIn("e.key<='5'", tv._HTML, "five tabs need five number keys")


class QwenSaysWhatItWasAskedAndWhatItAnswered(unittest.TestCase):

    def test_the_question_and_the_full_answer_are_logged(self):
        import vision.qwen_perception as q
        lines = []
        with mock.patch.object(q, "_build_prompt", lambda *a, **k: "PROMPT"), \
             mock.patch.object(q, "_call_mlx", lambda p: '{"detail": "a market"}'), \
             mock.patch.object(q.logger, "info", lambda m, *a, **k: lines.append(str(m))):
            q.qwen_perceive("sub_menu", "sub_menu: purchase", [])
        joined = "\n".join(lines)
        self.assertIn("[qwen] asked:", joined)
        self.assertIn('{"detail": "a market"}', joined,
                      "the answer in full — a 200-char head hides exactly the bad ones")


if __name__ == "__main__":
    unittest.main()
