# UWO Bot — Project Structure

```
uwo-bot/
├── CLAUDE.md                   # Project context for Claude Code
├── main.py                     # Entry point — starts the bot loop
├── config/
│   └── settings.py             # ADB device ID, capture FPS, timing config
├── capture/
│   └── adb_capture.py          # ADB screencap loop, returns PIL images
├── vision/
│   ├── template_matcher.py     # OpenCV template matching for UI elements
│   ├── ocr.py                  # pytesseract/easyocr for reading numbers (ducats, prices)
│   └── assets/                 # Screenshot templates (buttons, icons, port names)
│       ├── ports/              # Port name templates
│       ├── ui/                 # Common UI elements (map button, trade button, etc.)
│       └── market/             # Market screen elements
├── state/
│   └── game_state.py           # Dataclass: current port, ducats, cargo, fleet status
├── brain/
│   ├── fsm.py                  # Finite state machine (core decision engine)
│   └── states/
│       ├── idle.py             # Default state
│       ├── navigating.py       # Moving between ports
│       ├── trading.py          # Buying/selling at market
│       └── docked.py           # In port, deciding next action
├── actions/
│   └── adb_actions.py          # ADB input: tap, swipe, with human-like random delays
├── memory/
│   └── logger.py               # Logs state + action + outcome for future ML training
└── tests/
    ├── test_capture.py
    ├── test_vision.py
    └── test_actions.py
```

## Milestone Roadmap

### Milestone 1 — In-Port Exploration (build first)
- Capture screen via ADB ✓
- OCR port name from top-left corner
- Detect in-port overworld screen context
- Locate each functional building (Harbor, Market, Shipyard, Inn, etc.)
- Enter a building via proximity dialog tap
- Detect which building interior we are in
- Exit building, return to overworld

### Milestone 2 — Sea Navigation
- Open world map
- Tap destination port
- Detect successful arrival at new port

### Milestone 3 — Trading
- Read market buy/sell prices (OCR)
- Identify profitable goods
- Execute buy order
- Navigate to sell port
- Execute sell order
- Log profit/loss

### Milestone 4 — Self-learning
- Use memory logs to build trade route optimizer
- Track which routes yield best ducats/time
- Gradually replace hardcoded decisions with learned ones
