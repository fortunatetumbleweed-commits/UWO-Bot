# Dialogs understood, not matched — a design note

**Status: PROPOSED, not decided (2026-09-12).** Written the day a hardcoded button word ended
a mission. Recorded so the next person starts from the evidence rather than the symptom.

## The failure that prompted it

Hutu Village, 2026-09-12. Three barter rounds in, the hold at 4,952/4,952, and the game raised
its overflow card:

```
Insufficient Empty Space
Cannot receive item due to insufficient space. Please organize your Cargo Hold.
Unreceived trade goods will be discarded.
  Received Trade Goods :  147
  Cargo                :  4,952/4,952 (100%)
                     [ Receive ]
```

`detect_dialog` found the card correctly at (543, 110, 1857, 972). Then `DialogModel.kind()`
classified it:

```python
if action_labels & {"claim", "collect", "receive", "continue"}:
    return "reward"
```

One button word, and the card became a **reward**. `game_rules` then declined to answer a
reward whose only option was `Receive` — correctly, because blindly receiving is what
DISCARDS the overflow — and the mission failed with *"a reward dialog nobody will answer"*.

**Nothing looked at the title or the body.** Both say plainly what this is, and one of them
even says what accepting costs.

## Why it had never bitten before

`kind()` is only consulted when NOBODY owns the dialog. At San Village on 2026-09-10 the same
card appeared and the run handled it perfectly:

```
[classify] → village (left-menu vocab match ['barter','explore','gifting','loot','recruit crew'])
[village] the hold is full and the card is showing it — freeing space before receiving
[village] OVERFLOW — 47 unit(s) pending
[overflow] pending 47 → plan [('Water', 3), ('Food', 3)]
```

`VillageActivity` owned it and used its OWN classifier — `is_overflow_card`, which reads the
card's STRUCTURE, a `Received Trade Goods` strip above a `Cargo` strip. `kind()` was never
asked. It was very probably saying `reward` that day too.

So the label has been wrong for as long as it has existed, and it only became fatal the first
time the activity failed to resolve — which happened because the family CNN read the same card
as `transient@1.00` instead of `chromed@1.00`, and the state came out `unknown`. Two
independent weaknesses had to line up, which is why this looked like a regression and was not.

## What is actually wrong with the rule

**A button is the weakest evidence a card carries, and `Receive` is overloaded.** It claims a
prize on a reward card and accepts goods on an overflow card, where accepting is the
destructive branch. The same word, opposite consequences.

**It is the mistake the rest of the codebase has already moved away from.** `village_context`
carries the scar: `OVERFLOW_PROMPT` used to key on "overflow" / "exceeds" / "cargo is full",
three phrases the game has never drawn, so its handler was unreachable and every overflow met
was discarded in silence — 360 units at San on 2026-09-05. The market and village contexts now
classify by structure and position, never by wording. `DialogModel.kind()` never got the memo.

**And the detection is not the problem.** CLAUDE.md holds `DialogModel` up as the precedent to
trust because it "is not a model — it is classical numpy structure". That is true of finding
the card. What sits on top of it is a word list.

## The proposal: ask, with the whole situation

Give the model everything a person would need to answer, and let it decide:

| field | example from the failure above |
|---|---|
| title | `Insufficient Empty Space` |
| body | `Cannot receive item due to insufficient space… Unreceived trade goods will be discarded.` |
| structured strips | `Received Trade Goods: 147`, `Cargo: 4,952/4,952 (100%)` |
| buttons | `[Receive]` |
| what I was doing | committed barter round 4 of 7 at Hutu Village |
| my goal | `Barter(good='Bambara Groundnut', village='Hutu Village')` |
| the guideline | materials a further round can still use are protected; dump supplies first; never spend a red gem |

The answer wanted is not a category. It is **what this card means for what I am doing** —
here, *"accepting now discards 147 units; free space first"*, which is exactly what the
village handler concluded structurally.

## The precedent already in the codebase

`vision/obstruction_consult.py` does this shape today, and it works:

```
[obstruction_consult] consulting Claude: kind='popup' hash=95f5eb59 goal='Barter' bbox=(1011,106,1855,649)
[obstruction_consult] saved analysis: purpose='Trade result summary showing the completed…'
[perceive] the consult says this is about the goal — leaving it to the activity rather than tapping
```

Note what it already gets right, and what this proposal would inherit:

- **The GOAL is part of the question.** The same card means different things mid-barter and
  mid-sale, and the consult is keyed on it.
- **It answers a USEFUL question**, "is this about the goal or in the way of it", rather than
  naming a category.
- **It is cached learn-once**, by `(kind, perceptual hash, goal)`. The Svear run paid for 48
  consults and later runs pay nothing for those cards. The cost falls to zero as the catalogue
  fills, which is the Data Flywheel this project already runs on.
- **It is advisory, not authoritative** — the activity still decides.

## What this must NOT become

**Not a replacement for the structural handlers.** `is_overflow_card` is free, deterministic
and right; the consult is for the card nobody has taught the bot yet. Structure first, ask when
structure has no answer.

**Not a blocking call on the tick path.** The consult above costs ~2.9s and ran 77 times in one
run. It belongs where `kind()` is today — the fallback when no activity owns the card — not on
every frame.

**Not a licence to tap.** A learned answer must still pass `brain.game_rules`: never a red
gem, never a destructive branch without the owning activity's say-so. The model says what the
card MEANS; the rules say what may be pressed.

**Not self-certifying.** A learned classification that contradicts a structural detector is a
reason to look, not to overrule it. The structural detector is checkable by a fresh frame.

## The cheap fix meanwhile

Independent of any of this, `kind()` should ask the structural detectors it already shares a
codebase with before falling back to button words — `is_overflow_card` and `is_discard_notice`
both answer this card correctly and cost nothing. That is a small change and does not wait on
the design above.

## Open questions

- Where does the learned answer live: alongside the obstruction analyses keyed by perceptual
  hash, or in the control KB as a named card with a handler?
- What does the model return — a kind, a recommended action, or a statement of consequence
  ("accepting discards 147 units")? The last is the most useful and the hardest to verify.
- How is a learned entry retired when the game rewords a card? The obstruction cache went stale
  once already when detection narrowed the OCR to the front card.
- Does this subsume `_POSITIVE_WORDS` in `brain/game_rules.py`, which is the same kind of list
  one layer down and has the same failure mode?
