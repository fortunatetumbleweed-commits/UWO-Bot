"""Claude llm_fn — graceful degradation, and a live smoke when a key is present."""
import os
import unittest

from brain.llm_client import claude_llm_fn, available

_HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))


class GracefulTests(unittest.TestCase):
    @unittest.skipIf(_HAS_KEY, "key present — graceful path not exercised")
    def test_no_key_returns_empty(self):
        self.assertFalse(available())
        self.assertEqual(claude_llm_fn("anything"), "")


class LiveTests(unittest.TestCase):
    @unittest.skipUnless(_HAS_KEY, "needs ANTHROPIC_API_KEY")
    def test_live_reasoning_returns_valid_action(self):
        from brain.reasoning import ReasoningContext, reason, ACTION_OPS
        from brain.world_model import WorldModel, Fleet, Ship
        wm = WorldModel(fleets=[Fleet(location="London", current_building="Inn",
                                      crew_current=42, crew_capacity=60,
                                      ships=[Ship("Barque", 88)])])
        ctx = ReasoningContext(
            goal="Depart London → Amsterdam; the fleet cannot depart.",
            world_model=wm.to_prompt(),
            perception="base=panel context=Inn menu=[Recruit,Hire,Party] title='Inn'",
            game_knowledge="Recruit crew at Harbor/Inn/Village via 'Recruit'. Need full crew to sail.",
        )
        action = reason(ctx, llm_fn=claude_llm_fn, shadow=False)
        self.assertIsNotNone(action, "Claude returned an unparseable/invalid action")
        self.assertIn(action["op"], ACTION_OPS)


if __name__ == "__main__":
    unittest.main()
