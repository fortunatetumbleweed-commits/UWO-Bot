# The stage suite

Real frames from real runs, turned into a check you can run **before** a live run.

Every run leaves a trace under `data/sessions/trace_*/` — thousands of frames plus an
`actions.jsonl` of what was tapped. Those are working artifacts: they get pruned, renamed and
regenerated. **This folder owns copies**, so the suite cannot quietly lose its evidence and
start reporting green on nothing.

```
stages.json          the catalogue: stage -> frame, the reading, what the stage IS, where it came from
frames/<stage>.png   the frame itself, copied
perceived/<stage>.json   what perception read when the stage was recorded
```

## Running it

```bash
pytest tests/test_stages.py                     # every stage — for a refactor
pytest tests/test_stages.py -k market           # just the market stages
pytest tests/test_stages.py -k barter_panel     # one stage
pytest tests/test_stages.py --stage-perception  # ALSO re-perceive every frame (slow)
```

## Two layers, and why

**The decision layer** (always runs, milliseconds). Given the state a frame is known to show,
what does the activity decide? This is possible because an activity is now a function of
`(goal, state)` — the dispatcher refactor is what made a frame answerable without executing a
single tap.

**The perception layer** (`--stage-perception`, ~10s a frame). Does perception still read this
frame the way it did when the stage was recorded? Run it after touching `perceive`, the
fingerprints, or the family classifier.

They are split because `docs/simulation_tests.md` is the post-mortem of not splitting them: a
simulated test that drove the real action code took **1h05m** and its assertion could not fail
on its merits. The rule it ends on is the one this suite follows — *test the decision, not the
voyage*.

Nothing here touches the device. `brain.replay.no_device` makes every route to the phone
raise, because the difference between a simulation and a live run is exactly that.

## Adding a stage

```bash
python -m tools.add_stage <name> data/sessions/trace_.../frame_0060.png "what should happen here"
```

That copies the frame in, perceives it once, and records the reading. Then write the decision
test in `tests/test_stages.py` — the catalogue on its own asserts only that the stage exists.

Name stages so `-k` selects usefully: a shared prefix groups them (`market_*`, `barter_panel_*`,
`harbor_*`), which is what makes "run only the stages I affected" work.

## What a stage cannot tell you

One frame is one tick. The suite checks what the bot decides **at** a stage, not that a
sequence of stages flows — the next frame in a trace is what the OLD code did, not what the
new code would do. Flows are still a live-run question; stages are what can be settled first.
