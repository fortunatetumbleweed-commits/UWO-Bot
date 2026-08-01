# Journey: Planner Architecture and the Perception Revamp

*Picking up where `journey_not_enough_crew.md` left off — the recruit-crew
recipe was working but produced "five wasted steps" and the architecture
couldn't tell "this works" from "this works but could be better." Six
weeks later the bot recruits crew end-to-end through the planner's
verification + replan loop, and the perception layer has been pulled
apart and rebuilt around OmniParser as the structural primary. The
cost was twenty-something live runs each surfacing a new failure mode
that the architecture didn't yet have a name for.*

---

## Where the Last Journey Ended

The closing thought of the previous journey was a question, not a
solution: *a working solution is not a permanent solution*. The
recipe-walking architecture (`flows.json` + `learned_recoveries.json`)
could capture "this sequence of taps recruits crew at the inn" but
couldn't say "this sequence has five wasted steps; revisit it when
a better one is observed." A recipe was either in the KB or it wasn't.
Demotion existed but only as a binary disable — not a way to **plan**
toward the goal, **verify** progress, and **replan** when the recipe
fell short.

The next thing to build was an architecture that distinguishes:

- **Goal** — what we want (e.g. *fleet has crew sufficient to depart*)
- **Plan** — a sequence of steps believed to achieve it
- **Verify** — did each step move us closer? did we actually achieve the goal?
- **Replan** — when a step fails or the goal isn't reached, amend the plan

That structure is what the **planner** is. The journey from there to
a bot that actually recruits crew end-to-end was a six-phase migration,
followed by a perception revamp that took longer than the planner did.

---

## Phase 1–4: The Planner

**Phase 1 — Edge metadata + planner skeleton.** The first thing the
recipe-walker was missing was metadata on its edges. A flow-step had
no notion of "this is what I expect to see after the action" or
"here's how to know if the action committed something." `Plan` and
`PlanStep` types in `brain/plan.py` made those expectations first-class:
each step has an `expected_progress` (light verification — cheap, runs
every step) and optionally an `expected_goal` (heavy verification —
Claude-led, runs at checkpoints). `PlanRuntime` gained `lookup`,
`commit_success`, and `commit_failure` so plans could accumulate
empirical evidence over runs.

**Phase 2 — Migrate `sail_to` to the planner.** The first migration
target was the simplest: sail from one port to another. The planner's
seven-step sail plan replaced ad-hoc loops in `actions/sail_actions.py`,
and the planner ran successfully on every test sail.

**Phase 3 — Remove direct `where_am_i` calls.** Seventeen sites in the
codebase had been calling `where_am_i()` from outside the central
loop. Each was a place where local action code was making local
decisions about state — exactly the pattern that
`docs/fsm_design.md` had identified as the problem two milestones
earlier. They all got rewired through `perceive()` so the central
loop is the single decision point.

**Phase 4 — Layer 4 (replan + live wiring).** The hardest piece: when
verification fires `UNCERTAIN` or `NOT_YET`, what does the planner do?
The answer was a four-question Claude prompt (`brain/replan.py`):
*what's on screen, what was expected, has the goal been achieved, what
should the next step be?* The response is structured — `continue`,
`insert_step`, `replace`, or `escalate` — and `apply_replan_to_plan`
mutates the live plan accordingly. `brain/known_goals.py` wired one
goal end-to-end: `achieve_has_enough_crew()` calls `achieve_goal()`
with live `perceive_fn`, `capture_fn`, `execute_step_fn`, `replan_fn`.
The cutover commit (`2ac3578`) routed `recovery.execute_resolution`
to `achieve_has_enough_crew()` for the `not enough crew` blocker.

That's the architecture the rest of the journey needed.

---

## The Perception Bottleneck

The first time the planner ran live on `not_enough_crew`, it didn't
fail at planning. It failed at **seeing**. Step 2 (`navigate_to inn`)
timed out at 60 s while the bot was actually standing inside the
harbor with the Harbor Official saying *"Supply your fleet and set
sail"*. Perception classified the screen as `unknown`. The plan had
no idea where the bot was, so it couldn't proceed.

The perception layer was a chain of fallbacks:

```
chrome template match → OCR port-name top-left → Moondream "is in town?"
                                              → Moondream "is at sea?"
                                              → unknown
```

