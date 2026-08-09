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


if __name__ == "__main__":
    unittest.main()
