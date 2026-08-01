# YOLO Phase 4b — Labeling Plan

Status: ready to label, 2026-05-22.

## Goal

Bootstrap the navigation-class training set described in
`docs/yolo_navigation_classifier.md` Phase 4b — the proximity events
the bot can't currently see (Quick Supply prompts, port/village
nameplates floating above the horizon, landmarks, enemy fleets, etc.).

The frames are mostly already on disk under `data/sessions/`.  The
labeling work is **adding notes/tags** to the existing labeled frames
to mark which Phase 4b classes appear in each one.  No new screen
captures needed for the initial bootstrap.  Once enough frames carry
the tags, a Claude-Vision pass extracts pixel-precise bounding boxes
and writes YOLO-format `.txt` files.  Bbox extraction is *not* manual
work — only the per-class presence tags are.

## What we're labeling

Eight navigation classes from the design doc, plus a couple of
companion classes that the analysis of recent failure logs surfaced as
"would have saved time."  The labeler tag prefix is `nav:` so these
don't collide with the existing `weather:` and screen-type tags.

| Tag | What it is | Where it appears | Visual cue |
|---|---|---|---|
| `nav:port_nameplate` | Port name banner floating above the horizon when the fleet is in proximity | `sea`, `port_arrival_overlay` | Coloured banner with port name + flag, suspended above the coastline silhouette |
| `nav:village_nameplate` | Same shape but for a discovered village | `sea` | Smaller / different-styled banner above settlement icon |
| `nav:landmark` | Lighthouse / monument / ruin / shipwreck indicator | `sea`, `sailing_idle` | Small landmark glyph on the horizon |
| `nav:quick_supply` | The standalone "Quick Supply" prompt that appears beside a port nameplate at sea | `sea` | Yellow button labelled "Quick Supply" near the port nameplate |
| `nav:building_nameplate` | Building name floating above the character when walking close to it in port | `port_overworld` | Floating panel above a building entrance, may also show a one-line description |
| `nav:enemy_fleet` | Hostile sail icon on the horizon | `sea` | Red sail / hostile-marker glyph |
| `nav:player_fleet` | Another player's fleet visible | `sea` | Neutral / coloured sail with player tag |
| `nav:discovery_overlay` | First-visit notification banner | `sea`, `port_arrival_overlay`, `port_overworld` | Transient banner / badge — "Newly Discovered:" + name |
| `nav:nearby_ports_hud` | Persistent right-side panel listing nearby ports with distances | `sea` | Right-edge panel, mini-map at top + list of port-name + km |
| `nav:right_panel_minimap` | The small map at the top of the right-side panel | `port_overworld`, `sea` | Round mini-map preview, top of right rail |
| `nav:destination_action_button` | The big yellow "Go to City" / "Move to Village" / "Move" button on the world map | `world_map` | Bottom-centre full-width yellow action button |

The last three (`nearby_ports_hud`, `right_panel_minimap`,
`destination_action_button`) aren't in the original Phase 4b list but
came up directly in the 2026-05-22 analysis: the first two cause
expensive false-positive obstruction detections, the third caused the
Move-to-Village miss-tap loop.  Treating them as detector classes
eliminates those issues at the root.

## Where to look first

Highest-yield existing labelled frames, by screen type:

| Screen type | Frames | Most likely classes inside |
|---|---:|---|
| `sea` | 47 | port_nameplate, village_nameplate, quick_supply, landmark, enemy/player_fleet, nearby_ports_hud, right_panel_minimap, discovery_overlay |
| `port_arrival_overlay` | 25 | port_nameplate, discovery_overlay |
| `port_overworld` | 45 | building_nameplate, right_panel_minimap, discovery_overlay |
| `sailing_idle` | 21 | landmark, possibly enemy/player_fleet |
| `world_map` | 61 | destination_action_button (when a city/village is selected) |

The dialog/transient frames (`dialog_*`, `result_screen`, `loading`,
etc.) usually don't carry these classes; skip them for now.

## Labeling workflow

1. **Open the supervisor labeler** in the browser
   (`python -m supervisor.labeler` then visit the URL it prints).

