"""
Layer 4a tests — action dispatcher.

See docs/planner_architecture.md and brain/plan_actions.py.

Coverage:
  - Each registered action kind dispatches to the correct handler with
    the right argument shape (mocked underlying helpers).
  - Unknown action kind raises StepExecutionError.
  - Action without 'kind' raises StepExecutionError.
  - tap_button raises when no button found (mocked _find_button → None).
  - Region-pct conversion works for partial / full / empty regions.
  - registered_kinds enumerates all bundled handlers.
"""

import unittest
from unittest.mock import MagicMock, patch

from brain.plan_actions import (
    StepExecutionError, execute_step, register_action, registered_kinds,
    _region_pct_to_kwargs,
)


class RegisteredKindsTests(unittest.TestCase):

    def test_default_kinds_present(self):
        kinds = set(registered_kinds())
        for expected in {"tap", "press_back", "wait", "tap_button",
                          "navigate_to", "exit_to_port_overworld",
                          "tap_primary_action", "retry", "noop"}:
            self.assertIn(expected, kinds)


class UnknownActionTests(unittest.TestCase):

    def test_no_kind_raises(self):
        with self.assertRaises(StepExecutionError):
            execute_step({"x": 1, "y": 2})

    def test_unknown_kind_raises(self):
        with self.assertRaises(StepExecutionError):
            execute_step({"kind": "fly_to_the_moon"})


class RegionConversionTests(unittest.TestCase):

    def test_full_region_converts(self):
        kwargs = _region_pct_to_kwargs(
            {"y_min_pct": 0.1, "y_max_pct": 0.2, "x_min_pct": 0.5, "x_max_pct": 0.7},
            frame_w=2400, frame_h=1080,
        )
        self.assertEqual(kwargs, {
            "y_min": 108, "y_max": 216, "x_min": 1200, "x_max": 1680,
        })

    def test_partial_region_only_emits_set_keys(self):
        kwargs = _region_pct_to_kwargs(
            {"y_min_pct": 0.5}, 2400, 1080,
        )
        self.assertEqual(kwargs, {"y_min": 540})

    def test_none_region_returns_empty(self):
        self.assertEqual(_region_pct_to_kwargs(None, 2400, 1080), {})

    def test_empty_region_returns_empty(self):
        self.assertEqual(_region_pct_to_kwargs({}, 2400, 1080), {})

    def test_rectangle_format_converts_to_explicit_bounds(self):
        """Claude replan generates {x, y, w, h} naturally.  Without
        translation, the keys don't match any *_pct names and the
        spatial constraint silently drops — caused 'Recruit' to match
        the title-bar instead of the gold button on May-2 21:20."""
        kwargs = _region_pct_to_kwargs(
            {"x": 0.82, "y": 0.75, "w": 0.16, "h": 0.10},
            frame_w=2400, frame_h=1080,
        )
        # Note: 0.82 in float is actually 0.81999999..., so
        # int(2400 * 0.82) == 1967, not 1968.  Mirrors the existing
        # int()-based truncation in the explicit-bounds path.
        self.assertEqual(kwargs, {
            "x_min": 1967,
            "x_max": 2352,
            "y_min": 810,
            "y_max": 918,
        })

    def test_explicit_format_still_works_after_rectangle_addition(self):
        """Hand-authored regions in {x_min_pct, ...} format must still
        convert correctly — the rectangle path does not shadow them."""
        kwargs = _region_pct_to_kwargs(
            {"x_min_pct": 0.5, "x_max_pct": 0.7, "y_min_pct": 0.8},
            frame_w=2400, frame_h=1080,
        )
        self.assertEqual(kwargs, {
            "x_min": 1200, "x_max": 1680, "y_min": 864,
        })


class TapHandlerTests(unittest.TestCase):

    def test_tap_calls_adb_tap_with_int_coords(self):
        with patch("actions.adb_actions.tap") as mock_tap:
            execute_step({"kind": "tap", "x": 100.5, "y": 200.7})
            mock_tap.assert_called_once_with(100, 200)


class PressBackHandlerTests(unittest.TestCase):

    def test_press_back_calls_helper(self):
        with patch("actions.adb_actions.press_back") as mock_back:
            execute_step({"kind": "press_back"})
            mock_back.assert_called_once()


class WaitHandlerTests(unittest.TestCase):

    def test_wait_sleeps_for_seconds(self):
        with patch("time.sleep") as mock_sleep:
            execute_step({"kind": "wait", "wait_seconds": 3.0})
            mock_sleep.assert_called_once_with(3.0)

    def test_wait_default_two_seconds_when_unspecified(self):
        with patch("time.sleep") as mock_sleep:
            execute_step({"kind": "wait"})
            mock_sleep.assert_called_once_with(2.0)


