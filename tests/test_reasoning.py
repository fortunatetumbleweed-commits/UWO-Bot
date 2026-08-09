"""Reasoning layer — context assembly, action parsing, shadow mode, trace log."""
import json
import tempfile
import unittest
from pathlib import Path

from brain.reasoning import (
    ReasoningContext, reason, parse_action, ACTION_OPS,
)
from brain.world_model import WorldModel, Fleet, Ship


def _ctx():
    wm = WorldModel(fleets=[Fleet(location="London", current_building="Inn",
                                  crew_current=42, crew_capacity=60,
                                  ships=[Ship("Barque", 88)])])
    return ReasoningContext(
        goal="depart London → Amsterdam (crew short)",
        world_model=wm.to_prompt(),
        perception="base=panel context=Inn menu=[Recruit,Hire,Party] title='Inn'",
        game_knowledge="Recruit Crew is available at Harbor, Inn, Village.",
    )


class ParseActionTests(unittest.TestCase):
    def test_valid_json(self):
        a = parse_action('{"op":"go_to_building","arg":"Harbor","why":"crew"}')
        self.assertEqual(a["op"], "go_to_building")
        self.assertEqual(a["arg"], "Harbor")

    def test_json_with_surrounding_prose(self):
        a = parse_action('Sure — here you go: {"op":"exit_building","arg":null} done')
        self.assertEqual(a["op"], "exit_building")

    def test_op_not_whitelisted_rejected(self):
        self.assertIsNone(parse_action('{"op":"spend_red_gem","arg":"99"}'))

    def test_garbage_rejected(self):
        self.assertIsNone(parse_action("no json here"))
        self.assertIsNone(parse_action(""))


class ReasonTests(unittest.TestCase):
    def test_shadow_still_calls_llm_and_logs_the_decision(self):
        # shadow captures WHAT the LLM would do (it calls the LLM); the caller/loop
        # is what refrains from acting.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "traces.jsonl"
            called = []
            action = reason(
                _ctx(),
                llm_fn=lambda pr: called.append(pr) or '{"op":"tap","arg":"Recruit"}',
                trigger="precondition_mismatch", shadow=True, trace_path=p)
            self.assertEqual(action["op"], "tap")        # decision captured
            self.assertEqual(len(called), 1)             # LLM WAS called in shadow
            rec = json.loads(p.read_text().strip())
            self.assertEqual(rec["trigger"], "precondition_mismatch")
            self.assertTrue(rec["shadow"])
            self.assertEqual(rec["parsed_action"]["op"], "tap")
            self.assertIn("crew=42/60 SHORT", rec["context_sent"]["world_model"])

    def test_live_mode_calls_llm_parses_and_logs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "traces.jsonl"
            llm = lambda prompt: '{"op":"go_to_building","arg":"Harbor","why":"crew short"}'
            action = reason(_ctx(), llm_fn=llm, shadow=False, trace_path=p)
            self.assertEqual(action["op"], "go_to_building")
            self.assertEqual(action["arg"], "Harbor")
            rec = json.loads(p.read_text().strip())
            self.assertEqual(rec["parsed_action"]["op"], "go_to_building")
            self.assertIn("go_to_building", rec["llm_response"])

    def test_prompt_contains_the_pieces(self):
        prompt = _ctx().to_prompt()
        for piece in ("GOAL", "WORLD MODEL", "OBSERVED", "GAME KNOWLEDGE",
                      "ALLOWED ACTIONS", "go_to_building"):
            self.assertIn(piece, prompt)


if __name__ == "__main__":
    unittest.main()