2. Click into a session that has lots of `sea` / `port_overworld` /
   `port_arrival_overlay` frames.

3. For each frame, scan for any of the classes above:
   - If you spot one or more, type comma-separated tags into the
     **tags** field (not notes): e.g.
     `nav:port_nameplate, nav:quick_supply`
   - If a class appears multiple times in one frame (e.g. two enemy
     fleets), still tag once — bbox extraction handles multiplicity.
   - Skip the frame (no tags) if none of the classes are visible.

4. Don't worry about getting *every* frame.  We need ~30–50 examples
   per class.  Sparse classes (enemy fleet, discovery overlay) may
   need a dedicated re-capture pass after the obvious sources are
   mined; that comes later.

5. Notes field can stay free-form for anything unusual (e.g. *"only
   half the nameplate visible — partial occlusion"*).

## Target sample counts

| Class | Target | Notes |
|---|---:|---|
| port_nameplate | 50 | Common on every sea approach; mine `sea` + `port_arrival_overlay` first |
| village_nameplate | 30 | Less common; mine the Berber / Aceh / etc. session frames |
| landmark | 30 | Visible on long sailing legs |
| quick_supply | 30 | Rare in current corpus (only 3 mentions today); may need re-capture |
| building_nameplate | 50 | Very common on `port_overworld` |
| enemy_fleet | 20 | Rare; OK to undershoot for first model |
| player_fleet | 20 | Same |
| discovery_overlay | 20 | Mostly on first-visit frames; mine port-loading/arrival |
| nearby_ports_hud | 40 | Present on most `sea` frames; quick to mine |
| right_panel_minimap | 40 | Present on most `sea` + `port_overworld` frames |
| destination_action_button | 30 | Mine `world_map` frames with a city/village selected |

Total tagged frames expected: ~300–400.  Many frames will get
multiple tags (a `sea` frame can have `nav:port_nameplate +
nav:quick_supply + nav:nearby_ports_hud + nav:right_panel_minimap`
all at once — that's fine and even desirable, since it teaches the
YOLO model to detect multiple objects per frame).

## Quality criteria

- The class element should be **clearly visible** in the frame.
  Partial occlusion is OK if a human can recognise it; a single pixel
  of edge is not.
- **Don't tag the wrong class.**  A village nameplate and a port
  nameplate look superficially similar — if you can't tell, leave
  the tag off.  False positives hurt training more than missed
  positives.
- For `building_nameplate`: only tag when the floating nameplate
  panel is **expanded** (showing description text) or clearly
  floating above the building entrance — not when it's the small
  ambient label.

## What happens next (you don't do this part)

Once at least ~20 frames carry each class tag:

1. `tools/label_yolo_via_claude.py` (to write next): for every frame
   with at least one `nav:*` tag, send to Claude Vision with the
   class list and ask for YOLO-format bounding boxes.  Save to
   `data/training/yolo_uwo/labels/<session>__<frame>.txt`, symlink
   the image to `data/training/yolo_uwo/images/`.

2. Train YOLO11n on the resulting dataset (~30 min on Apple MPS).

3. Integrate `vision/yolo_uwo.py` into `perceive()` per the Phase 4a
   integration plan in `docs/yolo_navigation_classifier.md`.

The tags you add are the *only* manual input.  Claude does the
expensive part (drawing the boxes).  YOLO does the runtime part
(50ms per frame).

## Re-capture (later, if needed)

Some classes may stay underfilled after mining the existing corpus —
quick_supply and discovery_overlay are the obvious candidates.  When
that happens we'll capture a dedicated route (e.g. sail Lisbon →
Las Palmas → Berber with all events enabled) and add those frames to
the labeling set.  Not blocking the initial model.

## See also

- `docs/yolo_navigation_classifier.md` — the broader Phase 4a+4b
  design this labeling work feeds.
- `data/knowledge/screen_types.json` — current screen-type taxonomy
  (orthogonal to YOLO classes, but the screen_type tag is what we
  filter on when picking which frames to scan).
- `data/labels.jsonl` — the existing label corpus that gets
  amended with `nav:*` tags.
