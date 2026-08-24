"""The main menu covers the overworld, so the overworld is not actionable beneath it.

`_is_on_overworld` answers "can I act on the overworld now?" — callers tap the globe, the
building list, the harbour. Two independent bugs made it answer Yes on the main menu:

  1. `read_port_name` returned the player's level, **'LV 92'**, and the function treats any
     non-empty port name as proof of the overworld. (Earlier the same day the same
     pass-through returned 'World' on the world map and aborted a destination selection.)
  2. The SceneModel correctly reports `kind=port_overworld` **with `overlay=main_menu,
     is_modal=True`** — the menu really does sit on top of the overworld — but the overlay
     was ignored.

Consequence, live 2026-08-22: `exit_to_overworld` logged "Port overworld confirmed" while
the bot sat on the main menu, `open_world_map` (which classifies properly) kept reporting
main_menu, and the two deadlocked for all 10 attempts. The run died with 1 action recorded.
"""
from unittest import mock

import pytest


class _Overlay:
    def __init__(self, kind="main_menu", is_modal=True):
        self.kind, self.is_modal = kind, is_modal


class _SM:
    confidence = "high"
    top_left = None

    def __init__(self, overlay=None):
        self.overlay = overlay

    def is_at_port_overworld(self):
        return True

    def summary(self):
        return "[scene_model] family=overworld kind=port_overworld"


def _run(scene):
    import actions.sail_actions as sa
    taps = []
    with mock.patch("vision.scene_model.get_scene_model", return_value=scene), \
         mock.patch("actions.adb_actions.tap", side_effect=lambda x, y: taps.append((x, y))), \
         mock.patch.object(sa, "tap", side_effect=lambda x, y: taps.append((x, y))):
        return sa._is_on_overworld(object()), taps


class TestModalOverlayBlocksTheOverworld:
    def test_the_main_menu_over_the_overworld_is_not_actionable(self):
        ok, _ = _run(_SM(overlay=_Overlay("main_menu", is_modal=True)))
        assert ok is False, "the globe cannot be tapped through the main menu"

    def test_a_bare_overworld_is_actionable(self):
        ok, _ = _run(_SM(overlay=None))
        assert ok is True

    def test_a_non_modal_overlay_does_not_block(self):
        """Only overlays that actually cover the controls should reject."""
        ok, _ = _run(_SM(overlay=_Overlay("city_info", is_modal=False)))
        assert ok is True


class TestPortNameIsNotAStatReadout:
    @staticmethod
    def _frame():
        import numpy as np
        from PIL import Image
        return Image.fromarray(np.zeros((1080, 2400, 3), dtype=np.uint8))

    @pytest.mark.parametrize("raw", ["LV 92", "LV 1", "3,823/4,108", "51.16,-2.37"])
    def test_reads_containing_digits_are_not_port_names(self, raw):
        """A port is a NAME. 'LV 92' made the bot believe it was on the overworld."""
        from vision.ocr import read_port_name
        with mock.patch("vision.ocr.read_text", return_value=raw), \
             mock.patch("vision.text_correction.correct_port_name", return_value=(None, 0.0)):
            assert read_port_name(self._frame(), elements=[]) is None

    def test_a_real_port_name_still_reads(self):
        from vision.ocr import read_port_name
        with mock.patch("vision.ocr.read_text", return_value="Jakarta"), \
             mock.patch("vision.text_correction.correct_port_name",
                        return_value=("Jakarta", 1.0)):
            assert read_port_name(self._frame(), elements=[]) == "Jakarta"