Each link had its own failure mode. Chrome templates broke when icon
contrast drifted (Plymouth's home button rendered slightly different
from the captured PNG). Top-left OCR reads the port name on
`port_overworld` — but on a building screen the top-left is the
building title, not a port name, so reading it as "port name" returns
nothing. Moondream's "is in town?" correctly says `No` inside a
building. So three signals all returned negative, the chain fell off
the end, and the bot was lost in a state that wasn't actually unknown
at all — it was a perfectly ordinary harbor screen.

This is when the perception revamp started. The next ten commits were
all about it.

---

## Phase 5a–5b: Hardening Without Replacing

**Phase 5a** added diagnostic INFO logging on every classifier branch
and rewrote the fallback path's wording — the previous detail string
contained the literal phrase *"vision check says NOT at sea"* which
Qwen was paraphrasing back as a screen description, then
`handle_unknown_blocking` was saving it as a phantom interruptor.
The phantom-interruptor save filter in `_save_new_interruptor` rejects
descriptions that contain perceive-context phrases AND lack any
detection keywords from the actual frame. The Qwen prompt also
stopped including the perceive `detail` field — so Qwen reasons over
OCR tokens directly instead of paraphrasing the bot's own self-talk.
A debounce on `handle_unknown_blocking` (3 same-signature perceives
required before invoking learn-and-dismiss) stopped transient overlays
from generating more phantom interruptors.

**Phase 5b** added a vision-led port_overworld fallback. When chrome
+ OCR + flow detection all failed, perceive now asks Moondream *"Is
the bot in a port town?"* before falling through to `unknown`. A `Yes`
returns `port_overworld` with a degraded confidence rating and a
detail string that explicitly notes the vision-led classification.
A new `memory/knowledge/control/ui_elements.json` documented the three
categories of overlay the bot actually sees: chrome (persistent
controls), in-world overlays (street NPC bubbles, building name
plates), modal dialogs (system popups). The taxonomy itself was the
fix — Phase 5b's bug had been categorising street NPC bubbles as
modal dialogs, which led to dismiss-loops on screens that didn't
actually have anything to dismiss.

---

## Phase 5c: The Vision Architecture Doc

After 5b, every perception bug felt like the same bug looked at from
a different angle. The fix was a doc that named the four vision
tools and what each is good at:

| Tool | Question | Cost | Reliability |
|---|---|---|---|
| **OmniParser** | WHERE is every interactive element? | 0.3–0.5 s local | High (structural) |
| **Moondream** | WHAT KIND of screen is this? | 3–7 s local | High (yes/no) |
| **EasyOCR** | WHAT does the text say? | 0.5 s local | Medium (drift) |
| **Claude Vision** | WHAT does it MEAN in game context? | 5–10 s API | Highest (rare) |

`docs/vision_layered_perception.md` made the picking-the-wrong-tool
mistakes legible. A bunch of bugs were "Moondream answered an
OmniParser question" or "fixed-crop OCR was the wrong tool for a
question OmniParser already answers structurally."

The doc also planted a flag: *every state has a structural fingerprint
that survives chrome / font / position drift*. That fingerprint is an
OmniParser element list. Everything downstream of this doc is a
gradual migration toward making the OmniParser element list the single
source of truth for state classification.

**Phase 5c L2** built `vision/chrome_via_omniparser.py` —
`detect_chrome_from_elements(elements)` derives `has_back_arrow`,
`has_world_map_btn`, `has_right_panel` from OmniParser's element list
in normalised regions. Because `parse_fast` can't disambiguate the
home icon from the hamburger icon (same region, no captions), the
flags `has_home` and `has_hamburger` still go through template
matching; `fuse_chrome_states` combines the two sources. This was
opt-in at first (a new `detect_with_omniparser(frame, elements)`
method on `ChromeDetector`) so existing callers were untouched.

**Phase 5c L3** tightened `_find_button` from substring matching
(`"ok" in label`) to whole-word matching (regex `\bok\b`). Caught a
class of bugs where short canonical labels matched random words —
"OK" matching "Stock", "X" matching "Exit", "No" matching "Notice".

**Phase 5c L4** added a per-frame `parse_fast_cached(frame)` keyed
by `id(frame)` with a 4-entry cap. The dominant per-frame vision cost
(parse_fast at 0.3–0.5 s) was previously paid once per `_find_button`
call; with the cache, repeated lookups within a perceive tick are
free.

---

## The Daily News Detective Story

