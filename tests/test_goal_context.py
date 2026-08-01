"""GoalContext stack — Phase B1.

Foundation slot for goal-aware perception.  Consumers (B2: LLM
consult; C: re-check goal after action) read it; this module just
manages the stack.

Tests pin push/pop ordering, nested contexts, the context-manager
shape, and crash-safety on exception.
"""

from __future__ import annotations

import unittest

import brain.goal_context as gc


class StackBasicsTests(unittest.TestCase):

    def setUp(self):
        gc.clear_goal_stack()

    def test_empty_stack_returns_none(self):
        self.assertIsNone(gc.current_goal())
        self.assertEqual(gc.goal_stack(), [])

    def test_push_records_intent_and_target(self):
        ctx = gc.push_goal(
            "set_sail",
            target={"destination": "Lisbon"},
            success_signals=["at_sea"],
            progress_signals=["sailing_hud_visible"],
        )
        self.assertEqual(ctx.intent, "set_sail")
        self.assertEqual(ctx.target, {"destination": "Lisbon"})
        self.assertEqual(ctx.success_signals, ("at_sea",))
        self.assertIsNone(ctx.parent)
        self.assertIs(gc.current_goal(), ctx)

    def test_push_pop_round_trip(self):
        a = gc.push_goal("set_sail")
        popped = gc.pop_goal()
        self.assertIs(popped, a)
        self.assertIsNone(gc.current_goal())

    def test_pop_empty_is_safe(self):
        self.assertIsNone(gc.pop_goal())   # no-op, no exception

    def test_nested_pushes_chain_via_parent(self):
        a = gc.push_goal("set_sail", target={"destination": "Lisbon"})
        b = gc.push_goal("recruit_crew", target={"building": "harbor"})
        c = gc.push_goal("tap_recruit_button")
        self.assertIs(c.parent, b)
        self.assertIs(b.parent, a)
        self.assertIsNone(a.parent)
        self.assertIs(gc.current_goal(), c)
        # Chain returns top-down to root
        chain = c.chain()
        self.assertEqual([g.intent for g in chain],
                         ["tap_recruit_button", "recruit_crew", "set_sail"])

    def test_summary_renders_chain(self):
        gc.push_goal("set_sail",     target={"destination": "Lisbon"})
        gc.push_goal("recruit_crew", target={"building": "harbor"})
        self.assertEqual(
            gc.current_goal().summary(),
            "recruit_crew @ harbor → set_sail @ Lisbon",
        )


class ContextManagerTests(unittest.TestCase):

    def setUp(self):
        gc.clear_goal_stack()

    def test_with_block_pushes_and_pops(self):
        self.assertIsNone(gc.current_goal())
        with gc.goal("set_sail", target={"destination": "Lisbon"}):
            inside = gc.current_goal()
            self.assertIsNotNone(inside)
            self.assertEqual(inside.intent, "set_sail")
        self.assertIsNone(gc.current_goal(),
                          "context manager must pop on normal exit")

    def test_with_block_pops_on_exception(self):
        class _Boom(Exception):
            pass
        with self.assertRaises(_Boom):
            with gc.goal("set_sail"):
                self.assertIsNotNone(gc.current_goal())
                raise _Boom()
        self.assertIsNone(gc.current_goal(),
                          "context manager must pop on exception")

    def test_nested_with_blocks(self):
        with gc.goal("set_sail", target={"destination": "Lisbon"}):
            self.assertEqual(gc.current_goal().intent, "set_sail")
            with gc.goal("recruit_crew", target={"building": "harbor"}):
                self.assertEqual(gc.current_goal().intent, "recruit_crew")
                self.assertEqual(gc.current_goal().parent.intent, "set_sail")
            self.assertEqual(gc.current_goal().intent, "set_sail")
        self.assertIsNone(gc.current_goal())

    def test_yielded_value_is_the_pushed_context(self):
        with gc.goal("set_sail") as ctx:
            self.assertIs(ctx, gc.current_goal())


class CrashSafetyTests(unittest.TestCase):
    """Defensive: if something else mutates the stack out from under
    a context-manager pop, the with-block should not corrupt it
    further by popping someone else's context."""

    def setUp(self):
        gc.clear_goal_stack()

    def test_concurrent_mutation_does_not_corrupt_stack(self):
        outer = gc.push_goal("outer")
        try:
            with gc.goal("inner"):
                inner = gc.current_goal()
                # Simulate someone else popping our context
                gc.pop_goal()
                # The with-block's finally clause must NOT pop the outer
                # context now that it's at the top.
            # After the with-block, only `outer` should remain — the
            # context manager noticed the top wasn't its own and didn't pop.
            self.assertIs(gc.current_goal(), outer)
        finally:
            gc.clear_goal_stack()


if __name__ == "__main__":
    unittest.main()
