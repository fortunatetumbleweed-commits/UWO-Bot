"""
Phase 5a tests — perception-layer hardening.

Three changes in brain/perceive.py:

  1. Diagnostic logging in _classify_nav_state (every branch logs at INFO
     so unknown verdicts are diagnosable from the log alone).
  2. Replace "vision check" wording in the fallback detail with neutral
     text that doesn't feed Qwen a phrase to paraphrase back.
  3. Filter phantom interruptor saves: entries whose description
     contains perceive-context phrases AND have no detection_keywords
     are refused at save time.

Coverage:
  - _looks_like_phantom_paraphrase identifies the feedback-loop class
  - real interruptors with concrete keywords are NOT classified as phantom
  - real interruptors without the perceive phrases are NOT phantom
  - _save_new_interruptor refuses phantoms; allows real ones
  - Fallback detail string contains the new neutral wording
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import brain.perceive as perceive_mod


class PhantomDetectionTests(unittest.TestCase):
    """The heuristic for identifying Qwen paraphrase confabulations."""

    def test_phantom_classic_case_caught(self):
        """The exact pattern from May-2 13:04 — phantom indicator + no kws."""
        entry = {
            "id": "a_vision_check_overlay_message_indicatin",
            "description": "A vision check overlay message indicating the location is NOT at sea, likely a modal dialog.",
            "detection_keywords": [],
        }
        self.assertTrue(perceive_mod._looks_like_phantom_paraphrase(entry))

    def test_phantom_with_invented_keywords_is_still_phantom(self):
        """
        Qwen sometimes invents plausible-looking keyword lists by mixing
        prompt phrases with OCR tokens (e.g. 'Plymouth' + 'NOT at sea').
        The mixed entries fire on legitimate Plymouth port_overworld
        screens, so the description's phantom phrase is the discriminator,
        not the keyword presence.
        """
        entry = {
            "id": "phantom_with_invented_keywords",
            "description": "A vision check overlay message on the game's main screen.",
            "detection_keywords": ["Plymouth", "Fortune Teller"],   # OCR tokens, but desc is phantom
        }
        self.assertTrue(perceive_mod._looks_like_phantom_paraphrase(entry))

    def test_real_interruptor_without_phantom_phrases_is_kept(self):
        """A normal recruit-confirm interruptor has no phantom-context words."""
        entry = {
            "id": "recruit_crew_confirmation_dialog",
            "description": "Recruit Crew confirmation dialog asking to recruit 110 crew for 23,760 gold",
            "detection_keywords": ["Notice", "Recruit", "Crew?", "OK"],
        }
        self.assertFalse(perceive_mod._looks_like_phantom_paraphrase(entry))

    def test_real_interruptor_without_keywords_but_no_phantom_phrases(self):
        """No phantom phrases AND no keywords — not phantom, just incomplete.
        We don't refuse the save here; absence of keywords alone isn't enough
        evidence of confabulation."""
        entry = {
            "id": "some_dialog",
            "description": "Confirmation popup with two buttons",
            "detection_keywords": [],
        }
        self.assertFalse(perceive_mod._looks_like_phantom_paraphrase(entry))

    def test_each_phantom_phrase_individually_recognised(self):
        for phrase in perceive_mod._PHANTOM_PHRASES:
            entry = {
                "id": "test",
                "description": f"Some text including {phrase} in it",
                "detection_keywords": [],
            }
            self.assertTrue(
                perceive_mod._looks_like_phantom_paraphrase(entry),
                f"phrase {phrase!r} should be recognised as phantom-indicator",
            )


class SaveRefusalTests(unittest.TestCase):
    """_save_new_interruptor refuses phantom saves; persists real ones."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="phase5a_"))
        self._kb_file = self._tmp / "interruptors.json"
        self._kb_file.write_text("[]")
        # Redirect the module-level path constant so saves go to the temp file.
        self._original_path = perceive_mod._INTERRUPTORS_PATH
        perceive_mod._INTERRUPTORS_PATH = self._kb_file

    def tearDown(self):
        perceive_mod._INTERRUPTORS_PATH = self._original_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_phantom_save_refused(self):
        phantom = {
            "id": "a_vision_check_overlay_phantom",
            "description": "A vision check overlay message indicating the player is not at sea.",
            "detection_keywords": [],
        }
        perceive_mod._save_new_interruptor(phantom, source="qwen")
        self.assertEqual(json.loads(self._kb_file.read_text()), [])

    def test_real_save_persists(self):
        with patch("brain.kb.reload") as mock_reload:
            real = {
                "id": "real_modal_dialog",
                "description": "Confirmation popup for crew recruitment",
                "detection_keywords": ["Notice", "Recruit", "OK"],
            }
            perceive_mod._save_new_interruptor(real, source="claude")
            entries = json.loads(self._kb_file.read_text())
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["id"], "real_modal_dialog")
            mock_reload.assert_called_once()

    def test_duplicate_id_not_added_twice(self):
        with patch("brain.kb.reload"):
            self._kb_file.write_text(json.dumps([{
                "id": "existing", "description": "...",
                "detection_keywords": ["foo"],
            }]))
            perceive_mod._save_new_interruptor({
                "id": "existing",
                "description": "Different description",
                "detection_keywords": ["bar"],
            }, source="qwen")
            entries = json.loads(self._kb_file.read_text())
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["detection_keywords"], ["foo"])

    def test_phantom_phrase_alone_refuses_even_with_keywords(self):
        """Stricter heuristic: phantom phrase in desc → refuse regardless of keywords."""
        with patch("brain.kb.reload"):
            entry = {
                "id": "phantom_with_invented_kws",
                "description": "A vision check overlay informing about something",
                "detection_keywords": ["Plymouth", "Fortune Teller"],
            }
            perceive_mod._save_new_interruptor(entry, source="qwen")
            entries = json.loads(self._kb_file.read_text())
            self.assertEqual(entries, [], "phantom phrase alone must trigger refusal")


