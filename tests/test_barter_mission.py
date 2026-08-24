"""Tests for the barter PHASE loop (round budget + stop conditions) — mocked commits."""
import types
import unittest

from brain.barter_mission import run_barter_phase


def _play(rounds=3):
    return types.SimpleNamespace(good="Camas", village="Apache Village",
                                 sell_port="Edinburgh", rounds=rounds)


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(reason):
    return {"ok": False, "reason": reason}


class BarterPhaseTests(unittest.TestCase):
    def test_runs_target_rounds(self):
        commits = []
        r = run_barter_phase(
            _play(rounds=3),
            read_state_fn=lambda: {"rounds_remaining": 5, "overflow": 0},
            commit_fn=lambda: (commits.append(1) or {"ok": True}))
        self.assertTrue(r["ok"])
        self.assertEqual(r["committed"], 3)

    def test_stops_when_rounds_exhausted(self):
        state = {"n": 0}
        def read():
            state["n"] += 1
            return {"rounds_remaining": 2 - state["n"] + 1, "overflow": 0}
        r = run_barter_phase(_play(rounds=10),
                             read_state_fn=lambda: {"rounds_remaining": 0},
                             commit_fn=lambda: {"ok": True})
        self.assertEqual(r["committed"], 0)

    def test_stalled_commit_escalates(self):
        r = run_barter_phase(
            _play(rounds=5),
            read_state_fn=lambda: {"rounds_remaining": 5, "overflow": 0},
            commit_fn=lambda: {"ok": False})
        self.assertFalse(r["ok"])
        self.assertIn("stalled", r["reason"])

    def test_jettison_called_on_overflow(self):
        dumped = []
        reads = iter([{"rounds_remaining": 5, "overflow": 0},   # pre-commit
                      {"rounds_remaining": 5, "overflow": 143},  # post-commit → overflow
                      {"rounds_remaining": 0, "overflow": 0}])   # stop
        r = run_barter_phase(
            _play(rounds=2),
            read_state_fn=lambda: next(reads),
            commit_fn=lambda: {"ok": True},
            jettison_fn=lambda ov: dumped.append(ov))
        self.assertEqual(dumped, [143])


if __name__ == "__main__":
    unittest.main()
