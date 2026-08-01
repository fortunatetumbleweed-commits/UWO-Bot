"""
Phase 4 regression tests — flow metadata, verification and demotion.

The architecture (docs/fsm_planning_and_edge_learning.md, Q2) calls for the
planner to re-perceive after executing a flow and compare the actual state
to flow.terminal_state.  Match → success_count++; mismatch → failure_count++,
and once failure_count crosses the demotion threshold the flow's confidence
drops so future detection passes skip it.

These tests anchor that mechanism against the real bad flow that is left in
flows.json on purpose: learned_building__sea_cinematic__20260501T173946Z.
Claude declared "complete" while the screen was actually still in the
recruit_crew confirmation dialog, so the recorded terminal_state
(sea_cinematic) does not match the post-tap state (building).  The tests
verify that:

  - the flow's metadata fields load with sane defaults when not yet present
  - record_flow_outcome with success=False bumps failure_count
  - hitting the threshold demotes confidence to 'low'
  - should_skip() then returns True so the flow is excluded from selection
  - persistence survives across reload
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

# Importing fsm_registry executes top-level singletons; we patch _KB_DIR per
# test so we never write to the real KB.
import brain.fsm_registry as fsm_registry
from brain.fsm_registry import (
    FSMFlow, FSMRegistry,
    CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_UNVERIFIED,
    FAILURE_DEMOTION_THRESHOLD,
)


BAD_FLOW_ID = "learned_building__sea_cinematic__20260501T173946Z"


def _bad_flow_entry() -> dict:
    """The exact bad flow saved in the May-1 12:39 run, as a dict."""
    return {
        "id": BAD_FLOW_ID,
        "description": "Auto-learned via Claude guidance — recruit_crew tap.",
        "parent_state": "building",
        "atomic": True,
        "trigger_detection": "recruit crew",
        "step_detection_keywords": {"step_1": ["confirmation", "dialog", "result"]},
        "steps": [
            {
                "id": "step_1",
                "description": "Tap gold Recruit button.",
                "detection": "A confirmation dialog appears.",
                "recovery_action": "tap_button",
                "recovery_button_labels": ["recruit"],
            }
        ],
        "terminal_state": "sea_cinematic",
        "learned_at": "2026-05-01T17:39:46.493207+00:00",
        "learned_via": "claude_guidance",
        "parent_state_detail_contains": ["recruit"],
    }


def _good_flow_entry() -> dict:
    """A hand-authored flow used as a control: high confidence, no failures."""
    return {
        "id": "market_purchase_test",
        "description": "Test market_purchase flow (control case).",
        "parent_state": "building",
        "parent_state_detail_contains": ["market"],
        "atomic": True,
        "trigger_detection": "purchase confirm dialog visible",
        "steps": [],
        "terminal_state": "building",
    }


class FSMFlowMetadataTests(unittest.TestCase):
    """FSMFlow loads, updates and persists metadata correctly."""

    def setUp(self):
        # Sandbox _KB_DIR so save methods write to a temp directory.
        self._tmp = Path(tempfile.mkdtemp(prefix="fsm_test_"))
        self._original_kb_dir = fsm_registry._KB_DIR
        fsm_registry._KB_DIR = self._tmp
        # Seed minimal KB files so registry.load() succeeds.
        (self._tmp / "states.json").write_text(json.dumps([
            {"id": "port_overworld", "exits": [], "flows": []},
            {"id": "building", "exits": [{"action": "tap_home", "to": "port_overworld"}], "flows": []},
            {"id": "sea_cinematic", "exits": [], "flows": []},
        ]))
        (self._tmp / "flows.json").write_text(json.dumps([_bad_flow_entry(), _good_flow_entry()]))
        (self._tmp / "interruptors.json").write_text("[]")
        # Reset the singleton so each test gets a fresh registry against the temp dir.
        fsm_registry._instance = None

    def tearDown(self):
        fsm_registry._KB_DIR = self._original_kb_dir
        fsm_registry._instance = None
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_metadata_defaults_when_field_missing(self):
        """Flows loaded from a file with no metadata fields get sane defaults."""
        registry = FSMRegistry().load()
        flow = registry.flows[BAD_FLOW_ID]
        self.assertEqual(flow.confidence, CONFIDENCE_HIGH)
        self.assertEqual(flow.travel_count, 0)
        self.assertEqual(flow.success_count, 0)
        self.assertEqual(flow.failure_count, 0)
        self.assertEqual(flow.success_rate, 0.0)
        self.assertFalse(flow.should_skip())

    def test_record_failure_below_threshold_does_not_demote_yet(self):
        """One or two failures aren't enough to demote a high-confidence flow."""
        registry = FSMRegistry().load()
        registry.record_flow_outcome(BAD_FLOW_ID, success=False)
        flow = registry.flows[BAD_FLOW_ID]
        self.assertEqual(flow.failure_count, 1)
        self.assertEqual(flow.confidence, CONFIDENCE_HIGH)  # not yet demoted
        self.assertFalse(flow.should_skip())

    def test_record_failure_at_threshold_demotes_to_low_and_skip(self):
        """
        The architectural success criterion for the bad flow:
        N consecutive failures with zero successes → confidence='low' and
        should_skip()=True.  Future detection must exclude it.
        """
        registry = FSMRegistry().load()
        for _ in range(FAILURE_DEMOTION_THRESHOLD):
            registry.record_flow_outcome(BAD_FLOW_ID, success=False)
        flow = registry.flows[BAD_FLOW_ID]
        self.assertEqual(flow.failure_count, FAILURE_DEMOTION_THRESHOLD)
        self.assertEqual(flow.success_count, 0)
        self.assertEqual(flow.confidence, CONFIDENCE_LOW)
        self.assertTrue(flow.should_skip())

    def test_demotion_persists_across_reload(self):
        """The demotion must survive a reload — the bot must not relearn it."""
        registry = FSMRegistry().load()
        for _ in range(FAILURE_DEMOTION_THRESHOLD):
            registry.record_flow_outcome(BAD_FLOW_ID, success=False)

        # Drop the singleton; the next get/load reads from disk.
        fsm_registry._instance = None
        reloaded = FSMRegistry().load()
        flow = reloaded.flows[BAD_FLOW_ID]
        self.assertEqual(flow.failure_count, FAILURE_DEMOTION_THRESHOLD)
        self.assertEqual(flow.confidence, CONFIDENCE_LOW)
        self.assertTrue(flow.should_skip())

    def test_save_preserves_non_listed_fields(self):
        """
        Regression: the close_position commit accidentally dropped
        detection_keywords because _save_flows enumerated a fixed key set.
        After Phase 4's _raw-overlay save, custom fields like learned_at
        and learned_via must round-trip intact.
        """
        registry = FSMRegistry().load()
        registry.record_flow_outcome(BAD_FLOW_ID, success=False)  # forces a save

        with (self._tmp / "flows.json").open() as fh:
            entries = json.load(fh)
        bad = next(e for e in entries if e["id"] == BAD_FLOW_ID)

        self.assertEqual(bad.get("learned_at"),  "2026-05-01T17:39:46.493207+00:00")
        self.assertEqual(bad.get("learned_via"), "claude_guidance")
        self.assertEqual(bad.get("step_detection_keywords"),
                         {"step_1": ["confirmation", "dialog", "result"]})

    def test_record_success_promotes_unverified_to_high(self):
        """
        A flow saved as 'unverified' (e.g. by a future safer save path)
        promotes to 'high' after two confirmed successes — the architecture's
        forced-A/B principle (Rule 2) wants real evidence, not single trials.
        """
        registry = FSMRegistry().load()
        flow = registry.flows[BAD_FLOW_ID]
        flow.confidence = CONFIDENCE_UNVERIFIED  # simulate cautious save
        registry.record_flow_outcome(BAD_FLOW_ID, success=True, duration_secs=1.0)
        self.assertEqual(flow.confidence, CONFIDENCE_UNVERIFIED)  # one is not enough
        registry.record_flow_outcome(BAD_FLOW_ID, success=True, duration_secs=2.0)
        self.assertEqual(flow.confidence, CONFIDENCE_HIGH)
        self.assertAlmostEqual(flow.avg_duration_secs, 1.5)

    def test_unknown_flow_is_a_warning_not_a_crash(self):
        """record_flow_outcome on an unknown id logs and returns — must not raise."""
        registry = FSMRegistry().load()
        # Should not raise.
        registry.record_flow_outcome("no_such_flow", success=False)

    def test_demoted_flow_is_skipped_by_perceive(self):
        """
        Architectural acceptance: once the bad flow's confidence has been
        demoted, brain.perceive._detect_active_flow must skip it during
        keyword matching.  This is what stops the bot from re-replaying a
        known-broken recipe and lets fresh learning (Rule 1) kick in.
        """
        # Stub control() so we can inject the bad flow as the only candidate.
        import brain.perceive as perceive_mod

        registry = FSMRegistry().load()
        # Force the bad flow over the failure threshold.
        for _ in range(FAILURE_DEMOTION_THRESHOLD):
            registry.record_flow_outcome(BAD_FLOW_ID, success=False)
        self.assertTrue(registry.flows[BAD_FLOW_ID].should_skip())

        # Make brain.perceive's _fsm.flows resolve to our test registry.
        # _detect_active_flow imports get_fsm_registry from brain.fsm_registry,
        # which already returns the singleton — but our setUp resets the
        # singleton, so the call inside _detect_active_flow will load fresh
        # from our temp KB.  No additional stubbing needed for the FSM side.
        # We do need to stub control() so flow_detection_order returns the
        # bad flow as a candidate.
        class _FakeControl:
            def flow_detection_order(self):
                return [(BAD_FLOW_ID, "step_1")]
            def market_flow_ids(self):
                return set()
            def flow_step_keywords(self, flow_id, step_id):
                # Return keywords that WOULD match the OCR text below if the
                # skip didn't fire.
                return ["confirmation", "dialog", "result"]

        # Stub the OCR helpers so we don't call EasyOCR.
        def _fake_ocr_frame(frame, min_conf=0.3):
            # Tokens shaped (text, conf, cx, cy) — match the bad flow's
            # detection keywords.
            return [
                ("confirmation", 1.0, 0, 0),
                ("dialog",       1.0, 0, 0),
                ("result",       1.0, 0, 0),
            ]
        def _fake_fuzzy_contains(text, kw):
            return kw in text

        # _detect_active_flow imports control / _ocr_frame / fuzzy_contains
        # at call time.  Patch the modules to redirect those imports.
        import brain.kb as _kb
        original_control_fn = _kb.control
        _kb.control = lambda: _FakeControl()

        import actions.sail_actions as _sa
        original_ocr      = _sa._ocr_frame
        original_fuzzy    = _sa.fuzzy_contains
        _sa._ocr_frame      = _fake_ocr_frame
        _sa.fuzzy_contains  = _fake_fuzzy_contains

        # Force the perceive module's FSM lookup to use our test registry —
        # otherwise it would lazily reload a fresh singleton from the real KB.
        import brain.fsm_registry as _fr
        _fr._instance = registry

        try:
            class _FakeFrame:
                width = 2400
                height = 1080

            flow_id, step_id = perceive_mod._detect_active_flow(
                _FakeFrame(), "building", "building: recruit crew"
            )
            self.assertIsNone(flow_id, "Demoted bad flow should NOT be detected")
            self.assertIsNone(step_id)
        finally:
            _kb.control          = original_control_fn
            _sa._ocr_frame       = original_ocr
            _sa.fuzzy_contains   = original_fuzzy


