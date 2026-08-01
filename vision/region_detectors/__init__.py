# vision/region_detectors/__init__.py
#
# Deterministic region detectors for the SceneModel.  Each detector
# owns one rectangle of the screen and produces a structured region
# object consumed by vision/scene_model.py.
#
# Slice 1 ships:
#   - bottom_chrome  (universal: wifi/uid/server strip)
#   - top_left       (universal but family-aware)
#
# Subsequent slices:
#   - left_menu      (chromed scenes — slice 2)
#   - action_buttons (chromed scenes — slice 3)
#   - right_panel    (universal, variant-aware — slice 4)
#   - centre_panel   (chromed scenes — slice 4)
#   - top_right_chrome (chromed scenes — slice 2)
#   - overworld_world, row_2_buffs, level_progress, main_menu_overlay,
#     overworld_panel (overworld scenes — slice 6+)
#   - world_map      (slice 4)
