# Quick market reader test — run while the market screen is open
from capture.adb_capture import capture_screen
from vision.market_reader import read_market_page
from memory.logger import setup_logging
setup_logging()

frame = capture_screen()
goods = read_market_page(frame)
print(f"\n{len(goods)} goods on current page:")
for g in goods:
    print(f"  {g.name:<30} price={g.buy_price}  index={g.index_pct}%  trend={g.trend}")