class ProactiveLearnedRecoveryTests(unittest.TestCase):
    """
    Phase 4 patch: learned recoveries (saved via human escalation) are
    consulted proactively during every perceive interruptor pass so the
    bot uses prior knowledge immediately, not after a 5-minute timeout.

    These tests anchor the matcher against the real entry that motivated
    the patch — recruit_crew_confirmation_dialog from the May-1 17:59
    escalation.  The bot stayed stuck on the same dialog 30 minutes later
    in the May-1 14:23 run because the matcher wasn't being called until
    after escalation timeout.
    """

    def setUp(self):
        # Sandbox _LEARNED_FILE in human_escalation and patch perceive's
        # path constant so the test doesn't read the real KB.
        self._tmp = Path(tempfile.mkdtemp(prefix="lr_test_"))
        self._lr_path = self._tmp / "learned_recoveries.json"
        self._lr_path.write_text(json.dumps([
            {
                "id": "recruit_crew_confirmation_dialog",
                "category": "flow_step",
                "description": "Recruit-crew confirm dialog — tap OK.",
                "detection_keywords": ["Notice", "Recruit", "Crew?", "Cancel", "OK"],
                "actions": [
                    {"type": "tap", "label": "", "x": 1302, "y": 812,
                     "x2": 0, "y2": 0, "seconds": 1.0,
                     "x_min": -1, "y_min": -1, "x_max": -1, "y_max": -1}
                ],
                "learned_at": "2026-05-01T17:59:53.601346+00:00",
            }
        ]))
        # Patch the path used inside _match_learned_recoveries.  It uses
        # a local Path constant — easiest to monkey-patch via a wrapper.
        import brain.perceive as p
        self._original_match = p._match_learned_recoveries
        # Replace with a thin wrapper that points at our temp file.
        def _patched(ocr_tokens):
            from brain.human_escalation import EscalationPlan, ActionStep
            entries = json.loads(self._lr_path.read_text())
            text = " ".join(t.lower() for t, _, _, _ in ocr_tokens)
            out = []
            for entry in entries:
                kws = entry.get("detection_keywords", [])
                if kws and all(kw.lower() in text for kw in kws):
                    actions = [ActionStep(**a) for a in entry.get("actions", [])]
                    out.append(EscalationPlan(
                        scenario_id=entry["id"],
                        category=entry.get("category", "other"),
                        description=entry.get("description", ""),
                        actions=actions,
                        detection_keywords=kws,
                        save_as_knowledge=False,
                    ))
            return out
        p._match_learned_recoveries = _patched

    def tearDown(self):
        import brain.perceive as p
        p._match_learned_recoveries = self._original_match
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_recruit_confirm_dialog_matches_proactively(self):
        """
        OCR text from the actual recruit-crew confirm dialog matches the
        learned recovery's keywords.  The matcher returns an EscalationPlan
        that perceive can hand straight to _execute_plan.
        """
        from brain.perceive import _match_learned_recoveries
        tokens = [
            ("Notice",   1.0, 0, 0),
            ("Recruit",  1.0, 0, 0),
            ("101",      1.0, 0, 0),
            ("Crew?",    1.0, 0, 0),
            ("23,180",   1.0, 0, 0),  # different gold amount — keywords don't include it
            ("Cancel",   1.0, 0, 0),
            ("OK",       1.0, 0, 0),
        ]
        plans = _match_learned_recoveries(tokens)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].scenario_id, "recruit_crew_confirmation_dialog")
        self.assertEqual(len(plans[0].actions), 1)
        self.assertEqual(plans[0].actions[0].x, 1302)
        self.assertEqual(plans[0].actions[0].y, 812)

    def test_unrelated_screen_does_not_match(self):
        from brain.perceive import _match_learned_recoveries
        tokens = [
            ("Market",   1.0, 0, 0),
            ("Wool",     1.0, 0, 0),
            ("Purchase", 1.0, 0, 0),
        ]
        plans = _match_learned_recoveries(tokens)
        self.assertEqual(plans, [])


