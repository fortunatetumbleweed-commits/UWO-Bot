# Journey: "Not Enough Crew" — From a Stuck Loop to a Self-Learning Bot

*A debugging saga that started as a single failed tap and ended with the bot autonomously
discovering how to hire crew through a multi-dialog flow it had never seen before.
Along the way the bot's brain grew an "ask-Claude-when-stuck" loop, learned to distinguish
hardcoded UI assumptions from KB-driven ones, and surfaced an architectural question
about what "learned but improvable" should mean.*

---

## Prequel — Before "Not Enough Crew" Could Even Be a Concept

Long before the Interview-screen trap, the bot couldn't even *frame* the not-enough-crew
problem. It just kept failing in increasingly creative ways.

**Era 1 — Blind taps.** The early bot would arrive at the harbour and tap "Depart Now"
unconditionally. When the button was disabled (because crew was short, or supply was low,
or some other blocker was active), the tap silently did nothing. The bot would wait,
re-enter the harbour, tap again, wait, re-enter — for minutes. There was no notion of
"this button is grayed out for a reason." Around the same time, when the bot did notice
it needed crew, it sometimes tried to **recruit at the harbour** — which has its own
recruit_crew sub-menu with a confirmation panel, structurally different from the Inn's.
Those attempts would land in unrelated dialogs and fall further off the rails.

**Era 2 — Local-loop hell.** The deeper problem was architectural. Every action function
in `actions/sail_actions.py` had its **own** retry loop calling `where_am_i()` and making
local decisions. `navigate_to_building` had a loop. `_depart_from_harbour` had a loop.
`exit_to_overworld` had a loop. There was no shared state, no transition map, no global
picture of what the bot was doing. When one function returned False, the caller had no
way to recover other than calling another function — which would start its own local
loop. An unexpected departure landed the bot at sea, recovery declared "sea" stable,
the next step tried to navigate to a building, and the bot spun in unrecognised states
for minutes. `docs/fsm_design.md` opens with exactly this diagnosis:

> The original `brain/agent.py` described a `FSM + perceive → reason → act → learn`
> loop. In practice what was built is a series of **independent action functions** that
> each call `where_am_i()` in a local loop and make local decisions. There is no shared
> state, no transition map, and no global picture of what the bot is doing.

The principle was right; the implementation had drifted off it.

