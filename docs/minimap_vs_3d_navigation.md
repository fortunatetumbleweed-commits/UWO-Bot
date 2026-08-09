# Navigation: mini-map vs 3D full-frame — reactive vs memory, and the speed question

A design note on *why the mini-map stays* even if we add 3D-world shoreline
navigation, and on the (non-)cost of processing the full frame. Written
2026-08-02 from a design discussion.

## The idea being weighed
Navigate by recognising the shoreline in the **3D sea world** (full-screen
capture) instead of the top-down **mini-map**, with a simpler reactive rule:
*stay in water, keep the shoreline on the hugging side* — wall-following, which
handles forks/bends/dead-ends **emergently** (no need to model a Y-fork).

The rule is sound — and it's essentially what the current tactical layer already
does, just in mini-map space (it traces the hugging-side bank to the frame exit;
the Y-fork behaviour is emergent from bank-following, not modelled). So the real
question is only *which perception substrate* runs the rule.

## Two signal types — and a bounce splits them
- **3D world view = egocentric, reactive.** Rich and high-res when the scene is
  clear. But it is relative to the camera, and on a **bounce** the ship recoils
  and the camera swings, so "which way was I heading?" becomes ambiguous — even a
  human loses the thread.
- **Mini-map = allocentric (world-frame) reference + short-term memory.** The
  ship sprite's *orientation* gives absolute heading regardless of recoil, and
  the accumulated track says where the ship came from (which way is back to open
  water vs into the land just hit).

A bounce is a **sensor dropout on the reactive channel**; the correct response is
to fall back on the stable reference / memory. This is already implemented:
- bounce is **detected by speed** (≥70% tick-to-tick speed drop = collision;
  hard rudder alone never drops speed);
- during a bounce, **motion bearing is corrupted** (recoil), so heading authority
  passes to the **CNN read of the mini-map ship sprite** (the stable reference);
- dead-reckoned position + commit persistence carry continuity for the few
  disoriented ticks until the reactive signal recovers.

This is the same **arbitration** principle as the perception redesign: never
trust one signal unconditionally — when the reactive channel's confidence
collapses, hand authority to the stable one. The bounce is the most vivid case.

## Is the full frame too slow to process? No.
The "3D = 34× more pixels" worry (2400×1080 vs ~400×190) does not translate to
34× cost:
1. **Capture is already full-frame.** `adb screencap` grabs the whole screen
   every tick; the mini-map is a *crop* of it. No extra capture cost.
2. **Inference cost tracks the model *input* resolution, not the source.** You
   downscale to the segmenter's input (e.g. 384²/512²) — a few-ms resize — so you
   pay for the resolution you choose, not the raw source. The **family CNN
   already ingests the whole frame every tick** (downscaled to 224²) at **~50 ms**
   — full-frame CNN processing is already routine and cheap. A shoreline U-Net
   would be the same order (tens of ms MPS / low-hundreds CPU) vs the mini-map
   heading CNN's ~2–40 ms.
3. **Navigation is not latency-bound.** Sailing ticks are *seconds* — dominated
   by calibrated steering holds + anti-cheat jittered sleeps, not perception.
   A few hundred ms of full-frame inference is invisible. (The ~8 s OmniParser
   cost that *is* a bottleneck is UI/trading button-finding, not nav.)

So speed is **not** the blocker for 3D nav.

## Conclusion — hybrid, don't drop the mini-map
The real constraints on 3D shoreline nav are perception **reliability** (weather /
time-of-day / region variability → needs a learned segmenter, not colour rules)
and the **shore-off-screen** problem (wall-following needs the wall in view; the
mini-map keeps local shore always visible). Compute is not a constraint.

Recommended division of labour if 3D is pursued:
- **3D view** → smooth, high-res in-water hugging when close to shore and the
  scene is clear (also dodges the mini-map's port-icon-drawn-on-water corruption).
- **Mini-map** → heading reference, position memory, and **recovery through
  bounces / disorientation** — the moments the 3D view cannot be trusted.

Treat 3D as a **prototype/measure** step (train a water/shore U-Net across
conditions, replay the pure rule offline, measure segmentation robustness) — not
a rewrite, and never at the expense of the mini-map's reference/memory role.
