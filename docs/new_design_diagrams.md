# New Design — Diagrams (system / state / sequence)

Visual companion to `docs/refactor_plan_perceive_flow_fsm.md` and
`docs/architecture_review_perceive_flows_2026-08.md`.  Diagrams are Mermaid
(render on GitHub and most markdown viewers).

**Notation.** `base` = underlying screen state; `overlay` = orthogonal
popup/dialog axis; a **verified** step = perceive → act → re-perceive → verify
expected post-state.  Tiers: **local** (cheap, every tick) · **VLM** (rich
screens, low-frequency) · **RL** (strategy, later).

---

## 1. System diagrams

### 1.1 Tiered perception → verified action loop (end to end)
```mermaid
flowchart TB
    Phone["Android phone (game)"] -->|"screencap (1 frame/tick)"| Cap["capture"]
    Cap --> Fuse

    subgraph Perc["Perception — tiered, arbitrated"]
      direction TB
      Local["Local, every tick:<br/>family CNN · fingerprints ·<br/>layout-slot detector · detect_dialog"]
      Vlm["VLM — rich screens only:<br/>Claude Vision · Qwen-VL · Moondream"]
      Ocr["OCR on localized slots"]
      Fuse["Fusion / arbitration<br/>(one confidence scale)"]
      Local --> Fuse
      Vlm --> Fuse
      Ocr --> Fuse
    end

    Fuse --> PS["PerceivedState<br/>base + overlay + identity + confidence"]
    PS --> Loop

    subgraph Loop["Verified action loop"]
      direction TB
      R1["resolve overlay if present"]
      R2["assert precondition (base)"]
      R3["act"]
      R4["re-perceive + verify expected_post"]
      R1 --> R2 --> R3 --> R4
    end

    Loop --> Dec
    subgraph Dec["Decision tiers"]
      direction LR
      Mech["Mechanics<br/>deterministic contract"]
      Und["Understanding<br/>VLM + goal prompt"]
      Strat["Strategy<br/>RL / bandit (later)"]
    end

    Dec -->|"tap / swipe"| ADB["actions/adb + orientation guard"]
    ADB -->|input| Phone
    Loop -.->|"log (state,action,post,verified)"| KB[("Affordance / experience KB")]
    KB -.->|priors| Dec
```

### 1.2 Explore mode vs Play mode (feeding the affordance KB)
```mermaid
flowchart LR
    subgraph Ex["Explore mode — Claude, occasional/thorough"]
      direction TB
      E1["enumerate elements<br/>(layout detector + OmniParser)"]
      E2["predict effect (VLM)"]
      E3{"safe / reversible?"}
      E4["probe (bounded)"]
      E5["observe outcome"]
      E6["record: known, not probed"]
      E1 --> E2 --> E3
      E3 -->|yes| E4 --> E5
      E3 -->|"no (irreversible / red-gem)"| E6
    end
    E5 --> KB[("Affordance KB<br/>per building_type")]
    E6 --> KB
    KB --> Pl
    subgraph Pl["Play mode — fast, goal-driven"]
      direction TB
      P1["per-building goal spec"]
      P2["act via KB affordances<br/>+ fast local perception"]
      P1 --> P2
    end
    Pl -.->|"novelty / failure"| Ex
```

### 1.3 Model tiers & training (distillation)
```mermaid
flowchart LR
    Frames[("data/sessions frames")] --> Teach
    subgraph Teach["Offline teachers (expensive)"]
      TC["Claude Vision (paid)"]
      TM["Moondream (~5-7s)"]
    end
    Teach -->|"auto-label"| Train["train students"]
    Train --> Slot["layout-slot detector (YOLO)"]
    Train --> Cap2["small captioner / classifier (LoRA)"]
    Slot --> Runtime["Runtime: fast, local, pixel-aware"]
    Cap2 --> Runtime
    Runtime -.->|"novel / ambiguous only"| Teach
```

---

## 2. State-machine diagrams

### 2.1 Base navigation states (overlay is a separate, orthogonal axis)
```mermaid
stateDiagram-v2
    [*] --> loading : app launch or login
    loading --> port_overworld : arrived at destination port
    loading --> village : arrived at destination village
    loading --> sea : still sailing, auto-route in progress
    port_overworld --> market
    port_overworld --> building
    port_overworld --> world_map : open world map
    port_overworld --> sea : depart
    sea --> world_map : open world map
    world_map --> sea : set sail auto-route, or close if opened at sea
    world_map --> port_overworld : close if opened at port
    sea --> port_overworld : arrive at port
    sea --> village : arrive at village
    market --> port_overworld : back
    building --> port_overworld : back
    village --> sea : depart

    note right of market
      OVERLAY axis is orthogonal to base:
      none | dialog | confirm | negotiation | result
      | news | reward | error | main_menu (game hub)
      main_menu overlays sea OR port_overworld (open/close,
      base unchanged) — just like a dialog.
      An overlay does NOT change the base —
      base is kept from last-confident tick.
    end note
```

### 2.2 The verified action loop (control state machine)
```mermaid
stateDiagram-v2
    [*] --> Perceive
    Perceive --> HandleInterrupt : overlay present but UNEXPECTED
    Perceive --> AssertPre : ready to act
    HandleInterrupt --> Perceive : resolve generically, then resume
    AssertPre --> Act : precondition ok
    AssertPre --> Fail : precondition wrong
    Act --> Verify : re-perceive
    Verify --> Perceive : step ok, action has more steps
    Verify --> Done : action complete and verified
    Verify --> Fail : mismatch or stuck
    Fail --> Escalate : structured failure up
    Escalate --> [*]
    Done --> [*]

    note right of Perceive
      Overlay is EXPECTED vs UNEXPECTED w.r.t. the current
      action's contract:
      - EXPECTED (confirm / keypad / negotiation / result) =
        a STEP of the action -> act on it (tap OK, enter qty),
        do NOT dismiss. It is just the next 'ready to act'.
      - UNEXPECTED (news / event / error / reward / main_menu)
        = an interrupt -> resolve generically, then resume.
      DialogModel.kind says HOW to handle an overlay;
      expected-vs-unexpected says WHICH branch.
    end note

    note right of Escalate
      bounded retry / replan / safe-abort.
      NEVER inner-loop-retry forever.
      headless: safe autonomous fallback, not a crash.
    end note
```