**Era 3 — The Milestone 3 refactor.** Commit `427f895` ("Milestone 3: FSM recovery layer
+ two-KB architecture") was the big rewrite. It hurt — `actions/sail_actions.py` broke
in several places along the way — but it was the unavoidable correction:

- Local where_am_i loops in action functions were **deleted**. Each action no longer
  decides "should I retry?" or "am I stuck?" on its own; it does its job and reports
  success or failure to a centralised recovery layer.
- A three-pass `brain/perceive.py` replaced ad-hoc state checks: interruptors first,
  then active flow, then nav state.
- `brain/fsm_registry.py` loaded states/flows/interruptors from KB and computed BFS
  paths back to `port_overworld` for every state — so recovery from any state had a
  declarative answer.
- `brain/recovery.py` consolidated all stuck-state logic. `assert_at_port` became the
  entry guard for action functions; `recover_to_port_overworld` was the single
  recovery primitive.
- `brain/human_escalation.py` introduced terminal prompt → Claude API parse → execute →
  save-to-KB as the last-resort fallback.
- `brain/kb.py` split into `ControlKB` (how the UI works) and `StrategyKB` (how to play
  the game well). All hardcoded keywords moved into `memory/knowledge/control/` JSONs.

After this, recovery was no longer something each function had to invent locally. The
brain finally had the shape its CLAUDE.md docstrings had been describing all along.

**Era 4 — Naming the blocker.** Commit `5322ffc` ("Never abort on sail/departure failure")
introduced the `blocking_signals` concept: each flow could declare conditions that prevent
its terminal step. `harbor_departure` got two signals — `not_enough_crew` and
`not_enough_supply` — each with a `description`, an `action`, and (for `not_enough_crew`)
a six-step `resolution` sequence. For the first time the bot could *say*
"I cannot depart because I don't have enough crew" instead of just timing out at a
disabled button. The `_tap_depart_button` function checks the OCR text against
`blocking_signals` from KB before searching for departure buttons — and on a match,
returns "blocked" immediately so no 90-second timeout is wasted. The trade-loop runner
also stopped silently swallowing sail failures: any failure now flows into
`recover_to_port_overworld`, which escalates to Claude → human if automated recovery
can't reach a stable state.

**Era 5 — Generic learning of unknown blockers.** Commit `95666a4` ("Add generic
learn-and-dismiss for unknown blocking screens") closed the last big gap before the
recruit-crew arc could begin. When perception returned "unknown" and a blocking dialog
was on screen, the bot would now ask Claude to classify it, save the answer as a learned
recovery, and dismiss it — instead of either crashing or silently looping.

By the end of this prequel, the architecture finally matched what the bot had needed
all along: a single perception layer, a single recovery layer, KB-driven flows with
declared blocking conditions, and an escalation chain for the unknown. Only at this
point did "not enough crew" become a tractable, *named* problem the bot could try to
solve. The eight acts that follow are the story of actually solving it.

---

## The Original Symptom

The bot was running its trade loop when it hit a familiar blocker at the harbour:

```
Fleet not ready: 'not enough crew' — Fleet crew below departure minimum
— hire crew at an Inn then return
```

The `harbor_departure` flow's KB has a six-step recipe for this exact case:
exit harbour → go to Inn → tap Recruit Crew → exit → go to harbour → retry.

In theory this should just work. In practice the resolution kept failing in surprising ways
across multiple runs, and each failure exposed a different layer of fragility in the bot's
brain. What follows is the eight-act story of getting from "stuck in an enter-exit loop"
to "the bot actually departs with a full crew."

---

## Act 1 — The Interview Screen

**Symptom.** The bot taps `Inn` in the building list, character starts walking. Forty
seconds later the location oracle still reports `port_overworld` — because the cooldown
on retry was 12 seconds, the bot retaps the Inn entry. The second tap is queued by the
device and lands *inside* the inn, exactly when the scene transition completes. Inside
the Inn there are mate NPCs standing around; tapping a mate opens an `Interview` dialog.
The bot is now stuck on a screen its KB has never seen before.

**The trap.** When the bot tried to recover, `navigate_to_building("inn")` saw the
screen title was `"interview"`, didn't match `"inn"`, pressed Back blindly. After Back,
Qwen's free-text scene description hallucinated *"the game is in the harbor building"* —
a Qwen-only confabulation. The bot's substring match on `"inn"` failed against this
hallucinated text, and a learned-recovery lookup keyed on the post-Back description
also failed (the keyword was `"interview"` but the description after Back no longer
contained it). The bot fell through to human escalation.

**The fixes — three independent failure modes:**

1. **NPC-aware re-tap.** Replaced the 12-second cooldown with a 60-second cooldown plus
   a "did the screen change since the last tap?" check. The signature filters the
   building-list OCR against canonical building names from the KB so NPC speech bubbles
   (which the OCR region picks up alongside the real labels) don't churn the signature
   on every frame. A 120-second hard cap forces a retry if the signature flickers
   indefinitely.

2. **Sub-screen recognition.** Added a `sub_screens` field to `building_types/<x>.json`
   so the bot knows that *Interview* is inside the *Inn*. When `navigate_to_building`
   detects we're in a known sub-screen of the target building, it presses Back once
   and trusts the destination — without re-validating against Qwen's possibly-hallucinated
   post-Back title.

3. **Pre-Back learned-recovery lookup.** When the title doesn't match, look up a learned
   recovery using the **current** screen's keywords first (where "Interview", "Geographer",
   "Loyalty" are still visible) before issuing the blind Back that destroys the keywords.

This was the first lesson the run hammered home: *trust signals that come from OCR;
treat Qwen's freeform description as a hint, not ground truth.*

---

## Act 2 — The Wrong Place to Tap

**Next run.** The Interview-screen loop was gone, but the next run failed at a different
spot. `_tap_building_action("inn", "recruit_crew")` looked for any element labelled
`"recruit"` or `"hire"` (loose aliases inherited from when the same labels worked at the
harbour). OmniParser auto-labels some incidental element near the top-left chrome with a
description containing the word "recruit" — possibly an NPC tooltip — and the substring
match accepted it. The bot tapped `(183, 154)` (top-left chrome area), nothing happened,
and the resolution proceeded to step 4 (`exit_to_port_overworld`) without ever opening
the recruit dialog.

**Two diagnoses:**

1. **The labels list was too loose.** "Recruit" alone matches the inn's overworld
   building-name-panel description ("Recruit mates and hire new mates") and any
   incidental tooltip with "recruit" in it. Tightened to just `["recruit crew"]` —
   the literal menu-item text inside the Inn.
2. **No region constraint.** OmniParser was free to match anywhere on the screen.

**The user's clarification reframed the second fix into a universal pattern:**

> The menu of a building is always a list on the left side under the screen title.
> After a menu item is selected, the screen title changes to that menu item's string.

That observation collapsed several special cases into one rule. `_tap_building_action`
now applies a **default region** for any building's sub-menu items —
`{y_min_pct: 0.10, x_max_pct: 0.30}` — the left-side strip below the chrome.
Per-sub-menu KB regions still override when needed, but the base case is universal.

**Bonus — a falsified hypothesis.** When this round's verification fired
("Screen title unchanged after tap — action likely did not register"), the proposed
fix was to soften the check on the theory that sub-menu taps don't change the title.
The user corrected this directly: tapping a sub-menu **does** change the title to the
sub-menu's name. The verification was right; the bug was elsewhere. The softened check
got reverted within the next round. *Cheap to undo, valuable to record.*

---

## Act 3 — Tapping the Right Place but Not Finishing

**Third run.** Tap landed correctly on the Recruit Crew menu item at `(249, 370)`.
Title changed to `"Recruit Crew"`. The recruit panel opened with a yellow Recruit button.
But the resolution sequence — written long before we had this kind of multi-step
dialog support — didn't have a confirm step. It went straight to `exit_to_port_overworld`,
the recruit was never confirmed, and the next departure attempt found "not enough crew"
again.

**This was the architecturally interesting moment of the journey.** Rather than patch
the resolution sequence with a "confirm action" step, the user proposed a more general
solution:

> Whenever there is an unknown screen, we try to do it [via Claude], for every new
> frame, and record the solution as a flow so it can be reused.

That direction matches the data flywheel principle from CLAUDE.md ("learn once, reuse
forever") — but applied to **behaviours**, not just to perception.

**The build:** A new module `brain/claude_guidance.py` with a `claude_guided_loop` that

- captures the current frame, calls Claude with the goal + perceive result + action history,
- expects a single next action: `tap`, `wait`, or `complete`,
- with disambiguation hints because UWO routinely shows the same word in three places
  on one screen ("Depart" as tooltip, tab title, and yellow action button):
  `visual_hint` (yellow/gold/gray), `position_hint` (right_panel, bottom_right, …),
  explicit `region` in pct fractions, with `fallback_coord` only when the button has
  no readable label,
- executes the prescribed action and re-perceives,
- terminates on `action: "complete"`, on reaching a known terminal state, on detecting
  an active known flow (so the existing flow machinery can take over), or on `max_steps`,
- on success, persists the recorded sequence as a flow in `flows.json` and registers
  each step in `flow_detection_order` so the next encounter replays without API calls.

The cap was set at 10 Claude calls per invocation (≈ $0.20 with Sonnet 4.6).

---

## Act 4 — The Loop That Exited Before It Started

**Fourth run.** The guided loop fired, then immediately exited at step 0 without ever
calling Claude:

```
[guidance] reached expected_terminal_state='building' at step 0
```

The early-termination check was: "if `result.state == expected_terminal_state`, return."
The caller passed `expected_terminal_state="building"`. But of course the bot was
**already** in `building` state — recruit dialogs are inside the Inn — so the loop
short-circuited before doing anything useful.

**The fix:** drop `expected_terminal_state` for blocker-resolution use. Let Claude
decide via `action="complete"`; rely on `max_steps` as the budget cap.

A simple bug, but a useful one: it surfaced that "expected terminal state" is the wrong
abstraction for the dialog-completion case. Many dialog flows start *and* end in the
same broad nav state — only the on-screen content changes.

---

## Act 5 — The Hallucinating Local Model

**Fifth run.** The guided loop now fires, but Qwen's L2.5 layer keeps generating
descriptions like *"The game is in the harbor building, and the player is recruiting
crew members"* — while the OCR title plainly says `inn`. The Qwen-injected hallucination
propagates into perceive's `detail` and downstream consumers (the consistency check,
flow detection, learned-recovery matching) all stutter on the conflict.

**Two changes:**

1. **Upgrade Qwen 0.5B → 1.5B.** The 0.5B model wasn't following instructions. Even
   with the building-type and explicit disambiguation rules at the top of the prompt,
   it would confabulate a different building. 1.5B uses ~1 GB RAM, 5–8s per call,
   and follows the prompt structure noticeably better.
2. **OCR-conflict guard.** When `nav_state == "building"` and the OCR-derived nav_detail
   names a specific building from KB, scan Qwen's freeform detail for any *other* known
   building name as a whole word. If found, drop Qwen's detail back to the OCR string
   and downgrade confidence to `low` — which the rest of the pipeline already routes
   to `reclassify_with_claude`. Whole-word regex avoids false positives on substrings.

The lesson here was the same as Act 1, just in a different domain: *OCR is closer
to ground truth; freeform LLM descriptions are interpretive.*

---

## Act 6 — The Harbor Enter/Exit Loop

**Sixth run.** Different bug, same root cause. The bot reached the harbour, but every
tick `_handle_building` saw Qwen reporting `sub_menu='supply'` — because the harbour's
default view shows the Supply tab highlighted alongside the always-visible Depart Now
button — and pressed Back to "return to harbor top level." But the bot was *already*
at top level; Back exited the harbour entirely, the next tick re-entered it, and the
bot got stuck pinging in and out for 12 ticks straight. The fleet readiness check
never fired.

**The architecture lesson, now repeating:** `_ensure_fleet_ready` already calls
`_ensure_harbor_top_level`, which uses the **OCR screen title** (reliable) to back out
of an actual sub-menu when needed. The sail_to handler's pre-emptive Back was both
redundant and harmful — *it trusted Qwen's freeform sub_menu tag over a downstream
function that already used OCR*.

The fix was deletion: drop the pre-emptive Back, let the downstream check do its job.

---

## Act 7 — De-hardcoding

This is when the user surfaced a principle that propagated retroactively:

> Any comparison to understand the game content should not use hard-coded strings.

The grep across the codebase turned up several violations:

- `"harbor" in detail or "harbour" in detail` (the line that triggered the
  enter/exit loop)
- `target not in ("harbor", "harbour")` (harbor panel detection)
- `"harbour" not in bld_title and "harbor" not in bld_title` (depart sanity check)
- `title.lower() == "market"` (market detection in agent.py)
- `KNOWN_BUILDING_TYPES = ["harbor", "harbour", ...]` (port-map canonicalization)
- `KNOWN_BUILDINGS = ["harbor", "market", "shipyard", "inn"]` (a dead stub)

A new helper `ControlKB.detail_mentions_building(detail, building)` consults the
existing `building_name_variants` mapping. The hardcoded list in `port_map.py` was
replaced with a runtime table built from KB; five missing entries (sanctuary,
fortune_teller, blacksmith, estate, port_authority) were added to `ui_signals.json`
to maintain coverage. The dead stub was deleted.

This pass also surfaced **two domain-modelling insights** worth preserving:

- **Estates are a family.** Mercator Estate, Da Vinci Estate, etc. — same building
  type, different proper-noun names per port. Needs `name_pattern` matching, not
  a flat variant list.
- **Religious buildings are cultural variants of one type, but with different
  effects.** Cathedral (European) boosts all 3 skills; Sanctuary (Islamic) boosts
  combat only; Temple (East Asian) varies per port. Same family, different records,
  shared interaction shape.

Neither was implemented in this run — both are queued behind the immediate fix.

---

## Act 8 — Success, with a Twist

**The next run worked.** The bot reached the harbour, hit "not enough crew," exited to
overworld, navigated to the Inn, tapped the Recruit Crew menu item correctly, the
recruit panel opened with the gold Recruit button, the post-step adaptation block
detected the stray yellow primary button, and the Claude-guided loop fired:

```
step 1: tap Recruit (gold, right_panel) → confirmation dialog
step 2: tap OK (yellow, bottom)         → 953 crew hired
```

The blocker was **resolved** at step 2. But the loop didn't know that. The recruit
screen was still visible. The gold Recruit button was still visible. Claude looked
at the screen and reasonably decided "the goal said recruit crew; there's still a
Recruit button; let me keep going."

```
step 3-4: dismiss innkeeper chatter ("Have you tried our roast chicken?")
step 5:   tap Recruit Crew menu in left panel
step 6:   tap Max
step 7:   tap Recruit again to over-hire
step 8:   confirm over-hire
step 9-10: more innkeeper chatter
```

Loop hit `max_steps=10`. **All 10 steps got persisted as a flow** named
`learned_building__building__20260430T154211Z`, with 10 entries appended to
`flow_detection_order`. The bot then exited the Inn, returned to the harbour,
the readiness check passed, and the fleet departed.

**End result: blocker solved, fleet at sea — but a wasteful 10-step recipe is now
sitting in `flows.json` ready to misfire on the next run.**

---

## What's Left to Address

Three things in increasing scope.

### Immediate (must fix before next live run)

1. **Purge the bad saved flow.** `learned_building__building__20260430T154211Z`
   should come out of `flows.json` and its 10 entries should come out of
   `flow_detection_order` in `ui_signals.json`. Otherwise the next "not enough
   crew" event risks replaying the wasteful 10-step recipe (over-recruit,
   chatter dismissal) before the guided loop even gets a chance.

