# test_sail.py — sail from current position to a destination port
#
# Usage (run while inside a building OR on the port overworld):
#   python test_sail.py Lisbon
#   python test_sail.py Lisbon --from-overworld   # skip building exit step

import sys
from memory.logger import setup_logging
setup_logging()

if len(sys.argv) < 2:
    print("Usage: python test_sail.py <destination_port> [--from-overworld]")
    sys.exit(1)

destination = sys.argv[1]
from_building = "--from-overworld" not in sys.argv

from actions.sail_actions import sail_to_port

result = sail_to_port(destination, from_building=from_building)
print(f"\nSail result: {'arrived' if result else 'failed'}")
