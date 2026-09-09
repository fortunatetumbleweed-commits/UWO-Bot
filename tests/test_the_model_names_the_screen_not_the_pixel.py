"""A vision model says WHAT a screen is. It does not say WHERE to tap.

Live 2026-09-08 at Barcelona, the "Moon Rabbit's Part Gift Package" promo. `handle_unknown_
blocking` escalated to Claude Vision, which identified the card correctly and returned
tap_x=879, tap_y=109. The card's close-X is at (1764, 216) — nine hundred pixels away, out
on the world behind the popup. The tap was taken on trust:

    elif tap_x and tap_y:
        tap(int(tap_x), int(tap_y))

One Back cleared the same card, verified, still on the port overworld with no main menu.

The identification was worth having and is kept: it learned the interruptor
`moon_rabbit_s_part_gift_package_purchase`, whose record stores keywords and
`dismissal: "tap_close_button_only"` — a method, no coordinate — so the next encounter costs
no LLM call at all. Only the pixels were junk.

`a-fallback-fires-when-guessing-is-worst`: delete the coordinate when you add the lookup.
"""

from __future__ import annotations

import unittest
import unittest.mock as mock


class BackIsPreferredToAModelsCoordinates(unittest.TestCase):

    def _run(self, claude_answer):
        import brain.perceive as bp
        taps, backs = [], []
        with mock.patch("actions.adb_actions.tap",
                        lambda x, y, **k: taps.append((x, y))), \
             mock.patch("actions.adb_actions.press_back",
                        lambda **k: backs.append(True)), \
             mock.patch.object(bp, "_claude_resolve_unknown", lambda f, d: claude_answer), \
             mock.patch.object(bp, "_qwen_describe_unknown", lambda *a, **k: None), \
             mock.patch.object(bp, "_save_new_interruptor", lambda *a, **k: None), \
             mock.patch.object(bp, "_unknown_debounce_should_fire",
                               lambda *a, **k: (True, 3)), \
             mock.patch("time.sleep", lambda *_a: None):
            try:
                bp.handle_unknown_blocking(mock.Mock(width=2400, height=1080), [], "")
            except Exception:
                pass
        return taps, backs

    def test_the_offered_pixel_is_not_tapped(self):
        taps, backs = self._run({"description": "Moon Rabbit's Part Gift Package",
                                 "dismissal": "tap_close_button_only",
                                 "tap_x": 879, "tap_y": 109})
        self.assertNotIn((879, 109), taps,
                         "the model was 900px out; its coordinate must not reach the screen")
        self.assertTrue(backs, "Back is what clears this card")


class TheLearnedRecordCarriesAMethodNotAPlace(unittest.TestCase):
    """What makes the identification worth keeping — and safe to keep."""

    def test_the_moon_rabbit_record_has_no_coordinate(self):
        import json
        from pathlib import Path
        p = Path("memory/knowledge/fsm/interruptors.json")
        if not p.exists():
            self.skipTest("no interruptor KB in this checkout")
        node = json.loads(p.read_text())
        node = node.get("interruptors", node) if isinstance(node, dict) else node
        moon = [v for v in node if "moon_rabbit" in json.dumps(v).lower()]
        if not moon:
            self.skipTest("this run has not met the promo")
        rec = moon[0]
        self.assertEqual("tap_close_button_only", rec.get("dismissal"))
        for key in ("tap_x", "tap_y", "close_position"):
            self.assertNotIn(key, rec, "a learned record must not bake in a pixel")


class TheConsultIsVisibleInTheReport(unittest.TestCase):
    """The bad coordinate had to be inferred from actions.jsonl. It should be readable."""

    def test_the_screen_id_call_goes_through_the_llm_trace(self):
        import inspect
        import brain.perceive as bp
        src = inspect.getsource(bp.handle_unknown_blocking)
        self.assertIn("llm_trace", src)
        self.assertIn("claude (unknown screen)", src)


if __name__ == "__main__":
    unittest.main()