### 2.3 Overlay resolution sub-machine (dialog handling)
```mermaid
stateDiagram-v2
    [*] --> Detect
    Detect --> None : no overlay
    Detect --> Classify : detect_dialog fires
    Classify --> Dismiss : informational / system / reward
    Classify --> ActInDialog : confirmation, part of my transaction
    Classify --> AskVLM : unknown or quest, must read
    AskVLM --> ActInDialog : VLM picks button for goal
    Dismiss --> Verify
    ActInDialog --> Verify
    Verify --> None : overlay gone
    Verify --> Detect : still present, retry bounded
    None --> [*]

    note right of Dismiss
      tap the DETECTED close/X or action —
      wherever it is (fixes daily-news
      X-outside-frame). Not a hardcoded coord.
    end note
```

---

## 3. Message sequence charts (key procedures)

### 3.1 One verified action step (the core loop)
```mermaid
sequenceDiagram
    autonumber
    participant L as Loop
    participant P as Perception
    participant D as Detectors and VLM
    participant C as ActionContract
    participant A as ADB
    participant G as Game
    L->>P: perceive()
    P->>D: run tiered detectors
    D-->>P: evidence (scored)
    P-->>L: PerceivedState(base, overlay)
    opt overlay present
        L->>A: resolve overlay (detected affordance)
        A->>G: tap close / ok
        L->>P: re-perceive
    end
    L->>C: precondition ok? (base)
    C-->>L: yes
    L->>A: act
    A->>G: tap / swipe
    L->>P: re-perceive
    P-->>L: PerceivedState'
    L->>C: verify(post == expected_post)
    alt verified
        C-->>L: +progress (log tuple)
    else mismatch / stuck
        C-->>L: -fail (structured → escalate)
    end
```

### 3.2 Market buy — Play mode (goal-driven)
```mermaid
sequenceDiagram
    autonumber
    participant M as Market mission
    participant L as Loop
    participant R as Market reader
    participant K as KB
    participant A as ADB
    participant G as Game
    M->>L: goal = buy profitable goods within budget
    L->>L: assert base == market
    L->>R: read grid (goods, prices, demand)
    R-->>L: structured goods[]
    L->>K: which is profitable at destination?
    K-->>L: choose good X (+ qty)
    L->>A: tap detected tile X
    A->>G: tap
    L->>L: re-perceive + verify tile loaded
    L->>A: tap detected Purchase button
    A->>G: tap
    L->>L: overlay == confirm → tap OK (detected)
    L->>L: verify result dialog → +progress
    Note over L,K: on read failure → escalate to VLM, not blind flow-coords
```

### 3.3 Dialog / overlay handling (daily-news over the inn)
```mermaid
sequenceDiagram
    autonumber
    participant G as Game
    participant L as Loop
    participant P as Perception
    participant Dg as detect_dialog
    participant A as ADB
    Note over G: daily-news popup appears over the inn
    L->>P: perceive()
    P->>Dg: structural overlay check (every tick, ungated)
    Dg-->>P: DialogModel(kind=informational, close=X@detected)
    P-->>L: base = inn (last-confident), overlay = dialog
    L->>A: tap DETECTED close X (wherever it is)
    A->>G: tap X
    L->>P: re-perceive
    P-->>L: base = inn, overlay = none
    Note over L: resume the inn goal
```

### 3.4 Explore mode — discover the Trade-Point chest affordance
```mermaid
sequenceDiagram
    autonumber
    participant X as Explore Claude
    participant Ly as Layout detector
    participant V as VLM
    participant A as ADB
    participant G as Game
    participant K as KB
    X->>Ly: enumerate elements (market)
    Ly-->>X: slots incl. trade_point_chest (glowing = actionable)
    X->>V: predict — what does this element do?
    V-->>X: claim reward when points full, cost none
    X->>X: safe / reversible? yes
    X->>A: tap chest
    A->>G: tap
    X->>V: observe outcome
    V-->>X: reward claimed, plus blue gems
    X->>K: write affordance market.trade_point_chest, when=points_full, effect=reward, cost=none
    Note over X,K: irreversible or red-gem elements are recorded as known, not probed
```

### 3.5 Grow trade-round (orchestration, incl. failure escalation)
```mermaid
sequenceDiagram
    autonumber
    participant S as self_grow
    participant L as Loop
    participant N as Nav sail_to
    participant Mk as Market
    participant R as Recovery
    S->>L: assert at home port
    alt orientation wrong
        L->>R: guard fails → refuse / re-lock (no mis-tap)
    end
    S->>S: pick destination
    S->>N: sail_to(destination)
    N-->>S: ARRIVED (verified port @ high conf)
    S->>Mk: sell cargo → buy goods (goal-driven)
    alt market read ok
        Mk-->>S: transaction verified (+profit)
    else read fails
        Mk->>R: structured failure (no blind coords)
        R-->>S: safe-abort round (headless: no crash)
    end
    S->>N: sail home, then repeat
```

---

## Cross-references
- Design + decisions: `docs/refactor_plan_perceive_flow_fsm.md`
- Findings behind the design: `docs/architecture_review_perceive_flows_2026-08.md`