2. **Refuse to persist flows that hit `max_steps`.** Clean termination requires
   Claude saying `action: "complete"`. Anything else (max_steps, parse error,
   no API key) means the recipe is suspect. `save_learned_flow` should check
   `any(s.action == "complete" for s in history)` before writing.

3. **Tighter prompt for blocker-resolution goals.** Tell Claude: "When the
   user goal is 'resolve a blocking condition', the dialog is pre-filled with
   the **minimum** needed. Confirm once and stop. Don't max out the recruit;
   don't repeat after success."

### Soon (deferred but cheap)

4. **Innkeeper chatter as an interruptor type.** "Here's all the mates we got",
   "Have you tried our roast chicken?" — these are auto-dismiss-on-tap dialogs.
   They shouldn't consume guided-loop steps; the perceive() interruptor pass
   should handle them.

5. **Red-dot detection.** The user pointed out that the game uses a red dot on
   menu items to indicate "needs attention." A red dot on the Recruit Crew
   menu means "you don't have enough crew." Absence of the red dot is exactly
   the "we're done" signal the guided loop was missing in Act 8. This is a
   small-icon-presence detector.

### Architectural (the bigger question this journey surfaced)

The successful run produced a **viable but non-optimal** solution. Today the
bot cannot represent that distinction. A learned flow lives next to a hand-authored
flow in `flows.json`, indistinguishable. There's no provenance, no confidence,
no success/failure history, no notion of supersession.