class FallbackDetailWordingTests(unittest.TestCase):
    """The classifier's unknown-fallback detail must NOT contain feedback-loop phrases."""

    def test_fallback_detail_neutral_wording(self):
        """
        Strip docstrings + comments and confirm the actual return-statement
        bodies don't contain the old phrasing — only the docstring/comment
        retains it as historical context for what was changed.
        """
        import inspect
        import re
        # The classify cascade was split into a thin family-gate wrapper
        # (_classify_nav_state) + the detail cascade (_classify_nav_state_inner);
        # the fallback wording lives in the inner. Inspect both halves.
        source = inspect.getsource(perceive_mod._classify_nav_state)
        source += inspect.getsource(perceive_mod._classify_nav_state_inner)

        # Strip triple-quoted docstrings and # comments so we only check the
        # executable code, not historical-context comments.
        src_no_docstring = re.sub(r'"""[\s\S]*?"""', "", source)
        src_code_only = "\n".join(
            line for line in src_no_docstring.splitlines()
            if not line.lstrip().startswith("#")
        )

        # Old phrase removed from active code (still allowed in docstring)
        self.assertNotIn(
            '"vision check says NOT at sea"',
            src_code_only,
            "Old detail wording still emitted by code path — would feed "
            "Qwen a phrase to paraphrase back",
        )
        # New neutral wording present in active code
        self.assertIn("unrecognised layout", src_code_only)
        self.assertIn("classifier could not determine state", src_code_only)


