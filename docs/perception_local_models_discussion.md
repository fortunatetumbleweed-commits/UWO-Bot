# Perception, Local Models, and LLM Consultation — Discussion Notes

Captured 2026-05-04.  Resume work tomorrow.  This doc summarises the
design discussion that followed the labels-only Claude experiment on
the three harbour sub-menu frames (Supply / Repair / Recruit Crew).

The session showed three things:
1. Labels-only Claude can navigate the bot correctly *most* of the
   time.  It chose "Recruit Crew" sidebar item from both the Supply and
   Repair screens unprompted.
2. Claude builds context across the conversation — by the third frame
   it was explicitly referencing the sidebar pattern it had inferred
   from the prior two frames.
3. Without colour info, Claude can pick a mode-selector (e.g.
   *"Normal Recruit 218,714"*) instead of the actual gold commit
   button (*"218,714 Recruit"*) — labels alone cannot distinguish them.

---

## 1. API key — no need to exit the conversation

Two practical paths:

**One-off** for testing scripts:
```bash
ANTHROPIC_API_KEY=sk-... python -m tools.labels_only_consult <png>
```

**Persistent** in a separate terminal:
- Open a new terminal tab/window (one not running Claude Code).
- `export ANTHROPIC_API_KEY=sk-...` in that shell.
- Run experiments there.

Claude Code runs in its own subprocess and inherits whatever env was
set when it was launched.  Changes to your interactive shell after
launch do not propagate back, but they don't need to — running the
experiment in a separate shell is fine.

Operational bot calls (`_resolve_blocker_with_reasoning`) already work
in live runs, so the key is set somewhere — likely in `~/.zshrc` or a
launch script.  Check with `cat ~/.zshrc | grep ANTHROPIC` in any
terminal.

---

## 2. Less restrictive prompt → ranked options + try-multiple

Current Claude prompt says "Recommend ONE next tap" — brittle.  Better
prompt shape:

> Rank the top 3 candidate taps by likelihood of advancing my goal.
> For each: label, (cx,cy), confidence 0-1, one-line reasoning.
> Also include "press_back" as a candidate if relevant.

Runtime then tries them in order:

1. Tap candidate #1 → check post-tap signature changed AND a
   transaction occurred.  (No-op detection already exists and will
   catch "tapped, nothing happened.")
2. If no progress → press Back to undo, try candidate #2.
3. After ~3 failed candidates → fall through to the heavier image
   revamp call.

This turns a single LLM call into a **bounded exploration**.  Each
candidate gets ~5s to prove itself.  Worst case 3 × 5s = 15s; success
case usually #1 → 5s.  Much more robust than single-shot, and the LLM
isn't penalised for ranking ties (e.g. *Normal Recruit* vs *218,714
Recruit* — both in top 3, bot tries the first, sees it's a
mode-selector, tries the next, succeeds).

**Implementation:** small change to `_resolve_blocker_with_reasoning`
in `actions/sail_actions.py` — change the prompt to ask for a ranked
list, parse 3 candidates, loop with no-op check between.

---

## 3. What Qwen can do (today and at limit)

Currently `vision/qwen_perception.py` runs Qwen2.5-1.5B-Instruct-4bit
via MLX, locally, ~3-5s per call.  Used by `qwen_perceive` for L2.5
detail enrichment — turning a generic `building: harbor` into a
freeform `"Plymouth, Wet Season, Harbor Official dialog visible"`.

**At 1.5B parameters, what Qwen can reliably do:**
- Pattern-match labels against a known taxonomy you give it.
- Pick a button from a short ranked list when the right answer is
  obvious.