In the middle of Phase 5c the bot started dismissing the market
Purchase modal as if it were the daily news popup. The cascade:
pixel signature at (1794, 240) matched (the Purchase modal also has
a round X close icon there), then Moondream's confirmation prompt
asked *"Is there a small centred popup window with a round black X
close button?"* — yes, of course there is, said Moondream, looking at
the Purchase modal. The bot dismissed the Purchase modal and the trade
loop fell over.

The user articulated the actual invariants:

> Daily news only shows on overworld screens (sea or port_overworld),
> never inside a building. The close X is positioned **outside** the
> dialog window, hanging above its top-right corner — every other
> in-game dialog has its X **inside** the frame.

Two specific structural facts. Two specific fixes:

1. **Context guard** — chrome detection runs first. If `has_home` or
   `has_back_arrow` is True (we're inside a building), short-circuit to
   `False` without paying the Moondream cost. The May-2 incident dies
   here: Purchase is a sub-menu of the market → `has_back_arrow=True`
   → reject without any model call.

2. **Visual prompt sharpened** — *"Is the round X close button
   positioned OUTSIDE the popup, hanging just above its top-right
   corner? If the close X is inside the popup's border, answer 'no'."*
   Generic in-game dialogs (X inside) say no; only daily_news (X
   outside) says yes.

Indeterminate / exception paths flipped from `return True` to
`return False` (fail closed). Previously the cost calculus was wrong —
false positives dismiss live game screens; false negatives just delay
daily_news by one tick. The new defaults match the actual costs.

The lesson was bigger than this one bug: **structural invariants
articulated by the user become structural assertions in the code.**
Both the "only on overworld" and "X outside the frame" rules are
specific enough to encode and test, and the project memory now carries
them so future detectors start from these constraints rather than
re-discovering them.

---

## Phase 5d: World Map Exploration via OmniParser

`explore_visible_ports_on_world_map` was the next victim. The function
was OCR-everything-on-screen and tap-each-text-fragment. When daily
news overlaid the world map, the OCR picked up article fragments —
*"Tne dig one IS"*, *"Britain!"*, *"need to build a lot:"* — and the
bot dutifully tapped each one as if it were a port. The tap sequence
went through NPC dialogue snippets, player names from the right
panel ("TaylorFP"), and timestamps ("00.00–23.59") for ~12 minutes
before the safety net stopped it.

The fix was structural. `find_world_map_city_candidates(elements,
port_kb)` filters OmniParser text elements through:

1. **Allow-list** — fuzzy match against `PORT_POSITIONS` keys (83
   known ports). High-confidence match returns the canonical port name.
2. **Shape filter** — for unknown ports, accept text that looks like
   a proper noun: each whitespace-split word starts uppercase with
   only lowercase afterwards (with hyphenated capitals OK for
   "Saint-Malo"); common connectors (`de`, `la`, `of`) excepted; no
   digits; no sentence punctuation.
3. **Right-panel skip** — anything with `cx >= 1500` is City Info
   panel content, not a map label.
4. **Control blocklist** — `Port`, `Explore`, `Route`, `Trade` (mode
   tabs), `World Map` (title), `Go to City` (action button), and the
   bottom-left utility buttons are explicitly rejected as port
   candidates while remaining valid through their own call paths.

Tests anchored the May-2 garbage strings: every fragment that the
bot tapped on May-2 16:04 is now rejected by the filter.

The approach scaled: the user supplied annotated frames showing the
City Info panel structure (`Base / Trade / Facility / Invest` main
tabs with sub-tab decompositions), and that taxonomy is now in
project memory ready for the deferred `walk_city_info_panel_tabs`
work.

---

## Phase 5e: The Structural Revamp

Phase 5c had built OmniParser-backed alternatives. Phase 5d had
proven they worked for one specific function. Phase 5e was the
top-down replacement: **OmniParser-driven structural fingerprints
become the primary state classifier, with the chrome/OCR/Moondream
chain as fallback only.**

**L1** built `vision/screen_classifier.py:classify_screen(frame, elements)`.
Each game state has a structural fingerprint, expressed in **normalised
coordinates** so the bot is resolution-agnostic:

| State | Fingerprint |
|---|---|
| `building` | building-name title in top-left + top-right icon + back arrow + optional bottom-center NPC dialog |
| `sub_menu` | menu-item title + back arrow |
| `port_overworld` | port name in top-left + right-edge tab/minimap cluster |
| `world_map` | "World Map" title + mode tabs + "Go to City" |
| `port_map` | back arrow + "World map" button bottom-left |
| `sea` | sailing HUD tokens (Day, ETA, Sailing, …) |

`brain/perceive.py:_classify_nav_state` calls `classify_screen` first;
on any non-unknown verdict (HIGH or MEDIUM confidence), it returns
immediately. Tonight's harbor-with-NPC-dialog stuck case became a
pinned test (`test_harbor_with_npc_dialog_classifies_as_building`):
synthetic element list reconstructs the May-2 18:00 frame, classifier
returns `state='building', detail='harbor'`, HIGH confidence.

**L2** replaced fixed-crop OCR helpers (`read_port_name`,
`read_screen_title`, `read_building_menu`) with OmniParser-element
queries in normalised regions. Each takes an optional
`elements=` so the perceive flow can pass the cached `parse_fast_cached`
result without double-parsing. The legacy fixed-crop path remains as
a fallback when OmniParser is unavailable. Bug discovered and fixed
along the way: the `_looks_like_real_text` vowel-ratio garbage filter
(designed for fixed-crop OCR noise) was rejecting valid names —
"Plymouth" and "World Map" both have exactly 25% vowels and fail the
strict `> 25%` threshold. OmniParser-sourced text is conf ≥ 0.4
filtered upstream so the vowel filter is unnecessary on its path.

**Fix-pack** that landed alongside L2 fixed three bugs the live runs
had surfaced:

1. **Plan KB demotion reset** — after several runs failed because
   step_2 hit the (now-fixed) perception bug, `inn_recruit_route`
   was demoted to `confidence='low'` with `failure_count >= 3`.
   `Plan.should_skip()` filtered it out so `achieve_goal` returned
   `no_plan_available` instantly, falling through to the legacy
   Claude path with its own bugs. Reset to `unverified, 0`.

2. **`sub_menu` state added to FSM graph.** Phase 5e L1's classifier
   introduced `state='sub_menu'` but the FSM state graph had no node
   for it, so `recover_to_port_overworld` logged "no FSM path from
   sub_menu" and fell back to blind Back+Home — which abandons
   in-progress sub-menu transactions. Sub_menu now has `press_back →
   building` as its only navigation exit; recovery to port_overworld
   goes 2 hops (preserves transaction context).

3. **`navigate_to_building` recognises sub_menu of the target.** When
   perceive returns `state='sub_menu'` and the title is a known
   sub-menu of the target building (per `building_types/<target>.json`),
   press Back once to climb to the building's main pane and continue.
   Without this, polling for `state='building'` while sitting at a
   perfectly-fine sub-menu inside the target ran out at 60 s.

---

## The Positive-Button-Tap Primitive

The planner could now plan, perceive, and verify. But on the recruit
screen specifically, the seeded `inn_recruit_route` plan was
**incomplete by design**: step 3 opens the Recruit Crew sub-menu,
step 4 exits to the overworld, and the **actual recruit-and-confirm
sub-sequence** between them was a TODO. The plan author's note:
*"replan inserts the missing tap-Recruit + tap-OK + dismiss-innkeeper
sub-steps automatically when heavy_check at step 4 reports
has_enough_crew is NOT_YET."*

That assumed Claude replan would always be available, would always
generate accurate region constraints, and would always identify the
right buttons. None of those assumptions held in practice. Claude's
generated plans had the regions in the wrong format
(`{x, y, w, h}` instead of `{x_min_pct, …}`) so spatial constraints
were silently dropped; `_find_button` then walked OmniParser's element
list in reading order and matched the title-bar "Recruit Crew" before
the gold "Recruit" button further down.

The user articulated the architectural fix:

> Click the positive button on the screen, until no new dialog shows
> up and no new screen shows up. After that it's verify-goal time.
> Replan can not happen before that.

That's `commit_via_positive_taps`. Generic primitive, in
`brain/commit_actions.py`. The action handler discovers what to tap
dynamically each call:

- `find_positive_button(elements)` picks the screen's primary action
  button by label allow-list (Recruit, OK, Confirm, Yes, Continue, Buy,
  Sell, Depart, Collect, …) with whole-word matching that rejects
  short-label false positives, and a negative-label deny-list (Cancel,
  No, Close, Back, X, …).
- The selector goes through three iterations of refinement:
  - **First version** preferred right-bottom by position. Worked on
    synthetic tests but failed live: a right-panel header text saying
    "Recruit Crew" at top-right matched as the selected positive
    button, and the actual gold "Recruit" further down was missed.
  - **Second version** added position thresholds (`cx_norm < 0.30`
    rejected, `cy_norm < 0.15` rejected) to skip menu items and
    title bars. Worked, but felt like hardcoding shouldn't be needed.
  - **Final version** uses `element_type='button'` as the primary
    discriminator. OmniParser's `_merge_icons_and_text` promotes a
    YOLO-detected icon shape with text inside to `element_type='button'`;
    title bars, menu list items (even when highlighted with the
    yellow-glow streak), and right-panel headers all stay as
    `element_type='text'` because their underlying shape isn't a
    button rectangle. The discriminator the game's UI design itself
    encodes — the bot inherits it for free. Position thresholds
    became a fallback for when YOLO misses a button shape.

The cycle stops by **button sequence**, not screen signature: track
each `(label, cx_norm, cy_norm)` tapped; when the next positive button
matches one already tapped within position tolerance, the loop closes.
On the recruit screen the cycle is: tap Recruit → confirmation dialog
→ tap OK → back at recruit screen → see Recruit again → stop. Two
taps. The recorded sequence is returned so a follow-up commit can save
it as a learned edge.

The action handler runs the cycle to completion: dialogs in this flow
are dismissed only via their positive button, never via Cancel /
Close / Back / X / outside-dialog tap. Negative buttons are
**never tapped** — confirmed by an explicit test. The user's rule was
encoded as code structure.

---

## Replan Protection

After commit_via_positive_taps shipped, the next live run executed
`step_3b_commit_recruit` correctly — and then the run after that
failed because **Claude's replan had replaced step_3b**. The plan
notes recorded it: *"replan replaced steps from index 3"*. With
`step_3b` gone, the bot reverted to the pre-fix behaviour: opened
the Recruit Crew sub-menu, then immediately ran Claude's hardcoded
`tap_button "Max"` step which couldn't find the button in its
guessed region.

The user's framing was the spec:

> Positive button tapping is the top priority. To find a new path
> you must go through a full cycle of positive button tapping. Replan
> can not happen before that.

Concrete fix: `apply_replan_to_plan` now preserves any step in the
`DECISION_REPLACE` dropped range whose `action.kind` is in
`_PROTECTED_ACTION_KINDS = {"commit_via_positive_taps"}`. Protected
steps go to the **front** of the replacement sequence so the
positive-button cycle runs before any Claude-inserted recovery.
Claude can still INSERT new steps before/after, REPLACE the
post-protected-step tail, ESCALATE, or CONTINUE — but the commit
cycle is structurally undeletable.

Three new tests pin the protection: protected step preserved in
replace, plain replace unchanged, multiple protected steps preserved
in original order.

---

## The First Successful Recruit

May-2 22:25 evening run, all the pieces in place:

```
22:25:34 — Fleet not ready: 'not enough crew' — execute_resolution
22:25:34 — achieve_has_enough_crew starts plan='inn_recruit_route' (7 steps)
22:27:32 — recovery reached port_overworld          [step 1]
22:29:08 — Tapping 'inn' in building list           [step 2]
22:29:57 — Inside 'inn' confirmed                   [step 2 done]
22:31:22 — Tapping 'inn'/'recruit_crew' @ (249, 370) [step 3]
22:32:04 — state=sub_menu title='Recruit Crew'      [step 3 done]
22:32:56 — [commit] iter 0: tap '205,848 Recruit'
                    @ (2092,898) (positive)         [step 3b — gold button]
22:33:09 — [commit] iter 1: positive button '205,848 Recruit'
                    already tapped — cycle closed   [step 3b done]
22:33:18 — Interruptor dismissed (innkeeper dialog)
22:33:36 — Interruptor dismissed (recruit confirmation)
22:36:40 — recovered to port_overworld with detail
                    'recruited a crew of 953 members'  [step 4 done]
```

The gold Recruit button position `(2092, 898)` was discovered live —
no coordinate baked into any plan or test. `find_positive_button`
saw `element_type='button'`, label `205,848 Recruit` (OmniParser
merged the cost figure with the verb), passed the whole-word match
on "recruit", was the rightmost-bottommost candidate, got tapped.
Two iterations later the cycle closed at one tap.

---

## What's Still Open

The recruit completed. Then the bot tried to navigate back to the
harbor and got stuck because **the perception layer is still ad hoc**.
Specifically:

- **`port_overworld` matches sea screens** when right-edge sea HUD
  icons cluster into the right_edge_panel signal. The matcher order
  has port_overworld before sea, and port_overworld accepts the
  right-edge cluster as a sufficient signal even without a port-name
  match. Sea screens have their own right-side widgets; they double-
  match.

- **`main_menu` has no fingerprint at all** in `screen_classifier.py`.
  When the bot accidentally opens the hamburger side panel, the right
  edge contains menu items (`combat`, `lighthouse`, `assault`, …) and
  the screen matches port_overworld via the same right_edge_panel
  signal.

- **Interruptor detection still uses pure OCR-keyword matching**
  (`brain/perceive.py:_detect_interruptors`). A learned interruptor's
  detection_keywords match against full-frame OCR text without any
  structural verification. On a misclassified main_menu screen, an
  unrelated interruptor's keywords incidentally matched and the bot
  spent five iterations tapping "OK / X" on a screen that had neither.

These three are the same class of bug, observed three different ways:
**the perception layer uses a mix of fixed-position OCR, template
matching, and ad-hoc OmniParser fingerprints, with no consistent
structural model for what each game state IS in OmniParser-native
terms.**

The next phase is the comprehensive structural revamp. The shape of
it is:

- **L1** — `vision/state_fingerprints.py`: one canonical fingerprint
  per state, expressed in OmniParser-native terms only
  (element_type counts, label sets, normalised region clusters,
  positive-and-negative signals).
- **L2** — `screen_classifier.py` becomes a thin reader of the registry.
- **L3** — interruptor detection consults the same registry. Modal
  popup is a structural shape (centred frame + OK/Close button visible).
  Keyword KB augments rather than primary-detects.
- **L4** — chrome template matching becomes the rare fallback (Phase
  5c L2 already built the OmniParser path; flip it on by default).
- **L5** — audit and migrate any remaining fixed-crop / template paths.

That's a multi-commit pass. The current journey ended at the last
working commit; the next journey begins there.

---

## What This Journey Earned

Lessons from the planner build:

1. **Light verification + heavy verification + replan is the
   minimum viable planner.** Cheap per-step checks catch obvious
   failures; expensive checkpoint checks confirm goal achievement;
   replan handles the gaps. Removing any of the three breaks the
   architecture's ability to self-correct.

2. **Provenance matters.** Hand-authored plans, Claude-replan steps,
   and learned edges all need to be distinguishable in the KB —
   because they have different reliability profiles and different
   correction strategies.

3. **Demotion thresholds are a tax on early failures.** A plan with
   one underlying perception bug can demote to confidence=low after
   three runs and effectively disable itself; even after the bug is
   fixed, the plan stays demoted until manually reset. The follow-up
   architectural fix (`PlanRuntime.lookup` falling back to demoted
   plans when no alternatives exist) is still pending.

Lessons from the perception revamp:

4. **One source of structural truth.** Mixing chrome-template-match
   + fixed-crop-OCR + Moondream verbal classification multiplies
   failure modes. OmniParser's element list is one source; everything
   else should be a query against it, not an independent system.

5. **Normalised coordinates from the start.** Every region constant
   should be `(0.0, 1.0)` floats scaled by the actual frame size,
   not absolute pixel boxes. The cost is one division; the benefit is
   resolution-independence forever.

6. **`element_type` is the discriminator most code wanted.** When
   the question is "is this an action button or a label?", the
   answer is structural — YOLO sees a button shape or it doesn't.
   Position thresholds are an approximation; element_type is the
   real signal.

7. **The user's articulated invariants become code assertions.**
   "Daily news only fires on overworld." "The X is outside the
   dialog frame." "Positive button cycle runs before replan." Each
   was a sentence; each became a structural rule with tests pinning
   it. The conversation log of this debugging is itself a design
   spec — the bot's correctness comes from absorbing the user's
   knowledge of the game's UI invariants and encoding them as
   verifiable rules.

8. **The planner architecture needs to protect critical sequence
   steps from its own replan mechanism.** Without the protection,
   Claude replan happily replaces the very thing the planner needs
   to function. With the protection, replan augments rather than
   overwrites.

---

*Where the previous journey ended with "a working solution is not a
permanent solution," this one ends with the planner mechanism that
makes that distinction operational. The bot now has plans with
verifiable expectations, replans on uncertainty without overwriting
the load-bearing parts, and a perception layer that's two-thirds
through its migration to a single structural primary. The next
journey is the last third — and after it, every state classification
in the bot routes through one OmniParser-native fingerprint registry,
not three.*
