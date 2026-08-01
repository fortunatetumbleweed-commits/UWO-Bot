# Flow Completeness & Self-Correction

A project-level guideline.  Mirrors `memory/project_incomplete_flow_learning_guideline.md`
and `memory/project_plan_completeness_guideline.md` (memory is the rule,
this doc is the long-form rationale).

## Flows
Learned flows are not authoritative just because they were saved.  A
flow is **complete** only when it satisfies BOTH conditions:

1. **Terminates at a recognized state** — a state present in the
   fingerprint registry (foundational or learned) when the flow
   finishes.
2. **Contains at least one positive transaction** — a tap that commits
   or advances state (e.g. *confirm*, *recruit*, *buy*, *sell*, *set
   sail*).  Tapping a menu item that merely opens a sub-menu is
   *navigation*, not a transaction.

Anything else is **incomplete**:

- A flow that ends in an unrecognized state → incomplete; re-fire
  learning.
- A flow whose only steps are navigation taps with no commit → incomplete;
  re-fire learning.
- Leaving the current state via **Back arrow or Home icon counts as
  Cancel**, not as flow progress.  Cancel does not satisfy condition
  (1) or (2), even if it returns the bot to `port_overworld`.

**Runtime behaviour:**
- When a flow is authored (by hand or by the learning hook), validate
  against this rule before persisting as complete; otherwise persist
  with `status: "incomplete"`.
- When the runtime encounters an incomplete flow, do not treat it as
  authoritative — fall back to the learning hook and continue
  exploration until the rule is met.
- Back / Home dismissals during a flow attempt are recorded as
  `cancelled`, never as a successful terminal step.

**Scope:** applies to every flow — building, dialog, sail, combat.
There are no exceptions for "navigation-only" flows.  Pure navigation
belongs in the state graph (transitions between states), not in the
flow registry (committed transactions).

**Origin:** the recruit-crew loop (2026-05-02) saved a one-step flow
that only tapped the Recruit Crew menu item; the bot then looped
between opening the sub-menu and recovering to overworld because the
flow lacked a commit step.  A flow that only opens a menu is half a
flow, and the runtime must know it.

## Claude-generated plans are recommendations, not truth

Escalation plans returned by Claude Vision (e.g.
`_resolve_blocker_with_reasoning`, the `escalate()` teaching loop) are
**recommendations** — they are bound by the same completeness rule as
learned flows.  A plan is valid only when its execution produces at
least one positive transaction.

When a plan step fails to find its labelled button (e.g.
*"Button 'Confirm' not found — skipping"*) the runtime must NOT
silently skip and continue.  The skip implies "this step did nothing,"
which violates the rule.  Instead:

1. **Refine via positive-button fallback** — call
   `commit_via_positive_taps` on the current frame.  If a gold /
   positive button is visible, tap it; the cycle-closing primitive
   handles confirm / result follow-ups.  This is the cheap,
   deterministic refinement.
2. **Revamp via Claude** — if no positive button is found, send the
   current plan, the failed step id, and a screenshot back to Claude
   with the request: *"this step couldn't find its target — propose a
   revised plan from the actual screen state."*  One round of revision
   per plan to avoid loops; if the revision also fails, escalate.

If neither refinement nor revision produces a positive transaction,
the plan is invalid: do not persist it as a learned recovery, do not
increment its success counter, do not promote its confidence.  Caching
a transaction-less plan reproduces the recruit-crew failure mode at a
different layer.

**Origin:** the harbour Recruit-Crew failures (2026-05-04).  Claude
generated plans like `[tap Recruit, tap "Depart Now"]` and
`[tap Recruit, tap "Confirm"]` on consecutive runs.  The "Recruit" tap
landed correctly; the second-step buttons did not exist on the post-tap
screen.  The runner logged *"Button … not found — skipping"* and
returned without recruiting anything, yet the plan was saved as a
learned recovery.  Each subsequent encounter re-fired the broken plan.
