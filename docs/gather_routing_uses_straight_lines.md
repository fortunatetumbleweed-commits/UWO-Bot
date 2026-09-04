# The gather route is planned on straight lines

**Status:** known defect, found live 2026-08-26. Not yet fixed.
**Found by:** the user, watching the bot sail Amsterdam → Barcelona for Matchlock Gun —
"it works but not the optimal route for sure, Seville is closer, but was not picked."

## What happened

Planning a Birch Tree barter at Svear Village from Amsterdam, the solver produced:

    gather:Amsterdam -> gather:Barcelona -> gather:Tripoli -> ... -> Svear Village -> Lisboa

Both Barcelona and Seville sell Matchlock Gun, and both have coordinates, so Seville was
considered and lost on distance:

| leg | straight line |
|---|---|
| Amsterdam → Barcelona | 470.7 |
| Amsterdam → Seville | 713.8 |

## Why it is wrong

`brain/gathering_solver._dist` is `math.hypot` over world-map coordinates, and the straight
line from Amsterdam to Barcelona crosses **Iberia and southern France**. The voyage does not:
it rounds Iberia and passes through the Strait of Gibraltar. Seville is on the Atlantic side
and needs no such detour.

The error is not random. Euclidean distance **systematically** favours Mediterranean ports
over Atlantic ones from any northern-European start, because that is exactly where the land
lies between the two points.

## The second defect, which compounds it

    score = len(new) / (d + 1.0)

There is no term for where the mission is ultimately going. The route ends at Svear Village
in Scandinavia, so the plan descends into the Mediterranean for Barcelona AND Tripoli and
then sails back north past its own starting point. Even with exact distances, greedy-nearest
would still produce a detour of this shape.

Note the module docstring credits the solver with an "exit-direction gradient" that beat an
LLM by ~20%. Re-read against the code, that gradient is just the marginal-distance
denominator — there is no destination term. The claim reads stronger than what is implemented.

## The third gap

`catalogue_coords()` returns `None` for `Svear Village` — village positions are
`lat: null, lon: null` in `memory/knowledge/barter/villages.json`. So a destination bias
currently has nothing to aim at.

## The oracle we already have

The game publishes true sailing distance. `vision/destinations_panel.py` parses
`Approx. NN.Nkm` for each row of the nearby-destinations panel, and that list is ordered by
real proximity — the top entry is always the closest (user, 2026-08-24). Today those numbers
are read and thrown away: `tools/sail_capture.py` is the only consumer, and nothing persists
them.

That makes the fix incremental rather than a modelling problem. Learn distances as the bot
sails, store them, and prefer a learned real distance over the straight-line estimate —
"learn once, reuse forever", as with every other KB in this project.

## Options, in increasing order of work

1. **Destination bias** — add a pull toward the mission's end point. Cheap, and fixes the
   Mediterranean-and-back shape. Blocked on village coordinates (the third gap).
2. **Learned distance KB** — persist `Approx. NN.Nkm` per port pair from the destinations
   panel; use it when known, fall back to Euclidean. Self-improving, no modelling.
3. **Land-aware distance** — a sea-graph or coarse land mask over the world map. Correct for
   pairs never sailed, and much the largest change.

(2) subsumes (1) for any pair the bot has actually seen, and (1) is the only one that helps
on the very first voyage to a new region.