- Summarise screen state in 1-2 sentences.
- Follow tightly-templated instructions ("output JSON with exactly
  these fields").

**What Qwen reliably *can't* do at 1.5B:**
- Multi-hop reasoning ("you're on Supply, need to recruit, navigate
  via the sidebar to Recruit Crew, then tap the gold Recruit").
- Distinguish similar-looking labels by intent (Normal Recruit vs
  218,714 Recruit — the kind of mistake Claude made once).
- Long-context memory across many turns.

So Qwen is good for **constrained pickers** ("given these 5 labels in
the action region, which one is most likely to be the commit button?")
but unreliable for **open-ended planning** ("my crew is short, what's
the full sequence to fix it?").

**Upgrade options:** Qwen2.5-3B-4bit on the same MLX runtime — about
2× slower (~6-10s) but markedly better at multi-hop reasoning.  Or
Qwen2.5-7B if you have 16GB+ RAM headroom on the Mac.

---

## 4. Local context for Qwen — three ways

**A. System prompt with domain knowledge.**
Put a 1-2K-token UWO primer in the system message: screen taxonomy
(port_overworld / building / sub_menu / sea / world_map), harbour
tabs (Supply / Repair / Recruit Crew / Departure), typical
action-button positions, visual conventions (gold = active commit,
grey = disabled).  Loaded once at startup, applied to every call.
Free, instant, dramatically improves accuracy for known scenes.

**B. Few-shot examples.**
Add 3-5 worked examples to the prompt: *"Here's a Supply screen.
Here are its labels.  The right tap was 'Recruit Crew' at (247, 371).
Now: here's a new screen, what's the right tap?"*  Bumps small-model
accuracy substantially.  Static, version-controlled.

**C. Retrieval-augmented (the most powerful).**
Index every successful tap from past runs by
`(screen_signature, goal)`.  At inference, find the K nearest matches
and include them as in-context examples.  The bot literally remembers
prior wins.  Cheap to build on top of `data/training/` which is
already accumulating frames — a vector index on label-bag-of-words
would work.

**The right order:** A first (one afternoon), B when you've manually
annotated a few "gotcha" screens, C once you have ~100+ successful
transactions to retrieve from.

---

## 5. Game-specific local model

Two flavours, ranked by effort:

**Retrieval (cheapest, no training):**
Embedding model (e.g. all-MiniLM-L6) maps `(sorted_labels + goal)` →
vector.  Cosine-similarity lookup against `data/training/`.  Returns
the closest past example's `correct_tap`.  As the bot runs, the index
grows; accuracy improves automatically.  ~1-2 days of work; runs in
milliseconds.

**LoRA fine-tune Qwen-1.5B on game-specific data:**
Format examples as `{labels, goal} → {tap_label, cx, cy}`.  Fine-tune
for an hour on Apple Silicon with a few hundred examples.  Inference
is the same speed as base Qwen but accuracy on UWO screens approaches
or exceeds Claude Sonnet on this narrow task.  ~1-2 weeks of work
including data collection + eval; once trained, runs forever for
free.

**Both at once — the data flywheel:**
This is what `CLAUDE.md` already describes as Phase 2.5.  Currently
`data/training/market_tile/` is the only category being filled.  The
same pattern would work for `button_recommendation/` — every Claude
call seeds a training example, and a local model gets trained from
accumulated data periodically.  The retrieval approach is the bridge
that makes it useful before the local model is trained.

---

## Recommended next steps (priority order)

1. **Switch the Claude prompt to ranked top-3** (small, immediate
   resilience win).  ~30 minutes of work.
2. **Pixel colour bucket on each OmniParser element**
   (disambiguates mode-selectors from commits — addresses the
   *Normal Recruit vs 218,714 Recruit* miss).  ~1-2 hours.
3. **Qwen system prompt with the UWO primer** (small, free at runtime
   forever after).  ~1 hour.
4. **Persistent Claude conversation per goal** (the context-build
   effect captured in the experiment — Claude got better at frame 3
   because it had seen frames 1 and 2).  ~2-3 hours.

Each is independent and self-contained.  (1) and (2) directly fix the
issues observed in the labels-only experiment; (3) and (4) reduce
ongoing Claude API spend by leveraging context.

After those four, the natural next layer is the retrieval-augmented
button-recommendation index — turn `data/training/` accumulation into
a usable inference path.

---

## Reference: the experiment

**Frames analysed** (in `data/sessions/2026-05-04_20-26-52/`):
- `0000_2026579366.png` — Harbor / Supply menu (sub_menu | action:supply)
- `0001_2027064173.png` — Harbor / Repair menu (sub_menu | action:repair)
- `0002_2027121661.png` — Harbor / Recruit Crew menu (sub_menu)

**Tooling:**
- `tools/perceive_dump.py` — runs every perception layer on each frame
  and prints raw OmniParser elements + EasyOCR readtext + chrome
  flags + fingerprint match + interruptor scan + flow scan + the
  context-aware drain preview.
- `tools/labels_only_consult.py` — builds a prompt with all detected
  elements + OCR tokens + active goal, sends to Claude (when
  `ANTHROPIC_API_KEY` is set), prints the response.

**Key observations from the experiment:**
- All three harbour sub-menu frames return *no fingerprint match* —
  the registry doesn't know about Supply / Repair / Recruit Crew as
  sub-menu titles.  Real bug; fix is to add them to the
  `sub_menu_title` LabelSet.
- L5 interruptor on Supply frame fires
  `harbor_supply_manual_dialog_dismiss` — false positive on a clean
  supply screen.  Source of the May-04 6-cycle dismissal loop.
- OmniParser's `confidence` field is YOLO shape-detection confidence,
  *not* button enabled/disabled state.  Visual styling (gold vs grey)
  is discarded during element extraction.
- Claude got the Recruit Crew commit wrong (chose
  *Normal Recruit 218,714* mode-selector at (2091, 828) instead of
  the actual commit *218,714 Recruit* at (2092, 897)) when given
  labels only.  Colour info would have disambiguated.