class TapButtonHandlerTests(unittest.TestCase):

    def test_tap_button_finds_and_taps(self):
        fake_frame = MagicMock(width=2400, height=1080)
        with patch("capture.adb_capture.capture_screen", return_value=fake_frame), \
             patch("actions.sail_actions._find_button", return_value=(1280, 880)) as mock_find, \
             patch("actions.adb_actions.tap") as mock_tap:
            execute_step({
                "kind": "tap_button",
                "labels": ["recruit", "Recruit"],
                "region": {"y_min_pct": 0.8, "x_min_pct": 0.75},
            })
            # _find_button got our labels + the converted region kwargs
            mock_find.assert_called_once_with(
                fake_frame, "recruit", "Recruit",
                y_min=864, x_min=1800,
            )
            mock_tap.assert_called_once_with(1280, 880)

    def test_tap_button_with_singular_label_field(self):
        """The legacy 'label' (singular) field is also accepted."""
        fake_frame = MagicMock(width=2400, height=1080)
        with patch("capture.adb_capture.capture_screen", return_value=fake_frame), \
             patch("actions.sail_actions._find_button", return_value=(100, 100)), \
             patch("actions.adb_actions.tap"):
            execute_step({"kind": "tap_button", "label": "OK"})
            # Should not raise

    def test_tap_button_raises_when_no_button_found(self):
        fake_frame = MagicMock(width=2400, height=1080)
        with patch("capture.adb_capture.capture_screen", return_value=fake_frame), \
             patch("actions.sail_actions._find_button", return_value=None):
            with self.assertRaises(StepExecutionError) as ctx:
                execute_step({"kind": "tap_button", "labels": ["nope"]})
            self.assertIn("nope", str(ctx.exception))

    def test_tap_button_raises_when_no_labels(self):
        with self.assertRaises(StepExecutionError):
            execute_step({"kind": "tap_button"})


class NavigateToHandlerTests(unittest.TestCase):

    def test_navigate_to_calls_helper_with_target(self):
        with patch("actions.sail_actions.navigate_to_building",
                    return_value=True) as mock_nav:
            execute_step({"kind": "navigate_to", "target": "inn"})
            mock_nav.assert_called_once_with("inn")

    def test_navigate_to_raises_when_helper_returns_false(self):
        with patch("actions.sail_actions.navigate_to_building",
                    return_value=False):
            with self.assertRaises(StepExecutionError):
                execute_step({"kind": "navigate_to", "target": "inn"})


class ExitToPortOverworldHandlerTests(unittest.TestCase):

    def test_exit_calls_recover_and_passes_home_port(self):
        fake_result = MagicMock(state="port_overworld")
        with patch("brain.recovery.recover_to_port_overworld",
                    return_value=fake_result) as mock_rec:
            execute_step({"kind": "exit_to_port_overworld", "home_port": "London"})
            mock_rec.assert_called_once_with(home_port="London")

    def test_exit_raises_when_state_mismatch(self):
        fake_result = MagicMock(state="building")
        with patch("brain.recovery.recover_to_port_overworld",
                    return_value=fake_result):
            with self.assertRaises(StepExecutionError) as ctx:
                execute_step({"kind": "exit_to_port_overworld"})
            self.assertIn("port_overworld", str(ctx.exception))


class TapPrimaryActionHandlerTests(unittest.TestCase):

    def test_tap_primary_action_calls_helper(self):
        with patch("brain.recovery._tap_building_action",
                    return_value=True) as mock_tap_act:
            execute_step({
                "kind": "tap_primary_action",
                "building": "inn",
                "sub_menu": "recruit_crew",
            })
            mock_tap_act.assert_called_once_with("inn", "recruit_crew")

    def test_tap_primary_action_raises_on_helper_failure(self):
        with patch("brain.recovery._tap_building_action", return_value=False):
            with self.assertRaises(StepExecutionError):
                execute_step({
                    "kind": "tap_primary_action",
                    "building": "inn",
                    "sub_menu": "recruit_crew",
                })

    def test_tap_primary_action_raises_when_no_submenu(self):
        with self.assertRaises(StepExecutionError):
            execute_step({"kind": "tap_primary_action", "building": "inn"})


class NoopAndRetryTests(unittest.TestCase):

    def test_retry_is_noop(self):
        execute_step({"kind": "retry"})  # should not raise
        execute_step({"kind": "noop"})    # should not raise


class CustomHandlerTests(unittest.TestCase):

    def test_register_action_decorator_adds_handler(self):
        called: list[dict] = []

        @register_action("test_only_kind")
        def _h(action):
            called.append(action)

        execute_step({"kind": "test_only_kind", "value": 42})
        self.assertEqual(called, [{"kind": "test_only_kind", "value": 42}])


if __name__ == "__main__":
    unittest.main()
