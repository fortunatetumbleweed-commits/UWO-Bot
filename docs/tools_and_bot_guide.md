# UWO Bot — Tools & Bot Usage Guide

Quick reference for every runnable script in the project.

---

## 1. The Bot (`run.py`)

Interactive chat loop for controlling the bot manually.

```bash
python run.py
```

Single-shot (no interactive prompt):

```bash
python run.py goto castle
python run.py routine daily
```

### Commands

| Command | What it does |
|---|---|
| `go to <building>` | Navigate to a building in the current port (e.g. `go to market`) |
| `exit` / `back` | Leave current building, return to port overworld |
| `explore [minutes]` | Visit every building in the port and record to KB (default 15 min) |
| `agent explore` | Same as explore but uses the FSM agent (with recovery) |
| `agent goto <building>` | Agent-driven navigation to a specific building |
| `agent sail <port>` | Agent-driven sail to a named port |
| `kb` | List all ports in the Knowledge Base |
| `kb <port>` | List buildings known for a port |
| `kb <port> <building>` | Details for one building |
| `kb type <type>` | Cross-port knowledge for a building type (e.g. `kb type castle`) |
| `kb market <port>` | Latest market prices for a port |
| `map explore` | Read City Info for all visible ports (world map must already be open) |
| `map ports` | List all ports with KB entries |
| `map info <port>` | Show KB entry for a specific port |
| `route` | List known trade routes |
| `route <origin> <dest>` | Show planned voyage with resupply stops (e.g. `route london port royal`) |
| `routine <name>` | Run a saved routine from `routines/definitions.py` |
| `routines` | List saved routines |
| `quit` / `q` | Stop the bot |

---

## 2. Task Runner (`run_task.py`)

Runs a fully autonomous trade loop from a YAML task file.

```bash
python run_task.py tasks/trade_london_port_royal.yaml
python run_task.py tasks/trade_ceylon_aceh.yaml --dry-run
```

`--dry-run` prints every step without sending any ADB input.

### Task file format

```yaml
name: "London to Port Royal — 1 Round Trip"
start_port: London
rounds: 1

loop:
  - action: sell_all
    port: London           # optional: skip step if bot is at wrong port
  - action: buy_all
    port: London
  - action: sail_to
    destination: Lisboa
  - action: sail_to
    destination: Port Royal
```

**Supported actions:**

| Action | What it does |
|---|---|
| `sell_all` | Navigate to market, sell all cargo |
| `buy_all` | Navigate to market, buy all recommended goods |
| `sail_to` | Sail to `destination` via world map |

At the end, a profit/loss report is printed per step and per round.

### Known task files

| File | Route |
|---|---|
| `tasks/trade_ceylon_aceh.yaml` | Ceylon ↔ Aceh (Indian Ocean short hop) |
| `tasks/trade_london_lisbon.yaml` | London ↔ Lisbon |
| `tasks/trade_london_port_royal.yaml` | London → Lisbon → Las Palmas → Port Royal (and back) |

---

## 3. Single Step Runner (`run_step.py`)

Test one action in isolation without a full task file. Useful for debugging.

```bash
python run_step.py sell           # sell all cargo at current port
python run_step.py buy            # buy recommended goods at current port
python run_step.py sail London    # sail to London
python run_step.py sell --dry-run # dry run — no ADB
```

---

## 4. Capture Session (`supervisor/capture_session.py`)

Captures screenshots during manual gameplay to build training data for the screen classifier.

```bash
python -m supervisor.capture_session              # auto mode: captures every 3s
python -m supervisor.capture_session --manual     # manual mode: press SPACE to capture
python -m supervisor.capture_session --interval 2.0
```

**Auto mode** — saves a frame every N seconds. Skips frames where the chrome (title bar,
buttons, mini-map) looks identical to the previous frame, so you get one frame per distinct
screen state rather than one per animation tick.

**Manual mode** — press `SPACE` to capture, `Q` to quit. Every press saves unconditionally.

Frames are saved to `data/sessions/<YYYY-MM-DD_HH-MM-SS>/frames/`.

After capturing, label the frames with the Labeler (step 5).

---

## 5. Labeler (`supervisor/labeler.py`)

Web UI for assigning screen-type labels to captured screenshots. Labels feed into classifier training.

```bash
python -m supervisor.labeler
# then open: http://localhost:5050
```

The UI shows one screenshot at a time. Click a label button (or press the keyboard shortcut shown
on the button) to assign a type and advance to the next unlabeled frame.

Features:
- Dashboard shows all sessions with labeled/total counts
- Jump straight to the first unlabeled frame in a session
- Filter view by screen type (e.g. see all frames labeled `market`) to review or fix labels
- Labels are saved to `data/labels.jsonl` — you can stop and resume any time

---

## 6. Classifier Trainer (`classifier/train.py`)

Trains the MobileNetV3-small screen classifier on labeled data.

```bash
python -m classifier.train
python -m classifier.train --epochs 20 --lr 1e-3
```

Reads labels from `data/labels.jsonl` and images from `data/sessions/`.
Outputs the trained model to `models/screen_classifier/model.pt`.

Run this after adding new labels from the Labeler.

---

## 7. Flow Capture & Analyzer (`tools/capture_flow.py`, `tools/analyze_flow.py`)

Records a multi-step UI transaction by capturing one screenshot per step, then sends the
sequence to Claude to produce a structured `flow.json` the bot can replay.

```bash
# Capture + analyze in one command:
python tools/capture_flow.py market_sell
python tools/capture_flow.py market_buy

# Analyze an existing screenshot directory:
python tools/analyze_flow.py market_sell --screenshots path/to/dir
```

**Workflow:**
1. Start the script
2. Perform the transaction on your phone, one step at a time
3. Press Enter after each new screen appears — the script captures that screen
4. Optionally type a short label for the step
5. Type `done` when finished
6. Claude analyzes the full sequence and writes `memory/knowledge/flows/<name>/flow.json`

The resulting `flow.json` describes each step, what to tap, what screen comes next, and
which steps are optional (e.g. a negotiation dialog that may or may not appear).

Currently defined flows: `market_sell`, `market_buy`, `market_negotiate`.

---

## Typical Workflows

### First time with a new port
1. `python run.py` → `explore` — visits every building, records to KB
2. `python run.py` → `kb <port>` — verify what was discovered

### Running a trade route
```bash
python run_task.py tasks/trade_london_port_royal.yaml
```

### Adding training data for a new screen type
1. `python -m supervisor.capture_session --manual` — capture while browsing the new screen
2. `python -m supervisor.labeler` → http://localhost:5050 — label the frames
3. `python -m classifier.train` — retrain the classifier

### Recording a new market transaction flow
```bash
python tools/capture_flow.py market_negotiate
# follow the prompts, perform the transaction on your phone
```
