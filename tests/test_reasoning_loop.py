"""Reasoning loop — shadow logging, live execute, done/abort/refuse, bound."""
import json
import tempfile
import unittest
from pathlib import Path

from brain.reasoning_loop import resolve, Observation, describe_perceived
from brain.perceived_state import PerceivedState
from brain.world_model import WorldModel, Fleet


def _inn_obs():
    return Observation(
        perceived=PerceivedState(base="panel", context="Inn", menu_item="Hire"),
        port="London",
        menu=["Recruit", "Hire", "Party"],
        buttons=["Recruit", "Hire", "Back"],
        frame=object(),
    )


class DescribeTests(unittest.TestCase):
    def test_describe_includes_menu_and_buttons(self):
        t = describe_perceived(PerceivedState(base="panel", context="Inn",
                                              menu_item="Hire"),
                               menu=["Recruit", "Hire"], buttons=["Recruit"])
        self.assertIn("context=Inn", t)
        self.assertIn("selected=Hire", t)
        self.assertIn("menu items: Recruit, Hire", t)
        self.assertIn("buttons: Recruit", t)

    def test_describe_renders_commit_element_with_cost(self):
        inv = [{"id": "e1", "label": "Recruit", "type": "commit",
                "region": "RIGHT-PANEL", "cx": 2076, "cy": 940, "cost": "205,848"}]
        t = describe_perceived(PerceivedState(base="panel", context="harbor"),
                               elements=inv)
        self.assertIn("[COMMIT] Recruit — cost 205,848", t)


class LoopTests(unittest.TestCase):
    def _wm(self):
        wm = WorldModel(fleets=[Fleet(location="London")])
        return wm

    def test_shadow_logs_one_trace_no_action(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            executed = []
            out = resolve(
                "Depart — crew short", self._wm(),
                observe_fn=_inn_obs,
                llm_fn=lambda pr: '{"op":"tap","arg":"Recruit","why":"x"}',
                execute_fn=lambda a, frame=None, **kw: executed.append(a),
                shadow=True, trace_path=p,
            )
            self.assertFalse(out["resolved"])
            self.assertIn("shadow", out["reason"])
            self.assertEqual(executed, [])                       # never acted
            self.assertEqual(len(p.read_text().strip().splitlines()), 1)  # one trace

    def test_live_executes_the_action(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            executed = []

            class R:  # mock ExecResult
                ok, note, refused = True, "tap(Recruit)", False
            out = resolve(
                "Depart — crew short", self._wm(),
                observe_fn=_inn_obs,
                llm_fn=lambda pr: '{"op":"tap","arg":"Recruit","why":"x"}',
                execute_fn=lambda a, frame=None, **kw: executed.append(a) or R(),
                done_fn=lambda wm, obs: len(executed) >= 1,     # done after one act
                shadow=False, trace_path=p,
            )
            self.assertTrue(out["resolved"])
            self.assertEqual(len(executed), 1)
            self.assertEqual(executed[0]["arg"], "Recruit")

    def test_abort_stops(self):
        with tempfile.TemporaryDirectory() as d:
            out = resolve(
                "impossible", self._wm(), observe_fn=_inn_obs,
                llm_fn=lambda pr: '{"op":"abort","why":"no crew anywhere"}',
                execute_fn=lambda a, frame=None, **kw: None,
                shadow=False, trace_path=Path(d) / "t.jsonl",
            )
            self.assertFalse(out["resolved"])
            self.assertIn("aborted", out["reason"])

    def test_world_model_updated_from_observation(self):
        with tempfile.TemporaryDirectory() as d:
            wm = self._wm()
            resolve("x", wm, observe_fn=_inn_obs,
                    llm_fn=lambda pr: "{}", execute_fn=lambda a, frame=None, **kw: None,
                    shadow=True, trace_path=Path(d) / "t.jsonl")
            self.assertEqual(wm.fleet.current_building, "Inn")   # folded in


class TaskExecutorTests(unittest.TestCase):
    """Attempt memory + progress/stuck detection (the self-correcting executor)."""
    class _R:
        ok, note, refused = True, "ok", False

    def test_progressed_by_signature_or_hud(self):
        from brain.reasoning_loop import _progressed, Observation

        def mk(buttons, hud=None):
            return Observation(
                perceived=PerceivedState(base="panel", context="Market", menu_item="Buy"),
                elements=[{"label": b, "type": "button", "cx": 1, "cy": 1, "region": "C"}
                          for b in buttons],
                hud=hud or {})
        self.assertFalse(_progressed(mk(["Buy", "Back"]), mk(["Buy", "Back"])))
        self.assertTrue(_progressed(mk(["Buy", "Back"]), mk(["Buy", "Confirm"])))  # buttons changed
        self.assertTrue(_progressed(mk(["Buy"], {"cargo": (10, 100)}),
                                    mk(["Buy"], {"cargo": (20, 100)})))            # hud changed

    def test_stuck_breaks_the_repeat_loop(self):
        # same screen + same action every step → must declare STUCK, not run to max
        out = resolve("goal", WorldModel(fleets=[Fleet(location="London")]),
                      observe_fn=_inn_obs,
                      llm_fn=lambda pr: '{"op":"tap","arg":"Recruit"}',
                      execute_fn=lambda a, frame=None, **kw: TaskExecutorTests._R(),
                      shadow=False, max_steps=10)
        self.assertTrue(out["reason"].startswith("stuck"), out["reason"])
        self.assertLess(len(out["steps"]), 10)                 # bailed early

    def test_avoid_list_reaches_the_llm_after_no_progress(self):
        prompts = []
        resolve("goal", WorldModel(fleets=[Fleet(location="London")]),
                observe_fn=_inn_obs,
                llm_fn=lambda pr: prompts.append(pr) or '{"op":"tap","arg":"Recruit"}',
                execute_fn=lambda a, frame=None, **kw: TaskExecutorTests._R(),
                shadow=False, max_steps=10)
        self.assertTrue(any("ALREADY TRIED" in p and "tap:Recruit" in p for p in prompts))

    def test_cache_hit_replays_action_without_calling_llm(self):
        import pathlib
        import tempfile
        from brain.decision_cache import DecisionCache
        from brain.reasoning_loop import _screen_sig

        obs = _inn_obs()
        cache = DecisionCache(path=pathlib.Path(tempfile.mktemp()), persist=False)
        cache.record(_screen_sig(obs), {"op": "tap", "arg": "Recruit"})
        prompts = []
        out = resolve("goal", WorldModel(fleets=[Fleet(location="London")]),
                      observe_fn=lambda: obs,
                      llm_fn=lambda pr: prompts.append(pr) or '{"op":"tap","arg":"WRONG"}',
                      execute_fn=lambda a, frame=None, **kw: TaskExecutorTests._R(),
                      shadow=False, max_steps=1, decision_cache=cache)
        self.assertEqual(out["steps"][0]["action"]["arg"], "Recruit")  # from cache
        self.assertTrue(out["steps"][0].get("cached"))
        self.assertEqual(prompts, [])                                  # LLM never called


if __name__ == "__main__":
    unittest.main()