class DebounceTests(unittest.TestCase):
    """
    handle_unknown_blocking debounces transient overlays.  N consecutive
    same-signature perceives must accumulate before Qwen/Claude fires.
    """

    def setUp(self):
        # Reset module-level debounce state before each test.
        perceive_mod.reset_unknown_debounce()

    def test_signature_stable_across_token_order(self):
        sig1 = perceive_mod._unknown_signature(
            [("alpha", 1, 0, 0), ("beta", 1, 0, 0), ("gamma", 1, 0, 0)]
        )
        sig2 = perceive_mod._unknown_signature(
            [("gamma", 1, 0, 0), ("alpha", 1, 0, 0), ("beta", 1, 0, 0)]
        )
        self.assertEqual(sig1, sig2)

    def test_signature_changes_with_different_content(self):
        sig1 = perceive_mod._unknown_signature(
            [("plymouth", 1, 0, 0), ("harbor", 1, 0, 0)]
        )
        sig2 = perceive_mod._unknown_signature(
            [("london", 1, 0, 0), ("market", 1, 0, 0)]
        )
        self.assertNotEqual(sig1, sig2)

    def test_first_two_unknown_frames_dont_fire(self):
        """Transient overlays (NPC bubble, name plate) clear before threshold."""
        same_tokens = [("plymouth", 1, 0, 0), ("harbor", 1, 0, 0)]
        for i in range(2):
            should_fire, consecutive = perceive_mod._unknown_debounce_should_fire(
                same_tokens,
            )
            self.assertFalse(should_fire,
                              f"unknown frame {i + 1} should not fire (need 3)")

    def test_third_consecutive_unknown_fires(self):
        same_tokens = [("plymouth", 1, 0, 0), ("harbor", 1, 0, 0)]
        for _ in range(3):
            should_fire, consecutive = perceive_mod._unknown_debounce_should_fire(
                same_tokens,
            )
        self.assertTrue(should_fire)
        self.assertGreaterEqual(consecutive, 3)

    def test_signature_change_resets_consecutive_count(self):
        first = [("plymouth", 1, 0, 0), ("harbor", 1, 0, 0)]
        second = [("london", 1, 0, 0), ("market", 1, 0, 0)]
        # Two with first sig
        for _ in range(2):
            perceive_mod._unknown_debounce_should_fire(first)
        # Now content changes
        should_fire, consecutive = perceive_mod._unknown_debounce_should_fire(second)
        self.assertFalse(should_fire)
        self.assertEqual(consecutive, 1)

    def test_handle_unknown_blocking_returns_false_during_debounce(self):
        """
        First two calls to handle_unknown_blocking with same OCR should not
        invoke Qwen (the gate trips first).  We confirm by patching the
        Qwen helper to assert it's NOT called.
        """
        with patch("brain.perceive._qwen_describe_unknown") as mock_qwen, \
             patch("brain.perceive._claude_resolve_unknown") as mock_claude:
            tokens = [("plymouth", 1, 0, 0), ("harbor", 1, 0, 0)]
            r1 = perceive_mod.handle_unknown_blocking(None, tokens, "")
            r2 = perceive_mod.handle_unknown_blocking(None, tokens, "")
            self.assertFalse(r1)
            self.assertFalse(r2)
            mock_qwen.assert_not_called()
            mock_claude.assert_not_called()


class NewPhantomPhraseTests(unittest.TestCase):
    """The May-2 14:10 phantom — different surface words, same metacognition."""

    def test_layout_not_recognized_phantom(self):
        """The exact phrase from the May-2 14:10 run."""
        entry = {
            "id": "the_screen_shows_a_layout_that_is_not_re",
            "description": "The screen shows a layout that is not recognized, indicating a lack of visual cues for navigation.",
            "detection_keywords": [],
        }
        self.assertTrue(perceive_mod._looks_like_phantom_paraphrase(entry))

    def test_screen_displays_phantom(self):
        entry = {
            "id": "test",
            "description": "The screen displays a layout that obscures navigation",
            "detection_keywords": [],
        }
        self.assertTrue(perceive_mod._looks_like_phantom_paraphrase(entry))

    def test_lack_of_visual_cues_phantom(self):
        entry = {
            "id": "test",
            "description": "Screen has a lack of visual cues making navigation unclear",
            "detection_keywords": ["plymouth"],
        }
        self.assertTrue(perceive_mod._looks_like_phantom_paraphrase(entry))


