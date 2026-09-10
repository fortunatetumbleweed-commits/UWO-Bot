# Multi-stage barter — planning a chain (DESIGN, 2026-09-10)

**Status: not decided, nothing built.** This records the problem, what the KB can and cannot
answer today, and the shape of the planner. Measured against the live KB on 2026-09-10.

One command should run a whole chain — `barter Eagle Feather`, `barter Moccasin` — the way
`barter Bambara Groundnut at Hutu Village` runs a single stage today.

---

## 1. Why the current planner cannot

`brain/barter_quantity.plan_barter_rounds(output_per_round, needs, rounds_remaining,
free_space, ...)` sizes **one good at one village**. Every one of its inputs is a given:

- `rounds_remaining` is *handed to it* — the Base tab's `barters_total − barters_used`.
- `needs` is one recipe's per-round materials, all assumed **bought at ports**.
- the village is chosen by the command, not by the plan.

So the planner optimises against **hold space and materials**. In a chain neither is the
binding constraint — **rounds are** — and rounds are the one input it treats as fixed.

### The round budget belongs to the village, not the good

The daily allowance is per village and shared by everything it trades (`barter_rounds_total`:
7 at San, Hutu, Svear and Apache; 6 at Melanesian; 5 at Berber). Two barters at one village
compete for one pool. That single fact drives the whole design — see the memory
`a-villages-rounds-are-one-budget`.

A round spent on the final good is worth much more than one spent on its intermediate, so:

> **Spend a village's rounds on the best good it can make, and get the intermediate
> somewhere else** — even when that village could make both.

Live precedent (user, last season): both Hutu and San offered a seasonal good taking Bambara
Groundnut, and both could make Groundnut. The play was to barter Groundnut at **San**, sail to
**Hutu**, and spend all seven of Hutu's rounds on the seasonal good.

### Two topologies

- **Same village** — Svear trades Birch Tree *and* Näverslöjd (`Näverslöjd ← Birch Tree 252 +
  Iron 126`). The rounds must be split; there is nowhere else to go.
- **Across villages** — `Eagle Feather ← Pulque + Guarana`, neither made at Cheyenne.
  Routing is the lever, and geography matters: of the three villages trading American Bison,
  the one that trades *only* Bison sits between the two that trade Moccasin.

---

## 2. The algorithm — plan backwards from the final good

The last stage is exactly today's barter: bounded by hold capacity, and to be maximised.
Everything upstream exists only to feed it.

```
plan(G, V):                       # final good G at village V
    rounds_G   = ceil(capacity / output_per_round(G, V, amity))
    materials  = rounds_G x per_round_inputs(G, V)
    budget     = barter_rounds_total(V) - barters_used(V)
    if rounds_G > budget: rounds_G = budget          # cannot fill the ship today
    spare      = budget - rounds_G

    for M in materials where M is itself a barter good:
        rounds_M = ceil(materials[M] / output_per_round(M, ?, amity))
        if V trades M and rounds_M <= spare:
            make M at V;  spare -= rounds_M          # same-village stage
        else:
            W = a village trading M, chosen for detour cost   # cross-village stage
            recurse plan(M, W) for the quantity needed
```

Two decisions fall out, and the bot makes neither today:

- **Assignment** — not *"where is this good made"* (several places may make it) but *"where is
  it cheapest **in rounds** to make it"*. A village with nothing better to do has cheap rounds.
- **Allocation** — how one village's budget splits when it must make more than one stage.

Note the intermediate stage is **not** capacity-bound: it needs exactly
`rounds_G x ratio` units, not a full hold. Overshooting it costs rounds that the final good
wanted.

### The Plains villages — the topology this design is aimed at

**Read from the game 2026-09-10** (`tools.learn_village_barter` on all three). The user's
account was right about the shape; the read adds the numbers and two corrections.

```
Cheyenne : American Bison, Moccasin, Eagle Feather, Goldenseal   5 rounds, Neutral
Sioux    : American Bison, Moccasin, Eagle Feather, Goldenseal   5 rounds, Neutral
Pawnee   : American Bison, Goldenseal                            5 rounds, Neutral
                                          ^^^ not Bison ALONE — but no Moccasin, no Eagle Feather
```

**Five rounds, not seven, and Neutral amity everywhere.** Moccasin yields 859/round, so
filling a 4,952 hold takes ~6 rounds — *no single village can fill a hold with Moccasin in a
day at current amity*. Amity-building is a precondition of this chain, not a refinement: a
grade adds a barter AND improves the ratio ([[amity-drives-rounds-and-ratio]]).

**The materials differ per village, and they invert the easy answer:**

