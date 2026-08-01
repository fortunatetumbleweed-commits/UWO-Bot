# Vision Layered Perception — Architecture and Tool Selection

*This document describes how the bot uses its vision tools today, and the
shift away from template-matching chrome detection toward an OmniParser-led
perception pipeline complemented by Moondream for scene classification.*

---

## The four vision tools and what each one is good at

The bot has four distinct vision capabilities.  They are not interchangeable
— each has a different sweet spot, latency, and failure mode.  Picking the
wrong tool for a question is the single most common source of perception
bugs in this codebase (see the May-2 cascade for the canonical example).

```
                ┌────────────────────────────────────────────────────┐
                │                  EASYOCR / PADDLEOCR               │
                │   "What text is on this screen?"                   │
                │   ~0.5s, local, free.  Reliable for legible text.  │
                │   Use for: port names, building titles, OCR-based  │
                │           text matching, button label reading.     │
                │   Avoid for: tiny icon labels, stylised game text. │
                └────────────────────────────────────────────────────┘

                ┌────────────────────────────────────────────────────┐
                │                    OMNIPARSER                      │
                │   "WHERE is every interactive element?"            │
                │   ~0.3-0.5s (parse_fast), local, free.             │
                │   Returns structured list of DetectedElement       │
                │   objects with bbox, label, element_type, conf.    │
                │   Use for: chrome flag derivation, button finding  │
                │            with bbox precision, element counting,  │
                │            location reasoning.                     │
                │   Avoid for: scene classification, semantics       │
                │              ("which one is the OK button?").     │
                └────────────────────────────────────────────────────┘

                ┌────────────────────────────────────────────────────┐
                │                    MOONDREAM                       │
                │   "WHAT KIND of screen is this?"                   │
                │   ~3-7s per question, local, free.                 │
                │   Reliable for: yes/no scene-class questions.      │
                │   Use for: "is in town?", "is open ocean?",        │
                │            "are there name plates visible?".       │
                │   Avoid for: counting, locating, OCR'ing text,     │
                │              "is there a modal dialog?" (false-     │
                │              positives expanded white panels).     │
                └────────────────────────────────────────────────────┘

                ┌────────────────────────────────────────────────────┐
                │                  CLAUDE VISION                     │
                │   "WHAT DOES IT MEAN in game context?"             │
                │   ~5-10s per call, API $, internet required.       │
                │   Rich reasoning over screenshot + game KB.        │
                │   Use for: heavy_check verification, replan,       │
                │            ambiguous reclassification.             │
                │   Avoid for: routine state-checking (cost).        │
                └────────────────────────────────────────────────────┘
```

The architecture is **layered**, not "one tool fits all":

```
perceive() ──▶ OmniParser (always)           ─────┐
            ──▶ EasyOCR  (always)             ─────┤
                                                   │
            On rule-based ambiguity:               │
            ──▶ Moondream (scene class check) ─────┤
                                                   │
            On lingering uncertainty:              ▼
            ──▶ Claude Vision (rich reasoning)
```

Each tier escalates only when the previous tier is inconclusive.  Routine
perceives stay in the cheap layers; expensive layers fire only when needed.

---

## The Moondream + OmniParser paradigm

These two tools complement each other on the exact failure modes that
template-matching chrome detection hit:

| Question                        | OmniParser                    | Moondream                     |
|---------------------------------|-------------------------------|-------------------------------|
| Is the home button visible?     | ✅ search element list for     | ❌ would need to be told      |
|                                 | icon in top-right region     | what to look for             |
| Is the right panel present?     | ✅ structured detection        | ❌ vague                      |
| Where exactly is the OK button? | ✅ exact bbox                  | ❌ "top" / "center" hand-wave |
| Is this a port town?            | ❌ doesn't reason about        | ✅ "is in town?" → yes/no     |
|                                 | scene gestalt                 |                               |
| Is this open ocean?             | ❌ same                        | ✅ reliable yes/no            |
| How many name plates visible?   | ✅ count elements              | ❌ counting unreliable        |
| Read the dialog title text      | ❌ labels are approximate;     | ❌ not an OCR engine          |
|                                 | use real OCR                  |                               |
| Is there a modal dialog?        | ✅ structural signal:          | ❌ false-positive on every    |
|                                 | centered panel + OK/Cancel    | expanded white panel          |
|                                 | buttons + nothing else        |                               |