class ChainedDismissalParserTests(unittest.TestCase):
    """The chained-action grammar Claude/teach-loops emit naturally:
        tap(x,y) | tap_<label> | wait_<n>s | wait_<n>ms | press_back
    joined by '_then_'.

    Anchored on the May-3 fleet_death_recovery_screen entry whose
    dismissal is
      'tap(1800,120)_then_wait_1s_then_tap_port return_then_wait_2s_then_tap_ok'
    which the old dispatcher couldn't read."""

    def _patch_actuators(self):
        """Patches every actuator that _execute_chained_dismissal uses."""
        from contextlib import ExitStack
        from unittest.mock import MagicMock, patch
        stack = ExitStack()
        mocks = {
            "tap":         stack.enter_context(patch("actions.adb_actions.tap")),
            "press_back":  stack.enter_context(patch("actions.adb_actions.press_back")),
            "find_button": stack.enter_context(patch("actions.sail_actions._find_button")),
            "capture":     stack.enter_context(patch("capture.adb_capture.capture_screen")),
            "sleep":       stack.enter_context(patch("time.sleep")),
        }
        mocks["capture"].return_value = MagicMock()
        mocks["find_button"].return_value = None
        return stack, mocks

    def test_fleet_death_chain_executes_each_step(self):
        from unittest.mock import MagicMock
        from brain.perceive import _execute_chained_dismissal

        stack, mocks = self._patch_actuators()
        with stack:
            def _find(frame, label, **kw):
                return {
                    "port return": (1800, 120),
                    "ok":          (1300, 720),
                }.get(label.lower())
            mocks["find_button"].side_effect = _find

            method = (
                "tap(1800,120)_then_wait_1s_then_"
                "tap_port return_then_wait_2s_then_tap_ok"
            )
            _execute_chained_dismissal(method, MagicMock(), {})

            # 1 coord tap + 2 label taps = 3 calls to tap
            self.assertEqual(mocks["tap"].call_count, 3)
            self.assertEqual(mocks["tap"].call_args_list[0][0], (1800, 120))
            self.assertEqual(mocks["tap"].call_args_list[1][0], (1800, 120))
            self.assertEqual(mocks["tap"].call_args_list[2][0], (1300, 720))

    def test_tap_coord_step(self):
        from unittest.mock import MagicMock
        from brain.perceive import _execute_chained_dismissal
        stack, mocks = self._patch_actuators()
        with stack:
            _execute_chained_dismissal("tap(500,300)", MagicMock(), {})
            mocks["tap"].assert_called_once_with(500, 300)

    def test_wait_seconds_step(self):
        from unittest.mock import MagicMock
        from brain.perceive import _execute_chained_dismissal
        stack, mocks = self._patch_actuators()
        with stack:
            _execute_chained_dismissal("wait_2.5s", MagicMock(), {})
            sleep_amounts = [c.args[0] for c in mocks["sleep"].call_args_list]
            self.assertIn(2.5, sleep_amounts)

    def test_wait_milliseconds_step(self):
        from unittest.mock import MagicMock
        from brain.perceive import _execute_chained_dismissal
        stack, mocks = self._patch_actuators()
        with stack:
            _execute_chained_dismissal("wait_500ms", MagicMock(), {})
            sleep_amounts = [c.args[0] for c in mocks["sleep"].call_args_list]
            self.assertIn(0.5, sleep_amounts)

    def test_press_back_step(self):
        from unittest.mock import MagicMock
        from brain.perceive import _execute_chained_dismissal
        stack, mocks = self._patch_actuators()
        with stack:
            _execute_chained_dismissal("press_back", MagicMock(), {})
            mocks["press_back"].assert_called_once()

    def test_tap_label_uses_find_button(self):
        from unittest.mock import MagicMock
        from brain.perceive import _execute_chained_dismissal
        stack, mocks = self._patch_actuators()
        with stack:
            mocks["find_button"].return_value = (2000, 800)
            _execute_chained_dismissal("tap_recruit", MagicMock(), {})
            self.assertEqual(mocks["find_button"].call_args[0][1], "recruit")
            mocks["tap"].assert_called_once_with(2000, 800)

    def test_unknown_step_skipped_chain_continues(self):
        """A typo in one step shouldn't abort the rest of the chain."""
        from unittest.mock import MagicMock
        from brain.perceive import _execute_chained_dismissal
        stack, mocks = self._patch_actuators()
        with stack:
            mocks["find_button"].return_value = (100, 100)
            method = "wait_1s_then_garbage_step_then_tap_ok"
            _execute_chained_dismissal(method, MagicMock(), {})
            mocks["tap"].assert_called_once_with(100, 100)

    def test_label_button_not_found_chain_continues(self):
        from unittest.mock import MagicMock
        from brain.perceive import _execute_chained_dismissal
        stack, mocks = self._patch_actuators()
        with stack:
            mocks["find_button"].return_value = None
            _execute_chained_dismissal(
                "tap_nonexistent_then_tap(50,50)", MagicMock(), {},
            )
            mocks["tap"].assert_called_once_with(50, 50)