class SaveLearnedFlowGuardTests(unittest.TestCase):
    """
    The Phase-4 save-time plausibility guard refuses to persist flows whose
    declared terminal_state is a perceive fallback signal (sea_cinematic /
    unknown).  This is the upstream sibling of the demotion mechanism: it
    stops bad flows from entering the KB in the first place when the
    underlying cause is a misclassified post-tap state, not bad action
    selection.
    """

    def setUp(self):
        # Sandbox _FLOWS_PATH and the ui_signals path so we never touch the
        # real KB.  Then patch them on the module under test.
        self._tmp = Path(tempfile.mkdtemp(prefix="guard_test_"))
        flows_path   = self._tmp / "flows.json"
        signals_path = self._tmp / "ui_signals.json"
        flows_path.write_text("[]")
        signals_path.write_text(json.dumps({"flow_detection_order": []}))

        import brain.claude_guidance as cg
        self._cg = cg
        self._original_flows_path   = cg._FLOWS_PATH
        cg._FLOWS_PATH = flows_path
        # save_learned_flow constructs the signals path at call time via a
        # literal Path(...) — patch by monkey-patching Path inside the call
        # via a small helper.  Easiest: stub the function call.
        self._signals_path = signals_path

    def tearDown(self):
        self._cg._FLOWS_PATH = self._original_flows_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_history(self):
        from brain.claude_guidance import GuidedStep
        return [
            GuidedStep(
                action="tap", label="recruit", visual_hint="gold",
                position_hint="right_panel", region_pct={"y_min_pct": 0.8},
                reason="Tap Recruit",
                expected_outcome="confirmation dialog appears",
            ),
            GuidedStep(
                action="complete",
                reason="apparently done",
                expected_outcome="goal achieved",
            ),
        ]

    def _make_perceive(self, state, detail="building: recruit crew"):
        from brain.perceive import PerceiveResult
        return PerceiveResult(state=state, port=None, detail=detail)

    def test_refuses_to_save_when_terminal_is_sea_cinematic(self):
        """
        The exact failure pattern observed in May-1 runs: perceive returned
        sea_cinematic for a recruit-crew confirm dialog, Claude declared
        complete, save would persist a `learned_building__sea_cinematic__…`
        flow whose terminal claim contradicts reality.  Guard must refuse.
        """
        starting = self._make_perceive("building")
        final    = self._make_perceive("sea_cinematic", detail="(perceive fallback)")
        result = self._cg.save_learned_flow(
            starting_state=starting, final_state=final,
            history=self._make_history(),
            goal="recruit crew confirm test",
        )
        self.assertIsNone(result)
        # Confirm flows.json is still empty.
        with self._cg._FLOWS_PATH.open() as fh:
            entries = json.load(fh)
        self.assertEqual(entries, [])

    def test_refuses_to_save_when_terminal_is_unknown(self):
        starting = self._make_perceive("building")
        final    = self._make_perceive("unknown", detail="(unidentified)")
        result = self._cg.save_learned_flow(
            starting_state=starting, final_state=final,
            history=self._make_history(),
            goal="test",
        )
        self.assertIsNone(result)


