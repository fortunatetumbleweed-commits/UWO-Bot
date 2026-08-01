"""
Tests for the flow-completeness guideline (CLAUDE.md → "Flow Completeness &
Self-Correction").

Covers:
- categorize_step heuristics (transaction / navigation / dismissal / cancel)
- is_flow_complete() rule: terminal_state_recognized AND has positive transaction
- FSMFlow loads and persists the new completeness fields
- record_flow_outcome flips status to STATUS_INCOMPLETE when conditions fail
- The audit pass classifies the recruit-crew failure pattern as INCOMPLETE
  when the actually-executed step set lacks a transaction
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import brain.fsm_registry as fsm_registry
from brain.fsm_registry import FSMRegistry
from brain.flow_completeness import (
    STATUS_COMPLETE, STATUS_INCOMPLETE, STATUS_CANCELLED, STATUS_UNKNOWN,
    CATEGORY_TRANSACTION, CATEGORY_NAVIGATION,
    CATEGORY_DISMISSAL,   CATEGORY_CANCEL,
    categorize_step, has_positive_transaction,
    is_flow_complete, evaluate_flow_status,
)


# ── Categoriser ───────────────────────────────────────────────────────────────

class CategorizeStepTests(unittest.TestCase):

    def test_recruit_label_alone_is_transaction(self):
        step = {
            "id": "step_2",
            "recovery_action": "tap_button",
            "recovery_button_labels": ["recruit"],
            "detection": "A confirmation dialog appears.",
        }
        self.assertEqual(categorize_step(step), CATEGORY_TRANSACTION)

    def test_recruit_crew_menu_item_is_navigation(self):
        """The 'recruit crew' menu item that opens a sub-menu — not a transaction."""
        step = {
            "id": "step_1",
            "recovery_action": "tap_button",
            "recovery_button_labels": ["recruit crew"],
            "detection": "A crew recruitment dialog or sub-menu opens.",
        }
        self.assertEqual(categorize_step(step), CATEGORY_NAVIGATION)

    def test_purchase_step_is_transaction(self):
        step = {
            "id": "basket",
            "recovery_action": "tap_button",
            "recovery_button_labels": ["purchase"],
        }
        self.assertEqual(categorize_step(step), CATEGORY_TRANSACTION)

    def test_result_step_with_ok_label_is_dismissal(self):
        step = {
            "id": "result",
            "recovery_action": "tap_button",
            "recovery_button_labels": ["ok", "close"],
        }
        self.assertEqual(categorize_step(step), CATEGORY_DISMISSAL)

    def test_wait_action_is_dismissal(self):
        step = {"id": "loading", "recovery_action": "wait"}
        self.assertEqual(categorize_step(step), CATEGORY_DISMISSAL)

    def test_press_back_is_cancel(self):
        step = {"id": "x", "recovery_action": "press_back"}
        self.assertEqual(categorize_step(step), CATEGORY_CANCEL)

    def test_label_back_is_cancel(self):
        step = {"recovery_action": "tap_button", "recovery_button_labels": ["back"]}
        self.assertEqual(categorize_step(step), CATEGORY_CANCEL)

    def test_explicit_category_field_is_honoured(self):
        step = {
            "id": "step_1",
            "recovery_button_labels": ["recruit crew"],
            "category": CATEGORY_TRANSACTION,  # forced
        }
        self.assertEqual(categorize_step(step), CATEGORY_TRANSACTION)

    def test_confirm_step_id_is_transaction_even_with_ok_label(self):
        step = {
            "id": "confirm",
            "recovery_action": "tap_button",
            "recovery_button_labels": ["ok"],
        }
        # 'ok' on its own categorises as dismissal, but step.id 'confirm' overrides.
        # The categoriser prefers label-keyword first, so 'ok' alone falls
        # through to step-id semantics → CATEGORY_TRANSACTION.
        self.assertEqual(categorize_step(step), CATEGORY_TRANSACTION)


# ── Completeness rule ─────────────────────────────────────────────────────────

class IsFlowCompleteTests(unittest.TestCase):

    def test_complete_when_recognized_terminal_and_transaction(self):
        flow = {
            "terminal_state": "building",
            "steps": [
                {"id": "confirm", "recovery_button_labels": ["recruit"]},
                {"id": "result", "recovery_button_labels": ["ok"]},
            ],
        }
        self.assertTrue(is_flow_complete(flow, terminal_state_recognized=True))

    def test_incomplete_when_terminal_unrecognized(self):
        flow = {
            "terminal_state": "ghost_state",
            "steps": [{"id": "confirm", "recovery_button_labels": ["recruit"]}],
        }
        self.assertFalse(is_flow_complete(flow, terminal_state_recognized=False))

    def test_incomplete_when_no_transaction(self):
        flow = {
            "terminal_state": "building",
            "steps": [
                {"id": "step_1", "recovery_button_labels": ["recruit crew"],
                 "detection": "A sub-menu opens."},
                {"id": "result", "recovery_button_labels": ["ok"]},
            ],
        }
        # navigation + dismissal — no transaction
        self.assertFalse(is_flow_complete(flow, terminal_state_recognized=True))

    def test_evaluate_flow_status_cancelled_overrides(self):
        flow = {"terminal_state": "building",
                "steps": [{"id": "confirm", "recovery_button_labels": ["recruit"]}]}
        status = evaluate_flow_status(flow, terminal_state_recognized=True, cancelled=True)
        self.assertEqual(status, STATUS_CANCELLED)

    def test_evaluate_flow_status_complete(self):
        flow = {"terminal_state": "building",
                "steps": [{"id": "confirm", "recovery_button_labels": ["recruit"]}]}
        status = evaluate_flow_status(flow, terminal_state_recognized=True, cancelled=False)
        self.assertEqual(status, STATUS_COMPLETE)

    def test_evaluate_flow_status_incomplete(self):
        flow = {"terminal_state": "building", "steps": []}
        status = evaluate_flow_status(flow, terminal_state_recognized=True, cancelled=False)
        self.assertEqual(status, STATUS_INCOMPLETE)


# ── FSMFlow schema persistence ────────────────────────────────────────────────

class FSMFlowSchemaPersistenceTests(unittest.TestCase):
    """The new completeness fields load with sane defaults and persist on save."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="flow_completeness_"))
        self._original_kb_dir = fsm_registry._KB_DIR
        fsm_registry._KB_DIR = self._tmp
        (self._tmp / "states.json").write_text(json.dumps([
            {"id": "port_overworld", "exits": [], "flows": []},
            {"id": "building", "exits": [
                {"action": "tap_home", "to": "port_overworld"}
            ], "flows": []},
        ]))
        (self._tmp / "flows.json").write_text(json.dumps([{
            "id": "test_flow",
            "description": "test",
            "parent_state": "building",
            "atomic": True,
            "steps": [
                {"id": "confirm", "recovery_action": "tap_button",
                 "recovery_button_labels": ["recruit"]}
            ],
            "terminal_state": "building",
        }]))
        (self._tmp / "interruptors.json").write_text("[]")
        fsm_registry._instance = None

    def tearDown(self):
        shutil.rmtree(self._tmp)
        fsm_registry._KB_DIR = self._original_kb_dir
        fsm_registry._instance = None

    def test_loads_with_default_status_unknown(self):
        reg = FSMRegistry().load()
        flow = reg.flows["test_flow"]
        self.assertEqual(flow.status, STATUS_UNKNOWN)
        self.assertEqual(flow.positive_transaction_count, 0)
        self.assertIsNone(flow.terminal_state_recognized)

    def test_record_flow_outcome_flips_to_complete(self):
        reg = FSMRegistry().load()
        reg.record_flow_outcome(
            "test_flow", success=True, duration_secs=1.0,
            positive_transaction_count=1,
            terminal_state_recognized=True,
            cancelled=False,
        )
        flow = reg.flows["test_flow"]
        self.assertEqual(flow.status, STATUS_COMPLETE)
        self.assertEqual(flow.positive_transaction_count, 1)
        self.assertTrue(flow.terminal_state_recognized)
        # Persists to disk
        reg2 = FSMRegistry().load()
        self.assertEqual(reg2.flows["test_flow"].status, STATUS_COMPLETE)

    def test_record_flow_outcome_flips_to_incomplete_when_no_transaction(self):
        reg = FSMRegistry().load()
        reg.record_flow_outcome(
            "test_flow", success=False, duration_secs=1.0,
            positive_transaction_count=0,
            terminal_state_recognized=True,
            cancelled=False,
        )
        self.assertEqual(reg.flows["test_flow"].status, STATUS_INCOMPLETE)

    def test_record_flow_outcome_flips_to_cancelled(self):
        reg = FSMRegistry().load()
        reg.record_flow_outcome(
            "test_flow", success=False, duration_secs=1.0,
            positive_transaction_count=0,
            terminal_state_recognized=True,
            cancelled=True,
        )
        self.assertEqual(reg.flows["test_flow"].status, STATUS_CANCELLED)

    def test_record_flow_outcome_without_signals_leaves_status_unchanged(self):
        """Legacy callers (no positive_transaction_count) don't update status."""
        reg = FSMRegistry().load()
        reg.record_flow_outcome("test_flow", success=True, duration_secs=1.0)
        # Status stays at default STATUS_UNKNOWN (legacy callers don't track it).
        self.assertEqual(reg.flows["test_flow"].status, STATUS_UNKNOWN)