class DismissalDispatcherChainAwarenessTests(unittest.TestCase):
    """_dismiss_interruptor routes methods containing '_then_' to the
    chained parser, EXCEPT for 'tap_ok_then_wait_reload' which is a
    known single-method whose name happens to contain '_then_'."""

    def test_tap_ok_then_wait_reload_uses_single_method(self):
        from unittest.mock import patch, MagicMock
        with patch("brain.perceive._execute_chained_dismissal") as mock_chain, \
             patch("brain.perceive._dismiss_android_connection") as mock_android, \
             patch("brain.fsm_registry.get_fsm_registry") as mock_reg:
            interruptor = MagicMock()
            interruptor._raw = {"dismissal": "tap_ok_then_wait_reload"}
            mock_reg.return_value.interruptors = {"x": interruptor}
            from brain.perceive import _dismiss_interruptor
            _dismiss_interruptor("x", MagicMock())
            mock_android.assert_called_once()
            mock_chain.assert_not_called()

    def test_chained_dismissal_routes_to_chain_parser(self):
        from unittest.mock import patch, MagicMock
        with patch("brain.perceive._execute_chained_dismissal") as mock_chain, \
             patch("brain.fsm_registry.get_fsm_registry") as mock_reg:
            interruptor = MagicMock()
            interruptor._raw = {"dismissal": "tap(1800,120)_then_wait_1s_then_tap_ok"}
            mock_reg.return_value.interruptors = {"x": interruptor}
            from brain.perceive import _dismiss_interruptor
            _dismiss_interruptor("x", MagicMock())
            mock_chain.assert_called_once()


if __name__ == "__main__":
    unittest.main()


def test_the_world_map_keeps_its_overlays():
    """A DIALOG ON THE WORLD MAP IS A CONTEXT, NOT AN INTERRUPTION.

    On the sea or at a port a confident `transient` reading outranks an overworld verdict —
    a modal there arrived unasked, and acting as if at the port is the hazard the gate
    exists for. The world map is the other case: City Info, Village Info, the destination
    panel and the Trade Event Schedule ALL open over it, and each is a screen
    WorldMapActivity exists to read. Demoting them to 'unknown' leaves no activity to claim
    the frame, so the dispatcher calls itself lost and re-perceives the same screen forever.

    Live 2026-08-29 with the Trade Event Schedule left open: CNN transient@0.86, cascade
    world_map on three signal groups, and the bot looped a Qwen call per tick.
    """
    import pathlib as _p

    frame_path = _p.Path("data/reference/world_map/event_schedule_dialog.png")
    if not frame_path.exists():
        pytest.skip("reference frame not present")
    from PIL import Image

    from brain.perceive import _classify_nav_state
    from vision.family_classifier import classify_family

    frame = Image.open(frame_path).convert("RGB")
    fam = classify_family(frame)
    assert fam.family == "transient", "the CNN is right — there IS an overlay"
    assert _classify_nav_state(frame).get("location") == "world_map"