| American Bison | Horse | Hand Cannon | Bullet | | Moccasin | Bison | Wool |
|---|---|---|---|---|---|---|---|
| Sioux | 126 | 126 | 126 | ← cheapest | Sioux | 251 | 251 |
| Cheyenne | 126 | 146 | 146 | | Cheyenne | 251 | 291 |
| Pawnee | 166 | 186 | 186 | ← ~30% dearer | | | |

So Pawnee has **cheap rounds and the worst ratios**, Sioux **the best ratios and the dearest
rounds** (its five are also wanted by Moccasin and Eagle Feather). Assignment is a real
trade-off, not the one-line answer this doc first gave it.

**And it cannot be settled yet**, which is the schema gap of §3 proved on fresh data:
`American Bison.output_per_round` is `{'Neutral': 923}` — one key for three villages.
Cheyenne's Bison read **859** (frame 00 of the 2026-09-10 sweep); the stored value is 923,
from whichever village was read last. If Pawnee's dearer materials buy a higher yield, routing
through it may win anyway — and the KB cannot represent the question.

Chains now fully sourced by the same read:

```
Moccasin      <- American Bison  [Sioux, Cheyenne, Pawnee] + Wool (ports)
Eagle Feather <- Pulque  [Apache, Chinook] + Guarana [Jívaro, Witoto]
```

Four things follow, and together they are the whole argument for this design:

1. **Pawnee's rounds are cheap.** It has nothing better to make, so a round spent there on
   Bison costs nothing that a round at Cheyenne or Sioux would have earned. That is the
   assignment rule (§1) with an unambiguous answer.
2. **The detour is nearly free**, because Pawnee lies between the two. Geography turns a
   two-village chain into one route rather than a round trip.
3. **Cheyenne and Sioux each face the allocation problem in full** — three goods, one shared
   budget. This is where "which good does this village's allowance buy" cannot be dodged by
   routing.
4. **Eagle Feather is a three-village chain at minimum.** It takes Pulque + Guarana, and
   neither is made where it is traded — Pulque is at `apache_village`; Guarana has no recipe
   at all. So the highest-value good needs the deepest plan, which is the usual shape.

### Worked: Moccasin

`Moccasin ← American Bison 300 + Wool 340` per round, and
`American Bison ← Horse 150 + Hand Cannon 150 + Bullet 150` — both at Cheyenne (7 rounds,
assumed). With capacity 4,952:

| | |
|---|---|
| rounds to fill the ship with Moccasin | `ceil(4952 / output_per_round(Moccasin))` — **unknown, see §3** |
| American Bison needed | `rounds_Moccasin x 300` |
| rounds to make that Bison at Cheyenne | `ceil(bison / output_per_round(Bison))` — **unknown** |
| fits at Cheyenne? | only if the two sums are `<= 7` |

If they do not fit — which the user's experience says is the usual case — Bison is made at one
of the other two villages first, and Cheyenne's whole allowance goes to Moccasin. The middle
village makes that detour cheap.

---

## 3. What the KB can and cannot answer (measured 2026-09-10)

**Has:**

- `BarterRecipe.villages` — `Bambara Groundnut → [San Village, Hutu Village]`,
  `Moccasin / American Bison / Eagle Feather → [Cheyenne Village]`.
- `village_inputs` **per village**, and they differ: San needs Pig 218/round, Hutu 188.
  A chain plan must use the per-village figure, not the recipe's default.
- `output_per_round` keyed by amity grade — `Bambara Groundnut {Neutral: 829, Friendly: 988}`,
  `Birch Tree {Neutral: 448, Friendly: 495}`. Amity is an input to the plan, not a detail.
- `barter_rounds_total` per village.

### The same good is a different trade at each village

Not only the ratios — the **materials themselves** can differ by village (user, 2026-09-10).
`village_inputs` is keyed by village and can carry that. **`output_per_round` cannot**: it is a
flat `{amity_grade: qty}` map on the recipe, shared by every village that trades the good.

Box of Nutmeg is the case to test against:

```
villages:         ['Melanesian Village', 'Malay Village', 'Khmer Village']
village_inputs:   { melanesian_village: [...] }          <- one of the three
output_per_round: { 'Neutral': 591, 'Friendly': 679, 'Trusting': 616 }
```

Three villages, one yield map. Choosing among them means comparing yields, and the yields are
stored so that they cannot be told apart.

Bambara Groundnut is the case where both villages HAVE been read, and it shows assignment is a
real choice with a real answer: San and Hutu both yield 988, but San wants **Pig 218/round**
against Hutu's **188**. Same output, 16% less Pig — Hutu dominates San for this good outright,
and nothing in the bot notices. (Only two recipes in the KB name more than one village today:
these two.)

