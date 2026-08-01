from capture.adb_capture import capture_screen
from vision.omniparser import get_omniparser
from vision.claude_vision import get_claude_vision

frame = capture_screen()
omni = get_omniparser()
elements = omni.parse(frame)
print(f'OmniParser: {len(elements)} elements')

cv = get_claude_vision()
inv = cv.analyse_scene(frame, 'port_overworld', 'Socotra', detected_elements=elements)
if inv:
    print(inv.summary())
    for e in inv.interactive():
        print(f'  {e.label} @ ({e.tap_x}, {e.tap_y})')
