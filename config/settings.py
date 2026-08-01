# config/settings.py
# Global configuration — ADB device, timing, capture, and vision settings.

# ADB
ADB_DEVICE_ID: str = "31101JEHN26098"  # physical device
ADB_TIMEOUT: int = 10            # seconds before ADB command is considered failed

# Screen capture
CAPTURE_FPS: int = 5             # target frames per second for the capture loop
SCREEN_WIDTH: int = 2400         # fixed resolution width — landscape (update if orientation changes)
SCREEN_HEIGHT: int = 1080        # fixed resolution height

# Input delays — intentionally slower than a human to avoid anti-bot detection.
# No two taps ever fire within ACTION_COOLDOWN_MIN seconds of each other.
TAP_DELAY_MIN: float = 1.0
TAP_DELAY_MAX: float = 2.0

# Vision
TEMPLATE_MATCH_THRESHOLD: float = 0.85   # minimum confidence for a template match to count
ASSETS_DIR: str = "vision/assets"

# OCR regions — (left, top, right, bottom) in pixels at 2400x1080
# "text_only" excludes the globe icon to the left of the port name
OCR_PORT_NAME_REGION: tuple = (310, 10, 650, 75)

# Market screen UI coordinates (full image, 2400x1080)
MARKET_COORDS: dict = {
    "back":     (60,   47),    # < back arrow, top left
    "purchase": (70,  145),    # • Purchase
    "sell":     (65,  225),    # • Sell
    "home":     (2350, 40),    # home icon, top right
}

# Market goods content area — covers the full goods grid including the leftmost column.
# Left boundary x=50 (not 650) so top-left tiles like Goldware are not cropped out.
# The sub-menu column (x=0–650) is excluded from tile detection by the right-panel
# guard (_RIGHT_PANEL_X) rather than by a left crop.
MARKET_CONTENT_REGION: tuple = (50, 130, 2380, 980)

# Scroll coordinates for paging through the goods list.
# Swipe from MARKET_SCROLL_START down to MARKET_SCROLL_END to advance one page.
MARKET_SCROLL_START: tuple = (1500, 800)   # start point (x, y) — finger down
MARKET_SCROLL_END:   tuple = (1500, 300)   # end point   (x, y) — finger up

# Right-side building menu panel region (full image, 2400x1080).
# The bot scans this area with OCR each step to discover what buildings are
# currently visible and where — no hardcoded item coordinates.
BUILDING_MENU_REGION: tuple = (1850, 380, 2400, 1080)

# Minimum OCR confidence to treat a detected text block as a building label.
BUILDING_MENU_OCR_MIN_CONFIDENCE: float = 0.35

# Left-side sub-menu panel inside a building (full image, 2400x1080).
# When inside a building the available services (Hire Sailors, Exchange, etc.)
# are listed as tappable buttons on the left side of the screen.
BUILDING_SUBMENU_REGION: tuple = (0, 100, 650, 1000)

# ── Right-panel tab bar (2400x1080) ─────────────────────────────────────────
# Four tabs at the top of the right panel: tasks / buildings / people / location
# Measured from live screenshot 2026-04-12.
TAB_TASKS_COORD:    tuple = (2093, 155)
TAB_BUILDINGS_COORD: tuple = (2180, 155)
TAB_PEOPLE_COORD:   tuple = (2267, 155)
TAB_LOCATION_COORD: tuple = (2354, 155)   # toggles the mini map on/off

# ── Mini map (visible below the tab bar when location tab is active) ─────────
MINIMAP_REGION:        tuple = (2155, 175, 2400, 365)   # bounding box
MINIMAP_PORT_MAP_COORD: tuple = (2240, 250)             # tap here (centre of map) → opens port map
MINIMAP_WORLD_MAP_COORD: tuple = (2335, 320)            # globe icon (bottom-right) → opens world map
# Pixel luminance at the port-map tap coord when the mini map is visible.
# The map area is light-grey (~200+); hidden/background area is dark (<80).
MINIMAP_GLOBE_VISIBLE_THRESHOLD: int = 150

# ── Port map screen ───────────────────────────────────────────────────────────
PORT_MAP_BACK_COORD: tuple = (177, 20)    # < back button top-left
PORT_MAP_OCR_REGION: tuple = (0, 50, 2400, 1050)  # scan area for building labels
# Top-left region where the screen title is shown on any opened screen.
# e.g. "Port Map" when the port map is open.
SCREEN_TITLE_REGION: tuple = (0, 0, 500, 60)

# Walkable ground waypoints for random port exploration (full image, 2400x1080)
# Spread across open areas of the port — avoid building interiors.
PORT_WALK_WAYPOINTS: list = [
    (400,  700),
    (600,  600),
    (800,  750),
    (1000, 650),
    (1200, 700),
    (1400, 600),
    (600,  850),
    (900,  850),
    (1100, 800),
    (1300, 750),
    (500,  500),
    (750,  500),
]

# ── Chrome element detection (2400x1080) ─────────────────────────────────────
# These are the stable UI chrome elements used for deterministic scene
# classification. Each entry is a (region, template_filename) pair where
# region is (left, top, right, bottom) — the search window for matchTemplate.
#
# Template PNG files live in vision/assets/chrome/.
# Capture them once with:  python -m vision.chrome_detector capture
#
# Regions are intentionally tight around the known button positions so that
# template matching is fast and avoids false positives from game content.
CHROME_TEMPLATES_DIR: str = "vision/assets/chrome"

# Search regions for each chrome element
CHROME_HAMBURGER_REGION:        tuple = (2280, 5,   2400, 80)    # ≡ top-right, port overworld only
CHROME_HOME_REGION:             tuple = (2280, 5,   2400, 80)    # ⌂ top-right, buildings + port map
CHROME_BACK_ARROW_REGION:       tuple = (0,    0,   220,  80)    # ← top-left, buildings + port map
CHROME_WORLD_MAP_BTN_REGION:    tuple = (0,    880, 320,  1080)  # globe+"World map", bottom-left, port map only
CHROME_RIGHT_PANEL_REGION:      tuple = (2050, 100, 2400, 420)   # tab bar + mini map, overworld only

# Minimum template match score (0–1) to consider an element present.
CHROME_MATCH_THRESHOLD: float = 0.75

# Logging
LOG_DIR: str = "memory/logs"
LOG_LEVEL: str = "DEBUG"
