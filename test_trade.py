# test_trade.py — run while inside the Market building
# Usage:
#   python test_trade.py sell              # sell all cargo
#   python test_trade.py buy Pomegranate   # buy max Pomegranate
#   python test_trade.py buy Pomegranate 50  # buy 50 Pomegranate

import sys
from memory.logger import setup_logging
setup_logging()

from vision.ocr import read_port_name
from capture.adb_capture import capture_screen

frame = capture_screen()
port = read_port_name(frame) or "unknown"

if "--port" in sys.argv:
    idx = sys.argv.index("--port")
    port = sys.argv[idx + 1]

print(f"Port: {port}")

cmd = sys.argv[1] if len(sys.argv) > 1 else "sell"

if cmd == "sell":
    from actions.market_actions import sell_all_cargo
    result = sell_all_cargo(port=port)
    print(f"\nSold: {result}")

elif cmd == "buy":
    from actions.market_actions import buy_goods, BuyOrder
    # Usage: buy Good1 [qty1] [Good2] [qty2] ...
    # If qty is omitted or non-numeric, defaults to "max"
    args = sys.argv[2:]
    if not args:
        print("Usage: python test_trade.py buy <good_name> [quantity] [good2] [qty2] ...")
        sys.exit(1)
    orders = []
    i = 0
    while i < len(args):
        name = args[i]
        i += 1
        qty = "max"
        if i < len(args) and args[i].isdigit():
            qty = int(args[i])
            i += 1
        orders.append(BuyOrder(name=name, quantity=qty))
    result = buy_goods(orders, port=port)
    print(f"\nBuy result: {result}")

elif cmd == "auto":
    from actions.market_actions import auto_buy
    result = auto_buy(port=port)
    print(f"\nAuto-buy result: {result}")

else:
    print("Usage: python test_trade.py sell|buy|auto [good] [qty]")
