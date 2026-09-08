"""Every source scarce is a reason to stay home — and a switch, because sometimes it isn't.

The rule (user, 2026-08-30): "if the bot sees a port is at low stock, it records the data and
replan immediately... If all ports have low stock, then just abandon the task as
non-profitable for the season."

Only the DETECTION was ever built. `plan_barter_task` logged

    [plan] every known source is scarce this season for ['Coral'] — this task is not
           profitable now

and carried straight on: `low_everywhere` was computed, never returned on the plan, and read
by nobody. The rule was a warning no one obeyed.

And it needs an override (user, 2026-09-07): "I would like make it a switch that can be on
and off, this time it is for a special mission, so even if stocks are low for materials, it
should still complete the task." The stock is thin, not absent — a scarce shelf still
refreshes, at about a quarter the yield — so sailing anyway is slower and smaller, not
impossible.

The test is deliberately narrow: `low_everywhere` requires EVERY known source of a material
to be recorded scarce inside the season record's 72h life. One unvisited port keeps the
mission running, which is why the Hutu run of 2026-09-07 sailed correctly with Madeira and
Bordeaux both low and Trabzon never visited.
"""

from __future__ import annotations

import unittest

from brain.barter_mission_live import TaskPlan


def _plan(**kw):
    base = dict(good="Box of Nutmeg", village="Melanesian Village", sell_port="Jakarta",
                rounds=5, needs={"Coral": 100}, gather_route=["Male"],
                purchases={"Male": {"Coral": 100}}, unsourced=[], total_output=500)
    base.update(kw)
    return TaskPlan(**base)


class ThePlanCarriesTheFinding(unittest.TestCase):
    """It used to be logged and dropped, so nothing downstream could act on it."""

    def test_a_plan_reports_which_materials_are_scarce_everywhere(self):
        self.assertEqual(_plan(low_everywhere=["Coral"]).low_everywhere, ["Coral"])

    def test_and_an_ordinary_plan_reports_none(self):
        self.assertEqual(_plan().low_everywhere, [])


class TheSwitch(unittest.TestCase):

    def _decide(self, plan, *, allow):
        """The refusal exactly as `_run_mission_for` makes it."""
        if plan.low_everywhere and not allow:
            return {"ok": False, "step": "gather-plan",
                    "reason": f"every known source is scarce this season for "
                              f"{plan.low_everywhere} — not profitable now; pass "
                              "--ignore-low-stock to sail anyway"}
        return {"ok": True}

    def test_OFF_by_default_the_mission_stays_home(self):
        out = self._decide(_plan(low_everywhere=["Coral"]), allow=False)
        self.assertFalse(out["ok"])
        self.assertIn("Coral", out["reason"])
        self.assertIn("--ignore-low-stock", out["reason"], "the refusal says how to override")

    def test_ON_it_sails_anyway(self):
        self.assertTrue(self._decide(_plan(low_everywhere=["Coral"]), allow=True)["ok"])

    def test_the_switch_changes_nothing_when_stock_is_fine(self):
        for allow in (True, False):
            with self.subTest(allow=allow):
                self.assertTrue(self._decide(_plan(), allow=allow)["ok"])


class TheFlagReachesTheCommand(unittest.TestCase):

    def test_run_barter_command_takes_it(self):
        import inspect
        from brain.barter_command import run_barter_command
        self.assertIn("allow_low_stock",
                      inspect.signature(run_barter_command).parameters)

    def test_it_defaults_to_staying_home(self):
        import inspect
        from brain.barter_command import run_barter_command
        p = inspect.signature(run_barter_command).parameters["allow_low_stock"]
        self.assertIs(p.default, False, "the rule is the default; sailing anyway is opt-in")

    def test_the_cli_exposes_it(self):
        from pathlib import Path
        src = Path("run_barter.py").read_text()
        self.assertIn('"--ignore-low-stock" in args', src)
        self.assertIn("allow_low_stock=allow_low_stock", src)


if __name__ == "__main__":
    unittest.main()
