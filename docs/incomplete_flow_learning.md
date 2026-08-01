# Incomplete-Flow Self-Correction

## The guideline

A learned flow is **complete** only when it satisfies BOTH:

1. **Recognized terminal state** — the fingerprint registry classifies the post-flow frame
   as a known state (foundational or learned).
2. **At least one positive transaction** — a tap that commits or advances game state
   (*confirm*, *recruit*, *buy*, *sell*, *set sail*, etc.). Opening a sub-menu is navigation,
   not a transaction.

If either condition fails, the flow is **incomplete** and learning must continue.

**Cancel semantics:** leaving the current state via Back arrow or Home icon is `cancelled`.
Cancel does not satisfy either condition — it does not count as a successful terminal step
even if it lands on port_overworld.

This is a **project-wide rule**, not a per-flow concern. Every flow — building, dialog,
sail, combat — is bound by it.

## Why this rule exists

Trigger incident (2026-05-02): the recruit-crew loop.

- Goal: `achieve_has_enough_crew` — visit Inn → tap Recruit Crew menu → tap gold Recruit
  button → confirm.
- What the bot did: tapped Recruit Crew menu, landed in sub_menu, then `sail_to._handle_unknown`
  treated sub_menu as unexpected and recovered to overworld via Home. Loop.
- Root cause: a learned flow `learned_building__building__20260502T173311Z` had been saved
  with only `step_1` (tap menu item). It had no commit step and its terminal state was
  sub_menu — but no one had recorded that sub_menu was an acceptable terminal state for
  this flow either. The flow was half-written and the runtime had no way to know.

A flow that only opens a menu is not a flow. It is a state transition. If we save it as a
flow we will keep "completing" it forever without ever buying any crew.

## Runtime mechanism (proposal — not yet implemented)

### Persisted shape

Every flow gets a `status` field and a positive-transaction count.

```json
{
  "id": "learned_building__inn__recruit_crew",
  "status": "incomplete",          // "complete" | "incomplete" | "cancelled"
  "positive_transaction_count": 0,
  "terminal_state": "sub_menu",
  "terminal_state_recognized": true,
  "steps": [
    { "kind": "tap", "label": "Recruit Crew", "category": "navigation" }
  ],
  "last_attempt_outcome": "cancelled_via_home"
}
```

`status: "complete"` requires both `positive_transaction_count >= 1` and
`terminal_state_recognized == true`.

### Step categorisation

Each step gets one of:

- `navigation` — opens a screen/sub-menu/panel. Does not commit anything.
- `transaction` — commits state. Includes `commit_via_positive_taps`, explicit confirm/buy/
  recruit/sell taps, and `set_sail`.
- `dismissal` — closes a dialog or overlay (does not count toward completeness).
- `cancel` — Back or Home press during the flow. Sets `last_attempt_outcome: "cancelled_*"`
  and prevents the run from being marked complete.

A flow is provisionally complete only if `transaction` step count ≥ 1.

### Detection during a run

The flow runner already logs every step. Add post-step bookkeeping:

1. After each step, classify the resulting frame via the registry.
2. If a step's kind is Back / Home → mark the run `cancelled`, do not advance status.
3. If a step is `commit_via_positive_taps` and the cycle closes → increment
   `positive_transaction_count`.
4. When the flow ends (planner exits, runner reaches end-of-step-list, or runner hits
   recover):
   - If `positive_transaction_count >= 1` AND terminal frame classifies to a registered
     state → save `status: "complete"`.
   - Otherwise → save `status: "incomplete"` and trigger the learning hook on the terminal
     frame.

### Runtime behaviour for incomplete flows

When `perceive` would attach an incomplete flow to a state, it does not. Instead:

- The state is treated as unhandled-by-flow.
- The planner's `achieve_*` route (if one exists) takes precedence.
- The learning hook fires on the current frame to fill the missing tail of the flow.

This severs the current loop where `sail_to._handle_flow` keeps re-running the same
half-written flow forever.

### Authoring-time validation

`vision/claude_vision._maybe_save_learned_candidate` and any future flow-authoring path
validates against the rule before persisting:

```python
def is_flow_complete(flow) -> bool:
    has_transaction = any(s.category == "transaction" for s in flow.steps)
    terminal_recognized = bool(flow.terminal_state and flow.terminal_state_recognized)
    return has_transaction and terminal_recognized
```

Flows that fail this check are still persisted (the partial learning is valuable) — but
with `status: "incomplete"` so the runtime will continue learning rather than treating
them as ground truth.

## Implementation order (for review before coding)

1. **Schema migration** — add `status`, `positive_transaction_count`, `terminal_state`,
   `terminal_state_recognized`, and per-step `category` to `flows.json`. Default existing
   flows to `status: "incomplete"` until they're audited.
2. **Categoriser** — small helper that maps a step's `kind` + `method` to navigation /
   transaction / dismissal / cancel.
3. **Audit pass** — script that walks current `flows.json` and prints incomplete entries.
   Run once; expect `learned_building__building__20260502T173311Z` to surface.
4. **Runner bookkeeping** — flow runner increments transaction count and writes status on
   exit.
5. **Perceive guard** — `perceive` skips attachment of incomplete flows; planner takes over.
6. **Learning hook re-trigger** — on incomplete-flow exit, fire `analyse_scene` on the
   terminal frame, scoped to "extend flow X with the next step from this state".
7. **Authoring validation** — gate new learned flows through `is_flow_complete()`.

Steps 1–3 are non-invasive (read + tag). Steps 4–7 change runtime behaviour and should be
reviewed and tested individually.

## Open questions

- **Multi-attempt flows.** If the flow's commit step requires retrying (e.g. recruit
  succeeds only when crew slots are open), is the absence of a commit on a single attempt
  always a sign of incompleteness? Probably yes for authoring; for runtime it should be
  tracked as a separate "attempt failed" outcome.
- **Cancel-as-design.** Some flows legitimately end in Back (e.g. "view today's news" — no
  commit needed). These would fail condition (2). Either we add a flow type
  `informational` that is allowed to terminate without a transaction, or we admit those
  aren't flows at all and move them into the state graph.
- **Demoting existing incomplete flows.** Should the audit pass automatically demote
  `learned_building__building__20260502T173311Z` to `status: "incomplete"` and stop
  attaching it, or only flag it for human review?