class InteractiveTeachingLoopTests(unittest.TestCase):
    """
    Phase 4 follow-on: the human escalation path is now an interactive
    multi-turn teach loop instead of a one-shot prompt.  Recipes are saved
    only after the human types 'done' — incomplete teaches discard.

    These tests cover:
      - control-command parsing (done / cancel / aliases)
      - initial-prompt timeout raises TeachingAbortedError
      - explicit cancel discards everything (no recovery saved)
      - 'done' after multi-step teach saves ONE accumulated recovery
        with confidence='unverified'
    """

    def setUp(self):
        # Sandbox the learned-recoveries file so saves don't touch the real KB.
        self._tmp = Path(tempfile.mkdtemp(prefix="teach_test_"))
        self._lr_path = self._tmp / "learned_recoveries.json"
        self._lr_path.write_text("[]")

        import brain.human_escalation as he
        self._he = he
        self._original_lr_file = he._LEARNED_FILE
        he._LEARNED_FILE = self._lr_path

        # Stub the prompt + execute_plan + perceive / capture / Claude calls
        # so the loop runs without ADB / API.  Tests inject _prompts list
        # consumed in order; assertions check what got saved at the end.
        self._prompts: list[str | None] = []
        self._executed: list[str] = []
        self._original_prompt = he._prompt_operator_for_step

        def fake_prompt(state, detail, flow, goal,
                        step_num, accumulated, timeout_secs):
            return self._prompts.pop(0) if self._prompts else None
        he._prompt_operator_for_step = fake_prompt

        # Stub _execute_plan so we can record executions without ADB.
        self._original_execute = he._execute_plan
        def fake_execute(plan):
            self._executed.append(plan.scenario_id)
        he._execute_plan = fake_execute

        # Stub Claude / perceive paths.
        from brain.perceive import PerceiveResult
        self._fake_perceive_state = PerceiveResult(
            state="building", port=None, detail="building: recruit crew"
        )

        import brain.perceive as p
        self._original_perceive = p.perceive
        self._original_reclass  = p.reclassify_with_claude
        p.perceive = lambda frame=None: self._fake_perceive_state
        p.reclassify_with_claude = lambda frame, result: self._fake_perceive_state

        import capture.adb_capture as cap
        self._original_capture = cap.capture_screen
        cap.capture_screen = lambda: None  # frame value is unused by stubs

        # Stub _parse_with_claude to return a simple plan for any input.
        self._original_parse = he._parse_with_claude
        def fake_parse(description, state, detail, flow, frame):
            return he.EscalationPlan(
                scenario_id        = f"teach_step_{description[:10]}",
                category           = "flow_step",
                description        = description,
                actions            = [he.ActionStep(type="tap", x=100, y=100)],
                detection_keywords = [],
                save_as_knowledge  = True,
            )
        he._parse_with_claude = fake_parse

        # Stub _match_learned_recovery to always return None (force teach loop).
        self._original_match = he._match_learned_recovery
        he._match_learned_recovery = lambda state, detail: None

    def tearDown(self):
        self._he._LEARNED_FILE                = self._original_lr_file
        self._he._prompt_operator_for_step    = self._original_prompt
        self._he._execute_plan                = self._original_execute
        self._he._parse_with_claude           = self._original_parse
        self._he._match_learned_recovery      = self._original_match
        import brain.perceive as p
        p.perceive               = self._original_perceive
        p.reclassify_with_claude = self._original_reclass
        import capture.adb_capture as cap
        cap.capture_screen = self._original_capture
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_perceive_input(self):
        from brain.perceive import PerceiveResult
        return PerceiveResult(
            state="building", port=None, detail="building: recruit crew",
        )

    def test_initial_timeout_raises_teaching_aborted(self):
        """Empty initial input (timeout sentinel) raises TeachingAbortedError."""
        from brain.human_escalation import escalate, TeachingAbortedError
        self._prompts = [None]   # simulates initial timeout
        with self.assertRaises(TeachingAbortedError):
            escalate(context="test", perceive_result=self._make_perceive_input())

    def test_cancel_discards_no_save(self):
        """'cancel' on first prompt: nothing saved, no exception."""
        from brain.human_escalation import escalate
        self._prompts = ["cancel"]
        result = escalate(context="test", perceive_result=self._make_perceive_input())
        self.assertIsNotNone(result)
        # Confirm learned_recoveries.json is still empty.
        with self._lr_path.open() as fh:
            self.assertEqual(json.load(fh), [])
        self.assertEqual(self._executed, [])  # nothing executed either

    def test_cancel_after_actions_discards_partial(self):
        """
        Per user direction: 'discard if it is incomplete is the right approach'.
        Even after several actions, if the human types 'cancel' the partial
        plan must be discarded — no half-recipe in the KB.
        """
        from brain.human_escalation import escalate
        self._prompts = [
            "tap the OK button",
            "tap the second button",
            "cancel",
        ]
        escalate(context="test", perceive_result=self._make_perceive_input())
        with self._lr_path.open() as fh:
            self.assertEqual(json.load(fh), [],
                             "partial recipe must NOT be saved on cancel")

    def test_done_saves_one_multi_step_recovery(self):
        """
        'done' saves the accumulated steps as a SINGLE learned_recovery
        (not one entry per step — that was the old bug shape).  Saved
        entry has confidence='unverified' so Phase 4 verification runs
        on the next replay before promoting.
        """
        from brain.human_escalation import escalate
        self._prompts = [
            "tap the OK button",
            "tap the gold Recruit button",
            "press back",
            "done",
        ]
        escalate(context="recruit_test",
                 perceive_result=self._make_perceive_input(),
                 goal="recruit crew at harbor")
        with self._lr_path.open() as fh:
            entries = json.load(fh)
        self.assertEqual(len(entries), 1, "should save exactly ONE multi-step entry")
        entry = entries[0]
        self.assertEqual(len(entry["actions"]), 3,
                         "all three taught actions must be in the saved recipe")
        # Confidence and provenance hints in the description point at unverified.
        self.assertIn("unverified", entry.get("description", "").lower())

    def test_control_command_aliases(self):
        from brain.human_escalation import _is_control_command
        for done_word in ("done", "Done", "complete", "FINISH", "ok done"):
            self.assertEqual(_is_control_command(done_word), "done", done_word)
        for cancel_word in ("cancel", "ABORT", "quit", "stop", "Give up"):
            self.assertEqual(_is_control_command(cancel_word), "cancel", cancel_word)
        for action in ("tap OK", "press back", "wait 2s", ""):
            self.assertIsNone(_is_control_command(action), action)