The discussion at the end of this journey landed on five additions that would
turn the existing FSM into a learning-and-improving system:

1. **Provenance/quality metadata** on every learned artifact — `provenance`
   (`hand_authored | claude_guided | human_taught | distilled`), `confidence`
   (`high | medium | low | unverified`), `success_count`, `failure_count`,
   `first_learned_at`, `last_used_at`, `supersedes`.

2. **A learning ledger** — `memory/knowledge/learning_log.jsonl`, one line
   per learned-flow invocation: `(flow_id, started_at, ended_at,
   terminated_via, outcome, observed_state_after)`. The audit trail that
   drives everything else. Same shape as the existing voyage_log.

3. **Quality-aware flow selection.** Sort matching flows by confidence
   (hand_authored > high > medium > low > unverified). Refuse to start a
   `confidence: low` flow from an unprompted detection — only run it when
   the resolution explicitly invokes it.

4. **Failure-driven refinement.** When a learned flow's success rate drops,
   demote its confidence and re-trigger `claude_guided_loop` with a tighter
   prompt: "this flow exists but failed N times — produce a better one."
   The new flow `supersedes` the old.

5. **Domain unification.** The same provenance/quality/ledger shape applies
   to sailing routes ("Azores → London via Lisboa, 3 successful runs, may
   have shorter alternative") and trade strategies ("Diu → London ginger,
   7 successes, average +220k, last failed because target market saturated").
   All "learned skills" live under one schema.

The current brain — perception, FSM, goals, recovery, claude_guidance — is
the right **working** layer. What's missing is a thin **learning layer** above
it. The five additions are incremental, not a rewrite.

---

## Lessons That Stuck

A few principles emerged from this journey that should outlive any specific
fix:

1. **OCR titles are ground truth; LLM freeform descriptions are interpretive.**
   When they disagree, the OCR wins. This rule got broken three separate times
   in different files — every time the failure mode looked different on the
   surface, but the root cause was the same. There is now a guard in
   `qwen_perception.py` that enforces it for one common case; the principle
   should propagate further.

2. **"Look and decide if things have changed" beats fixed cooldowns.**
   The smart re-tap mechanism is more code than a constant, but it's robust
   to walking time, NPC noise, and overlay popups in a way no fixed delay can be.

3. **Don't hardcode game content in code paths.** Building names, dialog
   keywords, screen titles, sub-menu labels — anything that names a thing
   in the game — belongs in KB, not in `if x == "harbor"` branches. The KB
   is the single source of truth and a config-only change should be enough
   to support a new spelling, plural, language, or variant.

4. **Universal UI conventions deserve universal defaults.** The "menu list
   on the left under the title" rule held across every building the bot has
   seen; it's now a code-level default, not a per-building KB constraint.
   Per-building override is still possible but rarely needed.

5. **Claude guidance is a recovery mechanism, not a replacement for the FSM.**
   When the FSM knows what to do, it should run; Claude is for the unknown.
   The guided loop persists discoveries back into the FSM so the boundary
   between "known" and "unknown" shrinks with each unfamiliar screen.

6. **A working solution is not a permanent solution.** The recruit-crew run
   succeeded but produced a recipe with five wasted steps. The architecture
   should be able to capture that distinction — "this works, but it could
   be better" — and revisit it when there's better information. That capability
   is the next thing to build.

---

*Uncharted Waters Origin's title turned out to be unusually apt for the project
itself. The hand-authored KB is the chartered map; everything beyond it is
where the bot has to navigate by inference and patient correction. The bot
that came out of this journey now carries a logbook, knows when to ask the
captain (Claude), and is starting to learn that one path through unfamiliar
seas is rarely the best one.*