**The combined approach:**

1. **OmniParser tells the bot WHAT'S ON the screen and WHERE**.  Run it on
   every perceive (it's fast — 0.3-0.5s parse_fast).  The element list
   becomes the primary source of truth for chrome flags AND for button
   coordinates.

2. **Moondream tells the bot WHAT KIND of screen this IS**, when the
   element-level evidence is ambiguous.  Used as a tier-2 confirmation
   on the perceive fallback path.

3. **Claude Vision** stays for ambiguous / complex cases (heavy_check
   verification, replan-on-uncertainty).  Rare; cost-bounded.

This separation matches each tool's sweet spot and avoids the
"hammer/nail" trap where one tool gets used for the wrong question.

---

## The shift from chrome detection (template match) to OmniParser

### The old approach — `vision/chrome_detector.py`

```python
class ChromeDetector:
    """Detects stable chrome UI elements using OpenCV template matching."""

    def detect(self, frame) -> ChromeState:
        # cv2.matchTemplate against pre-captured PNG templates
        # for each chrome element (home, back, hamburger, right_panel,
        # world_map_btn).  Returns boolean flags.
```

**Strengths:**
- Fast (~100ms).
- Deterministic.
- Boolean output is convenient for rule-based classifiers.

**Weaknesses (each observed in live runs):**
- **Template fragility**: a small visual change in the icon (different
  contrast at Plymouth vs London, weather affecting hue, animation frame)
  causes the template match score to drop below threshold and the flag
  goes False, even though the element is clearly visible.
- **No structured output**: only knows "home button is/isn't there", not
  WHERE it is exactly.  Action paths (`_find_button`, OK-tap dispatch)
  have to do their own OCR-based location finding, which fails on small
  labels.
- **Adding new elements requires capturing templates** — maintenance
  burden for every new chrome element.

### The new approach — OmniParser-backed chrome detection

The `ChromeState` *interface* is preserved (boolean flags `has_home`,
`has_right_panel`, etc.) but its *implementation* derives the flags from
OmniParser's element list rather than template matching.

```python
def detect_chrome_via_omniparser(frame, omniparser_elements) -> ChromeState:
    """Derive boolean chrome flags from OmniParser's structured output."""
    has_home = any(
        elem.element_type == "icon"
        and is_in_top_right_corner(elem.bbox)
        and elem.label in ("home", "house", "icon")  # OmniParser label heuristic
        for elem in omniparser_elements
    )
    has_right_panel = any(
        is_right_edge_panel(elem.bbox) and elem.height > 200
        for elem in omniparser_elements
    )
    # ... etc for back, hamburger, world_map_btn
```

**Strengths:**
- **Robust**: OmniParser's image-feature detector is trained on UI element
  detection, not pixel-pattern matching.  Small contrast / position drift
  no longer flips the flag.
- **Structured output reused**: the same OmniParser run that produces
  chrome flags also produces the full element list for action-level
  decisions.  No second model call needed.
- **Rich coordinates included**: chrome flags become more than booleans —
  callers that need to know WHERE the home button is can query the same
  element list (`elem.cx`, `elem.cy`).
- **No template maintenance**: adding new chrome elements is a code-only
  change; no PNG capture step.

**Weaknesses:**
- ~3-5x slower than template matching (300-500ms vs 100ms).
- Auto-labels are heuristic — `home button` might be labeled `icon` if the
  caption model wasn't run.  Region-based heuristics + label fuzzy match
  cover this.

The speed cost is acceptable because perceive() already takes 5-30s
end-to-end (chrome → OCR → Moondream → Qwen cascades).  An extra 300ms
to get a more reliable chrome verdict is a net win — it prevents the
20-30s misclassification timeout cascades that template-match misses
have caused.

### Migration shape

```
Layer 1 — Doc + interface  (this commit)
  Chrome detector docs + interface clarified.  ChromeState fields are
  the contract; implementation is swappable.

Layer 2 — OmniParser-backed implementation
  vision/chrome_detector.py grows an alternate path: when an OmniParser
  element list is available, derive the chrome flags from it instead of
  running template matching.  Falls back to template match when OmniParser
  is unavailable / disabled.

Layer 3 — _find_button via OmniParser
  Replace the OCR-based label search in _find_button with an OmniParser
  element list lookup for known dialog patterns (OK / Cancel / Confirm /
  Recruit).  Falls back to OCR for unknown labels.  Eliminates the
  centre-tap fallback class of bugs at the action layer.

Layer 4 — perceive caches OmniParser per frame
  perceive() runs OmniParser once at the top, caches the element list on
  the PerceiveResult.  Downstream callers (chrome detection, _find_button,
  cue catalog observations) all read from the cached list instead of
  re-running.
```

Each layer is independently shippable and incrementally improves
robustness.  Layer 2 ships first because it removes the most common
template-miss class.  Layers 3 and 4 are mechanical follow-ups.

---

## Tool-selection guidelines (for future patches)

When adding new perception logic, choose the tool that matches the
question:

```
What you need to know                      Use this tool
─────────────────────────────────────────────────────────────────
"Is there a button labeled X?"              OmniParser (label match in elements)
"Where exactly is the OK button?"           OmniParser (element bbox)
"How many ships are in the fleet list?"     OmniParser (count text elements
                                             in the right region)
"Is there a centered modal dialog?"         OmniParser (structural: centered
                                             panel + OK/Cancel + nothing else)

"Is the player in a town?"                  Moondream (scene-class yes/no)
"Is this open ocean?"                       Moondream
"Is there an active flow / dialog          Moondream + OmniParser
 covering the chrome?"                      (Moondream for scene, OmniParser
                                             for the panel structure)

"What does the screen title say?"           EasyOCR / PaddleOCR (read the
                                             top-left region)
"What's the price text in this market       OCR (read the price column)
 row?"

"Did the recruitment commit?  How much     Claude Vision (heavy_check —
 gold was spent?  Is the goal achieved?"    rich reasoning over the cue
                                             catalog + screenshot)
"Why did this step fail?  What's the       Claude Vision (replan)
 next action?"
```

### Anti-patterns (do NOT use these tool/question combinations)

- **Moondream for "is there a dialog?"** — false positive on every
  expanded white panel.  Use OmniParser's structural signal
  (centered panel + OK/Cancel + nothing else).
- **Moondream for OCR** — replies "None" or hallucinated text.  Use
  EasyOCR/PaddleOCR.
- **Moondream for counting** — unreliable.  Use OmniParser element list
  length.
- **Moondream for spatial reasoning** ("where is X on screen") —
  unreliable.  Use OmniParser bbox.
- **OmniParser for scene class** — doesn't reason about gestalt;
  just sees elements.  Use Moondream.
- **Template matching for stable-but-visually-varying icons** — Plymouth
  vs London right panel differs enough to fail template match.  Use
  OmniParser.
- **Centre-of-frame tap as button-not-found fallback** — landed on dialog
  body in May-2 incident, dismissed without confirming.  Use OmniParser
  bbox or refuse to tap.

---

## Status of related documents

- `docs/perception_reasoning_layer.md` — describes the L2.5 Qwen layer for
  scene description.  STILL CURRENT — Qwen is a perception primitive that
  works alongside the four tools above; this doc and that one are
  complementary.
- `docs/planner_architecture.md` — the planner uses these vision tools as
  building blocks (heavy_check uses Claude Vision; light_check uses local
  perception which now includes OmniParser).  STILL CURRENT.
- `docs/fsm_design.md` — SUPERSEDED, but its description of chrome
  detection / state classification is the historical context for what
  this doc is replacing.

---

## Net summary

The chrome-detection-template-match approach is being retired in favour
of OmniParser-backed element detection because the latter is more robust
and produces structured output that's reusable for action-level decisions.
Moondream remains the right tool for scene-class questions but should
NEVER be asked "is there a modal dialog?" — that question belongs to
OmniParser's structural signal.  The two tools layer cleanly: OmniParser
answers WHERE, Moondream answers WHAT KIND, OCR answers WHAT TEXT, Claude
Vision answers WHAT DOES IT MEAN.  Picking the right tool for the right
question eliminates entire classes of bugs (May-2 cascades, recruit-
confirm-dismiss-instead-of-OK, Plymouth right_panel template miss) that
came from the wrong-tool-for-the-job pattern.