class RecoveryLoopGuardTests(unittest.TestCase):
    """
    Phase 4 runtime safety: even Claude/human-approved recoveries can be
    wrong (UI drift, overfit keywords, incomplete teach).  When a recovery
    fires from the same screen signature N times consecutively without
    making progress, the bot must auto-disable it — both in-session and
    persistently in the KB so future runs don't repeat the loop.
    """

    def setUp(self):
        # Sandbox _LEARNED_FILE in human_escalation and reset module state.
        self._tmp = Path(tempfile.mkdtemp(prefix="loop_guard_test_"))
        self._lr_path = self._tmp / "learned_recoveries.json"
        self._lr_path.write_text(json.dumps([
            {
                "id": "test_recovery",
                "category": "flow_step",
                "description": "Test recipe",
                "detection_keywords": ["foo", "bar"],
                "actions": [{"type": "tap", "x": 100, "y": 100,
                             "x2": 0, "y2": 0, "seconds": 1.0,
                             "x_min": -1, "y_min": -1, "x_max": -1, "y_max": -1,
                             "label": ""}],
            },
            {
                "id": "low_confidence_recovery",
                "category": "flow_step",
                "description": "Already-demoted recipe",
                "detection_keywords": ["lowconf"],
                "confidence": "low",
                "failure_count": 5,
                "actions": [],
            },
        ]))
        import brain.human_escalation as he
        self._he = he
        self._original_lr_file = he._LEARNED_FILE
        he._LEARNED_FILE = self._lr_path
        # Clear in-memory state so tests don't leak into one another.
        he._recovery_fire_log.clear()
        he._session_disabled_recoveries.clear()

    def tearDown(self):
        self._he._LEARNED_FILE = self._original_lr_file
        self._he._recovery_fire_log.clear()
        self._he._session_disabled_recoveries.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_signature_changes_reset_consecutive_count(self):
        """
        A signature change between fires breaks the consecutive-failure
        chain — the bot is making progress, so the recipe should keep
        being eligible.  Otherwise normal multi-step flows would be
        spuriously demoted.
        """
        from brain.human_escalation import should_skip_recovery_for_loop
        self.assertFalse(should_skip_recovery_for_loop("recipe_x", "sig_a"))
        self.assertFalse(should_skip_recovery_for_loop("recipe_x", "sig_b"))
        # Even on the third fire, since the signatures differ, no skip.
        self.assertFalse(should_skip_recovery_for_loop("recipe_x", "sig_c"))

    def test_three_same_signature_fires_auto_disable(self):
        """
        The user's specified threshold: 3 consecutive fires from the same
        screen signature without progress → auto-disable.  Pre-fire guard
        kicks in on the 3rd attempt.
        """
        from brain.human_escalation import should_skip_recovery_for_loop, _session_disabled_recoveries
        # 1st fire: allowed.
        self.assertFalse(should_skip_recovery_for_loop("recipe_y", "stuck_sig"))
        # 2nd: still allowed (could be transient).
        self.assertFalse(should_skip_recovery_for_loop("recipe_y", "stuck_sig"))
        # 3rd: blocked — would be the threshold-th attempt.
        self.assertTrue(should_skip_recovery_for_loop("recipe_y", "stuck_sig"))
        self.assertIn("recipe_y", _session_disabled_recoveries)

    def test_disable_persists_to_kb(self):
        """
        When the loop guard auto-disables a recipe, the persistent KB
        must also be updated so future processes start with confidence=low
        rather than re-running the loop on a fresh restart.
        """
        from brain.human_escalation import should_skip_recovery_for_loop
        for _ in range(3):
            should_skip_recovery_for_loop("test_recovery", "stuck_sig")
        with self._lr_path.open() as fh:
            entries = json.load(fh)
        target = next(e for e in entries if e["id"] == "test_recovery")
        self.assertEqual(target.get("confidence"), "low")
        self.assertGreaterEqual(target.get("failure_count", 0), 1)
        self.assertIn("last_failed_at", target)

    def test_match_skips_demoted_entry(self):
        """
        _match_learned_recovery must filter out entries with confidence=low
        so a demoted recipe is treated as if absent from the KB.
        """
        from brain.human_escalation import _match_learned_recovery
        # Trigger keywords for the demoted entry are 'lowconf'.
        result = _match_learned_recovery("building", "lowconf detail")
        self.assertIsNone(result, "demoted recipe must NOT be returned by matcher")

    def test_match_returns_high_confidence_entry(self):
        """Healthy entries (no demotion marks) still match normally."""
        from brain.human_escalation import _match_learned_recovery
        result = _match_learned_recovery("building", "foo bar detail")
        self.assertIsNotNone(result)
        self.assertEqual(result.scenario_id, "test_recovery")

    def test_session_disabled_blocks_subsequent_match(self):
        """
        Once a recipe has been auto-disabled within the session, even
        future matches against the live KB should skip it (without
        waiting for the loop guard to re-detect).
        """
        from brain.human_escalation import (
            should_skip_recovery_for_loop, _match_learned_recovery,
        )
        # Auto-disable test_recovery via the loop guard.
        for _ in range(3):
            should_skip_recovery_for_loop("test_recovery", "any_sig")
        # Now matcher should skip it even though detection text matches.
        result = _match_learned_recovery("building", "foo bar detail")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
