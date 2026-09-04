# Dialogs are windows, not screens

**Status:** DESIGN, 2026-09-03. Motivated by the San Village barter wedge.

## The game is doing what Android does

This is not an analogy. The game reproduces Android's dialog mechanics closely enough that
we reverse-engineered one of its window flags without knowing it:

| Android | UWO, as measured |
|---|---|
| `Dialog` is a **separate window** above the Activity's, not in its view hierarchy | the card `DialogModel` segments off the brown title bar |
| that window sets `FLAG_DIM_BEHIND` with a `dimAmount` | a flat **×1.98** scrim over everything behind — `dimAmount ≈ 0.5` |
| the Activity is **not paused**; it keeps its state and loses **window focus** (`onWindowFocusChanged(false)`) | `active_submenu()` still reads `'Barter'`, truthfully, while nothing on it is reachable |
| input goes to the **topmost focusable window** | a tap aimed at the covered screen does nothing |
| dismissal is a **button the dialog registered** | `DialogModel.actions` — though WHICH one we press parts company with Android; see below |

The scrim stacks multiplicatively, so depth is readable: an undimmed white glyph saturates at
255, one dialog over it reads ~128, two ~64, three ~32.

## What that buys us

Two questions that the codebase had collapsed into one:

* **Which screen is this?** — `active_submenu()`. A dialog does **not** change the answer.
  Barter really is the open sub-menu. This is a reading and it stays true.
* **Can I act on it?** — `chrome_is_dimmed()`. This is window focus, and it is the question
  every precondition was actually asking.

Collapsing them is what wedged the San Village mission: `_open_barter_panel` asked
`on_submenu("barter")`, got a true answer to the wrong question, and returned "already there,
do not tap again" — without tapping, without logging — on every tick for two minutes.

## Where the Android analogy stops

Android's Back fires `onCancel`, and the system never presses your positive button. It is
tempting to copy that, and this design did for about an hour. It is wrong here, and the
correction matters more than the analogy did.

**That rule protects a HUMAN's intent from the framework. The bot IS the user.** There is no
intent to usurp, and cancelling is not neutral — it is refusing to play. The San Village frame
makes the case concretely:

* **Cancel loops.** It returns to `Insufficient Empty Space`, whose `Receive` cannot succeed
  with a full hold, which raises the same `Notice`. A village has no market, so no branch
  frees space.
* **The goods are lost either way.** *"Unclaimed trade goods will be discarded"* is what OK
  **acknowledges**, not what it causes.
* **The run had already made that trade three times** in the same barter — ~2,000 units
  discarded to land 4,279. The fourth OK was not a dangerous new decision; it simply did not
  register.

CLAUDE.md already said this from the other end: *"Back / Home = Cancel, not progress."*

## The default was already written down, in one place

`brain.game_rules.answer_dialog` — named rules first, then **take the positive option**
(user, 2026-08-23: *"the default is OK unless it is spending red gem"*), and a refusal for
anything spending **red gems**, which are real money.

Its docstring carries the constraint this design must respect:

> The default lives HERE, in the layer that knows the game, and nowhere else. […] the moment
> IT defaults, this layer is decorative and the decision has silently moved back down to UI
> mechanics.

So the dispatcher **asks**; it does not decide. A `safe_exit()` on `DialogModel` was exactly
the second decider that warning describes, and has been removed rather than left lying about.

## The design

Guiding principle #1 applied to dialogs — *centralize the observation and the routing;
localize the interpretation.*

1. **The dispatcher observes, once.** `DialogModel` on the tick's frame. A dialog means the
   world has lost window focus.
2. **It does not dispatch the normal work order.** Work aimed at a screen that cannot receive
   input is work that cannot happen — this is "check before acting, not after".
3. **It hands the dialog down** to the activity that owns the screen, via `on_dialog(dialog,
   goal)`. Only the activity knows what the buttons *mean*: the village knows
   `Complete the trade?` is the claim step of the barter it just ran, and knows the hold is
   full. The dispatcher knows none of that and must not guess.
4. **Unhandled goes to `game_rules.answer_dialog`** — which answers with the positive option
   unless a named rule or the red-gem refusal says otherwise. Bounded: a dialog surviving the
   budget is `BLOCKED`, a fact to report rather than something to grind at.

Verification comes from the loop, not from a sub-loop: if the dialog is still there next tick,
the counter advances. Nothing re-taps in place waiting for its own effect.

## Why not by wording

The previous mechanism was keyword matching against learned interruptor entries, and it fails
in three independent ways that the window model does not have:

* **It misses by vocabulary.** The village activity has an `OVERFLOW_PROMPT` context whose
  trigger words are `("overflow", "exceeds", "cargo is full")`. The dialog says
  *"Insufficient Empty Space"*. Same meaning, no match.
* **Entries go stale against a narrowed scope.** Both entries learned live on 2026-09-03 key
  on `"Insufficient Empty Space"` — which belongs to the dialog *behind* the front one. Once
  `DialogModel` correctly scoped the OCR to the front card, `all(kw in zone_text)` could no
  longer match either. Fixing detection broke the dismissal that depended on the old, wrong
  extent.
* **It cannot cover what nobody has met.** A dialog is blocking whether or not we have a
  keyword for it.

Dimming is none of these. It is a property of the compositing, not of the text.

## What this does not solve

* **The barter executor's own OK path** (`barter_commit_verified` → `commit_via_positive_taps`
  / `_confirm_result_dialog`) taps the gold button itself and verifies only that amity or
  cargo moved, never that the dialog closed. It is outside this design and still unverified —
  that is how the fourth OK tap was swallowed with nobody noticing.
* **`dismiss_action()`** on `DialogModel` (`tap_close` / `tap_ok` / `tap_claim` /
  `caller_decides`) encodes a similar split and remains dead code. It is a THIRD opinion about
  how to answer a dialog, alongside `game_rules.answer_dialog` and the interruptor entries'
  stored `dismissal`; consolidating those is worth doing and is not done here.