# ── Recruit-crew loop reproduction ────────────────────────────────────────────

class RecruitCrewIncompleteSimulationTests(unittest.TestCase):
    """
    Reproduce the 2026-05-02 recruit-crew loop bookkeeping.

    Bot enters Recruit Crew sub_menu (executes step_1 = navigation), then
    sail_to recovers via Home before step_2 (the gold Recruit transaction)
    can fire.  The runner reports zero positive transactions and Cancel.
    record_flow_outcome must flip status → CANCELLED so the perceive guard
    skips the flow on the next encounter.
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="recruit_crew_test_"))
        self._original_kb_dir = fsm_registry._KB_DIR
        fsm_registry._KB_DIR = self._tmp
        (self._tmp / "states.json").write_text(json.dumps([
            {"id": "port_overworld", "exits": [], "flows": []},
            {"id": "building", "exits": [
                {"action": "tap_home", "to": "port_overworld"}
            ], "flows": []},
        ]))
        (self._tmp / "flows.json").write_text(json.dumps([{
            "id": "learned_recruit_crew",
            "description": "Recruit-crew loop reproduction",
            "parent_state": "building",
            "atomic": True,
            "steps": [
                {"id": "step_1", "recovery_action": "tap_button",
                 "recovery_button_labels": ["recruit crew"],
                 "detection": "A sub-menu opens."},
                {"id": "step_2", "recovery_action": "tap_button",
                 "recovery_button_labels": ["recruit"],
                 "detection": "A confirmation dialog appears."},
            ],
            "terminal_state": "building",
        }]))
        (self._tmp / "interruptors.json").write_text("[]")
        fsm_registry._instance = None

    def tearDown(self):
        shutil.rmtree(self._tmp)
        fsm_registry._KB_DIR = self._original_kb_dir
        fsm_registry._instance = None

    def test_navigation_only_attempt_marks_flow_cancelled(self):
        reg = FSMRegistry().load()
        # Simulate: step_1 was executed (navigation), then the runner
        # cancelled via Home.  Tracker reports positive_count=0, cancelled=True.
        reg.record_flow_outcome(
            "learned_recruit_crew",
            success=False,
            duration_secs=5.0,
            positive_transaction_count=0,
            terminal_state_recognized=True,  # bot is at port_overworld (recognized)
            cancelled=True,
        )
        flow = reg.flows["learned_recruit_crew"]
        self.assertEqual(flow.status, STATUS_CANCELLED)
        self.assertEqual(flow.last_attempt_outcome, "cancelled")

    def test_full_attempt_with_transaction_marks_flow_complete(self):
        reg = FSMRegistry().load()
        # Simulate: both steps executed (step_2 is the transaction).
        reg.record_flow_outcome(
            "learned_recruit_crew",
            success=True,
            duration_secs=12.0,
            positive_transaction_count=1,
            terminal_state_recognized=True,
            cancelled=False,
        )
        self.assertEqual(reg.flows["learned_recruit_crew"].status, STATUS_COMPLETE)


# ── Module-level tracker singleton ────────────────────────────────────────────

class FlowTrackerSingletonTests(unittest.TestCase):
    """
    The module-level _FlowAttemptTracker singleton is fed by
    notify_perceive_result on every perceive tick.  It must:
      - initialise on first flow detection
      - record outcome (incomplete / cancelled) when the perceived flow changes
      - clear itself afterwards
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="flow_tracker_singleton_"))
        self._original_kb_dir = fsm_registry._KB_DIR
        fsm_registry._KB_DIR = self._tmp
        (self._tmp / "states.json").write_text(json.dumps([
            {"id": "port_overworld", "exits": [], "flows": []},
            {"id": "building", "exits": [], "flows": []},
            {"id": "sub_menu", "exits": [], "flows": []},
        ]))
        (self._tmp / "flows.json").write_text(json.dumps([{
            "id": "test_recruit_flow",
            "description": "test",
            "parent_state": "building",
            "atomic": True,
            "steps": [
                {"id": "step_1", "recovery_action": "tap_button",
                 "recovery_button_labels": ["recruit crew"],
                 "detection": "A sub-menu opens."},
                {"id": "step_2", "recovery_action": "tap_button",
                 "recovery_button_labels": ["recruit"]},
            ],
            "terminal_state": "building",
        }]))
        (self._tmp / "interruptors.json").write_text("[]")
        fsm_registry._instance = None
        # Clear the tracker so each test starts clean.
        from brain.recovery import reset_active_tracker
        reset_active_tracker()

    def tearDown(self):
        shutil.rmtree(self._tmp)
        fsm_registry._KB_DIR = self._original_kb_dir
        fsm_registry._instance = None
        from brain.recovery import reset_active_tracker
        reset_active_tracker()

    def _stub_result(self, state: str, flow=None, flow_step=None):
        """Tiny duck-type stand-in for PerceiveResult."""
        class R:
            pass
        r = R()
        r.state = state
        r.flow = flow
        r.flow_step = flow_step
        return r

    def test_perceive_sequence_drives_status_to_incomplete(self):
        """
        Reproduce the recruit-crew loop: tick #1 sees the flow active,
        tick #2 sees it gone (bot was cancelled to overworld) — without
        any positive transaction.  Flow status must flip to INCOMPLETE.
        """
        from brain.recovery import notify_perceive_result, get_active_tracker
        # Tick 1: flow is active at step_1
        notify_perceive_result(self._stub_result("building", "test_recruit_flow", "step_1"))
        self.assertIsNotNone(get_active_tracker())
        # Tick 2: flow is gone, bot is back at port_overworld (no transaction executed)
        notify_perceive_result(self._stub_result("port_overworld"))
        self.assertIsNone(get_active_tracker())
        reg = FSMRegistry().load()
        # Reload to check persisted status
        flow = reg.flows["test_recruit_flow"]
        self.assertEqual(flow.status, STATUS_INCOMPLETE)
        self.assertEqual(flow.positive_transaction_count, 0)

    def test_perceive_sequence_with_transaction_marks_complete(self):
        """When the runner executes a transaction step before the flow
        exits, the singleton must record COMPLETE."""
        from brain.recovery import (
            notify_perceive_result, get_active_tracker, reset_active_tracker,
        )
        reset_active_tracker()
        notify_perceive_result(self._stub_result("building", "test_recruit_flow", "step_1"))
        # Simulate the runner executing step_2 (the transaction).
        tracker = get_active_tracker()
        self.assertIsNotNone(tracker)
        tracker.note_step_executed("test_recruit_flow", "step_2")
        # Flow exits to building (its terminal_state).
        notify_perceive_result(self._stub_result("building"))
        reg = FSMRegistry().load()
        flow = reg.flows["test_recruit_flow"]
        self.assertEqual(flow.status, STATUS_COMPLETE)
        self.assertEqual(flow.positive_transaction_count, 1)


if __name__ == "__main__":
    unittest.main()
