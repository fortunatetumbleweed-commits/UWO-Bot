# test_market_layout.py
# Run while inside the Market on the Purchase or Sell tab.
# Discovers and prints Put-In-Bulk and action button positions.

from memory.logger import setup_logging
setup_logging()

from capture.adb_capture import capture_screen
from actions.market_actions import _discover_market_layout

frame = capture_screen()
layout = _discover_market_layout(frame)

print("\nMarket layout:")
pib = layout.get("put_in_bulk", {})
btn = layout.get("action_button", {})
print(f"  Put-In-Bulk: ({pib.get('tap_x')}, {pib.get('tap_y')})  checked={pib.get('checked')}")
print(f"  Action btn:  ({btn.get('tap_x')}, {btn.get('tap_y')})  label={btn.get('label')}  enabled={btn.get('enabled')}")
print(f"  Notes: {layout.get('notes', '')}")