There is a symptom of that collapse already in the data: **Trusting 616 < Friendly 679**, where
a higher amity grade should improve the ratio (`amity-drives-rounds-and-ratio`). Two
explanations, not yet separated — the grades are not ordered as assumed, or those readings came
from *different villages* and were merged as interchangeable. The second would make the anomaly
a direct consequence of the flat key. **Read the grade order off a village panel before
believing either.**

So `output_per_round` wants the same treatment `village_inputs` already has:
`{village: {amity_grade: qty}}`. Migration must not invent a village for existing entries —
a yield whose village is unknown is unknown, and an unmeasured village counts for nothing (the
same rule the low-stock gate learned).

**Cannot, and these are the real blockers — data, not algorithm:**

1. ~~**`output_per_round` is `{}` for every chained good**~~ — **collected 2026-09-10** for
   Bison, Moccasin, Eagle Feather and Goldenseal by adding the Base tab read to
   `tools.learn_village_barter` (the grade is the key it is stored under, and the tool never
   opened that tab). What remains is that the key is the GRADE ALONE — see "the same good is a
   different trade at each village". Näverslöjd is still unread.
2. ~~**`source_villages` is empty on every chained material**~~ — **fixed 2026-09-10**. The
   learner called a wrapper that kept only `['market']`, so the village was read by the same
   code hardened on the Damascus Steel panel and discarded a line later. One call site.
   Note the panel read is not authoritative on its own: Cheyenne's listed all three Bison
   villages, Sioux's listed two, so sources must UNION rather than replace. The derived join
   (material -> recipe -> `villages`) proved the more reliable of the two and is still worth
   building — it is what repaired the dropped Pawnee entry.
3. **Nothing consumes it for planning.** `mission_runner.py:692` is explicit: the reroute
   builds a `gather` leg that **buys**. A material only a village trades has no path.
4. ~~**The village index is incomplete**~~ — **Cheyenne, Sioux and Pawnee read 2026-09-10**,
   each with amity, rounds and eligible goods. The learner had also been passing a name-only
   `Village` to `save_village`, which REPLACES the record, so learning a village's goods
   erased its amity and rounds; it now merges.
5. **`Guarana` has no recipe at all**, so Eagle Feather is unplannable. `Pulque` is at
   `apache_village`.
6. `waversioja` is an OCR-corrupt duplicate of `Naverslojd` and would be planned as a
   separate good.

---

## 4. Build order

Data first — the algorithm is cheap and cannot be tested without it.

1. **Resolve a material to its producing villages** by joining material name → recipe →
   `villages`, and record it in `source_villages` so the join is not re-derived. Cheap, pure,
   testable offline against the existing KB.
2. **Fill the village index.** Cheyenne and the other Bison/Moccasin villages need a remote
   check each: `output_per_round`, `barter_rounds_total`, `eligible_goods`. This is the
   long pole and it needs sailing.
3. **A round-budget model** — `barters_total − barters_used` per village, spent by the plan
   rather than handed to one call.
4. **Backward planner** over §2, emitting the existing `SubTask` legs. Plan-only first: print
   the assignment and the allocation and let a human check them before anything sails.
5. **Execution** — `mission_progress` phases are per-mission today (`planning / gathering /
   bartering / sailing_route`); a chain needs them per stage, or a stage index alongside.

## 5. Open questions

- **Staleness.** `output_per_round` re-rolls ~6h and amity decays. A chain spans days
  (`barter-chains-are-the-endgame`). What is re-checked between stages, and what is pinned?
  Today the recipe is pinned at mission start precisely to stop re-checks sailing the fleet
  out of a village it had reached.
- **Seasonality.** "Both Hutu and San had a seasonal barter taking Groundnut" is a
  *this-season* fact. Assignment must be re-derived when the window re-rolls, never cached
  as a route.
- **The dump rule inverts mid-chain.** `dump-materials-only-on-the-last-round` and "the
  output is never a dump candidate" contradict each other at Svear, where Birch Tree is both.
  Whether a material may be dumped depends on what the *rest of the plan* still wants — which
  is the same mission-level knowledge assignment needs. The three deferred overflow fixes
  (dump ordering, the three-tile completeness check, the misleading log lines) should land
  after this is decided, not before, or they will encode the single-village assumption.
- **Is a partial chain worth running?** If Cheyenne's rounds cannot fill the hold with
  Moccasin, is a half-load better than a Bison-only trip that sets up a full load tomorrow?
