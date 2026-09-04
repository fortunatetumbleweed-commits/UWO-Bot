"""The village barter PANEL: open it, choose a good, read it back, tell if it is still live.

MOVED HERE FROM `brain/barter_mission_live.py`, unchanged. It captures screens, parses them
and taps — it is UI, and it was sitting in the task layer where UI is forbidden. Nothing
noticed because the layering guard counted a task module's imports of `actions`/`vision`/
`capture`, and this code WAS those imports: `brain/barter_command.py` measured perfectly clean
while reaching the whole barter panel through `barter_mission_live`, which imported thirteen
UI modules on its behalf. A façade inside the task layer defeats a guard that only looks one
hop.

Activities may reach the UI freely, and they are the callers: `VillageActivity` injects
`_open_barter_panel` / `_select_trade_good` / `_read_panel_state` as its panel functions.
"""
from __future__ import annotations

from typing import Mapping, Optional

from loguru import logger

# How many times to re-read the left menu before believing what it says. OmniParser is
# non-deterministic and a village menu that reads empty once is usually still there.
_MENU_READ_ATTEMPTS = 3
_DAILY_COUNT_SPENT = "used all your daily"   # the Notice sentence, lowercased


def _no_panel_failure() -> dict:
    """Why we are refusing to barter, described by what the screen ACTUALLY shows.

    Reporting the perceived screen is the point: the caller's state is the thing that is
    wrong, and it can only correct itself if it is told what is really there.
    """
    from actions.sail_actions import where_am_i
    from actions.ui import active_submenu
    try:
        here = where_am_i()
        detail = here.get("detail") or here.get("location")
        submenu = active_submenu()
    except Exception as exc:
        detail, submenu = f"unreadable ({exc})", None
    logger.warning(f"[mission.barter] the Barter panel is not open and could not be opened — "
                   f"screen reads {detail!r} (sub-menu {submenu!r}). Not committing from here.")
    return {"ok": False, "reason": f"barter panel not open — screen shows {detail!r}",
            "screen": detail, "submenu": submenu}


def _open_barter_panel() -> bool:
    """Open the village's Barter sub-menu. True when the screen confirms we are on it.

    Goes through the LEFT MENU REGION, not a frame-wide label search. A chromed screen has a
    known layout (user, 2026-08-23): title top-left, the menu item list directly below it on
    the left, a centre panel, a right panel, and a top menu bar at the top right — except a
    VILLAGE, which has no top menu bar. Searching the whole frame for a word ignores that
    layout and picks up prose: live 2026-08-23 the village page carried "Barter" both as the
    menu item (x=183) and inside "Increase Barter Count by 3" in the Amity Effect panel
    (x=1052). The unconstrained search took the prose and the tap did nothing.

    `vision.region_detectors.left_menu` is the canonical reader for that region and also
    reports `is_locked` / `is_selected`, so this needs no geometry of its own.

    Confirmed by the TITLE, which in this game is always the sub-menu currently selected
    (`actions.ui.active_submenu`) — so "did the tap work?" is answered by reading, not
    assuming.
    """
    import time as _t
    from actions import ui
    from actions.ui import on_submenu
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.left_menu import detect_left_menu
    try:
        frame = capture_screen()
        if on_submenu("barter", frame):
            return True                      # already there — do not tap again

        # ONE EMPTY FRAME IS NOT PROOF THE MENU IS ABSENT: the menu animates in, and a
        # standby gate can cover it for a moment, so a single capture can land on nothing.
        #
        # NB on history: this retry was added on 2026-08-24 believing a San Village read of
        # [] was transient. It was NOT — `detect_left_menu` was judging reward widgets by box
        # width, OmniParser boxed three of the five rows full-width in that capture, and the
        # real menu was discarded every single time. Three looks failed identically. The
        # actual fix was in the detector (identify by association, not dimension).
        # The retry is kept because animation and gates are real, but it is a cushion, not a
        # cure — a read that fails repeatedly means the DETECTOR is wrong, not the timing.
        item = None
        for attempt in range(_MENU_READ_ATTEMPTS):
            # PASS THE FRAME. The lock is a RED RIBBON, and only the pixels say so — the
            # wording (`Cannot Exchange` here, `Unavailable` elsewhere) is not dependable.
            menu = detect_left_menu(list(parse_fast_cached(frame)), frame.width,
                                    frame.height, frame=frame)
            item = menu.find("Barter") if menu else None
            if item is not None:
                break
            if attempt < _MENU_READ_ATTEMPTS - 1:
                logger.info(f"[mission.barter] left menu read as "
                            f"{menu.labels() if menu else None} — re-perceiving "
                            f"({attempt + 2}/{_MENU_READ_ATTEMPTS})")
                _t.sleep(1.5)
                frame = capture_screen()
        if item is None:
            logger.warning(f"[mission.barter] no 'Barter' item in the left menu after "
                           f"{_MENU_READ_ATTEMPTS} looks "
                           f"(menu reads {menu.labels() if menu else None})")
            return False
        if item.get("is_locked"):
            # NOT A FAILURE — this is the game saying the day's barters are used up. When the
            # last available round is spent the panel returns to the village top menu on its
            # own and the Barter item goes dark under a red "Unavailable" ribbon (user,
            # 2026-08-23). It means the ship should LEAVE, which is success, not an error.
            logger.info("[mission.barter] the Barter item is UNAVAILABLE — the day's barters "
                        "are used up; the bartering is finished and the fleet should leave")
            return "unavailable"

        ui.tap_element(item, why="village → Barter", dwell="dialog")
        after = capture_screen()
        opened = on_submenu("barter", after)
        if not opened and daily_barters_used_up(after):
            # THE SAME FACT, ANSWERED THE OTHER WAY. The `is_locked` check above reads the
            # game's answer BEFORE the tap, off the ribbon. Live 2026-08-30 at Svear the item
            # was not locked at all: the tap went through and the game answered AFTER it, with
            # a Notice. One detector saw nothing, so the run looped — tap, notice, dismiss,
            # tap — until it stalled. Both readings mean the day is spent and the fleet should
            # leave, so both return the same word.
            logger.info("[mission.barter] the game says the daily Trade Count is spent — the "
                        "bartering is finished and the fleet should leave")
            return "unavailable"
        logger.info(f"[mission.barter] Barter panel opened: {opened}")
        return opened
    except Exception as exc:
        logger.warning(f"[mission.barter] could not open the Barter panel: {exc}")
        return False


def daily_barters_used_up(frame=None, *, elements=None) -> bool:
    """The game's own refusal, read off the Notice it raises when the day's rounds are gone.

    Live 2026-08-30 at Svear Village, after run 12 spent every round reaching amity 100,000:
    `Notice / You have used all your daily Trade Count: / 17.3348 / OK`, over the village top
    menu. OmniParser reads that sentence as ONE label, so a substring is enough — and the
    phrase appears on no other screen.

    The trailing countdown is the time until the rounds reset. It is deliberately NOT read:
    nothing here waits for it, and a number nobody uses is a number nobody checks.
    """
    try:
        if elements is None:
            from vision.omniparser import parse_fast_cached
            elements = parse_fast_cached(frame) if frame is not None else []
        for e in elements or []:
            if _DAILY_COUNT_SPENT in (getattr(e, "label", "") or "").strip().lower():
                return True
    except Exception as exc:
        logger.debug(f"[mission.barter] could not check the daily-count notice: {exc}")
    return False


def refresh_stale_panel(good: str) -> bool:
    """Close the barter panel and open it again, reselecting `good`.

    A panel left open goes stale — at Svear on 2026-08-26 one had been open long enough for
    its refresh timer to run BACKWARDS, and it accepted the Exchange confirmation without
    doing anything. Reopening it fixed the commit outright.

    Module-level so the village activity and the mission node share ONE implementation of it
    rather than each carrying a copy (2026-08-26).
    """
    import time as _t
    from actions import ui
    logger.info("[barter] refreshing the barter panel")
    ui.back(why="close a stale barter panel")
    _t.sleep(2.0)
    if not _open_barter_panel():
        return False
    _t.sleep(1.5)
    return bool(_select_trade_good(good))   # None (unreadable) collapses to False here


def _exchange_still_live(frame=None, *, elements=None) -> bool:
    """True when a LIVE (yellow) Exchange button is on screen.

    The game greys the button the moment another barter is impossible, so this is its own
    verdict on "can I barter again?" — ahead of any count we compute from the panel.

    READ THE COLOUR, NOT A COST PILL. This asked `_yellow_commit_button`, which recognises
    the game's `<cost> VERB` commit — a button with a PRICE, like '205,848 Recruit'. A
    barter costs MATERIALS, not ducats, so Exchange carries no cost and that detector finds
    nothing on this panel: `detect_commit_buttons` returned 0 while the button sat there in
    plain gold.
    #
    Live 2026-08-29 at Svear Village: one round committed, four Trade Count slots still
    open, every material plentiful (Wares 734/104, Firearms 367/52, Sundries 1,243/104) —
    and the panel classified as BLOCKED because the button could not be seen. The tap path
    was never affected; `commit_via_positive_taps` uses the colour test and pressed the same
    button seconds earlier.

    Gold background IS the rule (CLAUDE.md): "a POSITIVE button is identified by its
    yellow/gold background, not its wording".
    """
    try:
        from capture.adb_capture import capture_screen
        from vision.omniparser import parse_fast_cached
        from brain.commit_actions import has_positive_background
        if frame is None:
            frame = capture_screen()
        if elements is None:
            elements = parse_fast_cached(frame)
        for e in elements or []:
            label = (getattr(e, "label", "") or "").strip().lower()
            if label != "exchange":          # 'Bulk Exch.' is a different control
                continue
            live = bool(has_positive_background(frame, e))
            logger.debug(f"[mission.barter] Exchange @({e.cx},{e.cy}) gold={live}")
            return live
        return False
    except Exception as exc:
        logger.debug(f"[mission.barter] Exchange liveness check failed: {exc}")
        return False


def _select_trade_good(good: str, recipe: Optional[Mapping[str, int]] = None) -> bool:
    """Select `good` in the Tradable Trade Goods row. True when the panel confirms it.

    The tiles carry NO NAMES — only a thumbnail, a stock status and a category (user,
    2026-08-23). So the bot cannot search for "Box of Nutmeg": it taps a tile and READS BACK
    what the panel then shows, which is the verification the name would have given.

    Order matters only as an optimisation: the category is a strong hint (Box of Nutmeg is a
    spice), so a tile whose category matches is tried first, and the rest follow. Correctness
    comes from the read-back, never from the hint.

    A stock status of "Insufficient" or "Depleted" is NOT a reason to skip a tile — those
    reduce the YIELD, not the ability to trade (docs/game_mechanics.md).
    """
    from actions import ui
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached

    # ONE OBSERVATION, SHARED. These reads used to take their own captures — three per round
    # of an unchanged panel, 17 calls producing 7 distinct readings across one barter session.
    # The repository hands out the frame it holds, and the ACTION LAYER tells it when that is
    # superseded, so a tap below is what forces the next capture rather than a habit here.
    from actions.perception import screen
    frame = screen().get(why="the barter panel's goods row").frame
    tiles = _tradable_tiles(parse_fast_cached(frame))
    if not tiles:
        # NOT SEEING THE GOODS IS NOT THE SAME AS THE GOODS NOT BEING THERE. Returning False
        # made the caller announce "'Bambara Groundnut' is not on offer today" — a claim about
        # the village — on the strength of a parse that had simply come back empty. Live
        # 2026-08-30 at Hutu Village that ended the mission with six barter rounds unspent,
        # both materials aboard, and the good sitting on the panel.
        #
        # None means "ask again", which is the dispatcher's business. False keeps its old
        # meaning: the tiles were read, and none of them was the good.
        logger.warning("[mission.barter] the Barter panel's goods row could not be read — "
                       "that is not the same as the good being absent")
        return None

    want = _category_hint(good)
    # Stable, so tiles of equal rank keep their left-to-right order.
    tiles.sort(key=lambda t: 0 if (want and t["category"] == want) else 1)
    logger.info(f"[mission.barter] {len(tiles)} tradable tile(s): "
                f"{[(t['category'], t['status']) for t in tiles]}"
                + (f" — trying {want!r} first" if want else ""))

    from actions.barter_reader import read_barter_panel

    # READ THE PANEL BEFORE TOUCHING ANYTHING. Without a baseline the FIRST tap has nothing to
    # be compared against, and a first tap is exactly the one most likely to be swallowed: an
    # info tip left over from a previous tap on a locked good's banner sits over the screen,
    # and the next tap anywhere DISMISSES THE TIP instead of selecting (user, 2026-08-26).
    #
    # Live that day: the tip was up, the bot tapped the Birch Tree tile, the tip vanished, the
    # selection did not change — and with no baseline the unchanged reading was recorded as
    # "the Wares tile is Juniper Berry, not Birch Tree". The one tile that WAS the wanted good
    # got crossed off without ever being tested.
    baseline = read_barter_panel(screen().get(why="barter panel baseline").frame)
    last_sig = _panel_signature(baseline)
    last_good = getattr(baseline, "selected_good", None)
    if baseline is not None and _panel_matches(baseline, good, recipe):
        logger.info(f"[mission.barter] {good!r} is already the selected good")
        return True
    logger.info(f"[mission.barter] panel starts on {last_good!r}")

    untested = []

    def _try(tile) -> Optional[bool]:
        """Tap one tile and read back. True = it is the good, False = it is not,
        None = the tap never landed, so this tile says NOTHING about the village."""
        nonlocal last_sig, last_good
        label = tile["category"] or "unnamed"
        reading = None
        # Twice, at most. A swallowed tap costs the tile nothing but a repeat; the second tap
        # meets a screen with no tip on it.
        for attempt in (1, 2):
            ui.tap_at(tile["cx"], tile.get("tap_y", tile["cy"]),
                      why=f"barter → select the {label} good")
            # Verify against the RAW reading: it carries `selected_good` and each material's
            # label/need. `_read_panel_state` derives a PanelBarterState for the ROUND maths
            # and drops the good's name, so it cannot answer "is this the right good?".
            # The tap above told the repository the screen moved, so this captures. That is
            # the ONLY reason to capture here, and now it is the repository's decision rather
            # than this loop's habit.
            reading = read_barter_panel(screen().get(why="barter tile read-back").frame)
            if reading is None:
                break
            if _panel_signature(reading) != last_sig:
                break
            # AN UNCHANGED PANEL SAYS NOTHING ABOUT THE VILLAGE — it says the tap did not
            # land. Four different tiles cannot all be the same good.
            logger.warning(f"[mission.barter] the panel still reads {last_good!r} after "
                           f"tapping the {label} tile (attempt {attempt}/2) — that tap did "
                           "not register. Not concluding anything about the village.")
        if reading is None or _panel_signature(reading) == last_sig:
            return None
        last_sig, last_good = _panel_signature(reading), reading.selected_good
        if _panel_matches(reading, good, recipe):
            logger.info(f"[mission.barter] selected {good!r} via the {label!r} tile")
            return True
        logger.info(f"[mission.barter] the {label!r} tile is "
                    f"{reading.selected_good!r}, not {good!r} — trying the next")
        return False

    for tile in tiles:
        verdict = _try(tile)
        if verdict:
            return True
        if verdict is None:
            logger.warning(f"[mission.barter] could not get the "
                           f"{tile['category'] or 'unnamed'} tile to select — moving on, but "
                           "it remains UNTESTED")
            untested.append(tile)

    # AN UNTESTED TILE FORBIDS THE CONCLUSION. Live 2026-08-30 at Hutu Village the FIRST tile
    # tapped swallowed both its taps while the two after it selected on one each — and the
    # swallowed one was Bambara Groundnut, the good the mission came for. The pass ended
    # "none of the tiles read back as 'Bambara Groundnut'", which the caller reported as "not
    # on offer today", with six rounds open and both materials aboard.
    #
    # By now the panel is demonstrably warm: other tiles have selected on it. So go back for
    # the ones that never answered, rather than reporting a village's offerings from a tile
    # that was never read.
    if untested:
        logger.info(f"[mission.barter] {len(untested)} tile(s) never answered on the first "
                    f"pass; the panel is warm now — going back for them")
        still_untested = []
        for tile in untested:
            verdict = _try(tile)
            if verdict:
                return True
            if verdict is None:
                still_untested.append(tile)
        if still_untested:
            logger.warning(
                f"[mission.barter] {[t['category'] for t in still_untested]} would not select "
                f"even on a warm panel — the row is unread, NOT proof that {good!r} is absent")
            return None

    logger.warning(f"[mission.barter] none of the tiles read back as {good!r} "
                   f"(saw: {last_good!r} last)")
    return False


# How far a column may sit from the strip's own pitch and still belong to it.
_STRIP_PITCH_TOL_FRACTION = 0.35


def _tradable_tiles(elements) -> list:
    """The goods tiles: an icon with a CATEGORY label directly beneath it.

    Measured on the Melanesian Village barter panel: icons at cy≈424, status labels at
    cy≈509, category labels at cy≈552, in four columns at cx ≈ 424/560/693/828. The columns
    are found by pairing each category label with the icon above it, so nothing here is a
    fixed coordinate.
    """
    icons = [e for e in elements
             if getattr(e, "element_type", "") == "icon" and 350 < e.cy < 480]
    labels = [e for e in elements
              if (getattr(e, "label", "") or "").strip() and 530 < e.cy < 580]
    status = [e for e in elements
              if (getattr(e, "label", "") or "").strip().lower()
              in ("insufficient", "depleted", "sufficient", "abundant")]
    out = []
    for lab in labels:
        icon = min(icons, key=lambda e: abs(e.cx - lab.cx), default=None)
        if icon is not None and abs(icon.cx - lab.cx) > 80:
            icon = None
        st = min(status, key=lambda e: abs(e.cx - lab.cx), default=None)
        if st is not None and abs(st.cx - lab.cx) > 80:
            st = None
        # THE CATEGORY LABEL PROVES THE TILE; THE ICON ONLY REFINES IT. Requiring an icon
        # DROPPED the tile when OmniParser did not emit one — and it often does not: measured
        # live 2026-08-30 at Hutu Village, three tiles on screen produced three category
        # labels, three status chips, and only TWO icon elements. On the frame that mattered
        # it produced none at all, so this returned [], and the caller reported "'Bambara
        # Groundnut' is not on offer today" about a panel offering it, with six barter rounds
        # unspent and both materials aboard.
        #
        # The row already decides the tap height (see `_aim_below_the_banner`, which takes the
        # MEDIAN extent precisely because a single box comes back short when something
        # overlaps it). A missing box is the same problem one step further on, and the same
        # answer serves: keep the tile, let the row place it.
        if icon is None and st is None:
            continue                      # a label with nothing under it is not a tile
        anchor = icon if icon is not None else st
        out.append({"cx": lab.cx, "cy": anchor.cy,
                    "y1": getattr(icon, "y1", None) if icon is not None else None,
                    "y2": getattr(icon, "y2", None) if icon is not None else None,
                    "category": (lab.label or "").strip(),
                    "status": (st.label or "").strip() if st is not None else ""})
    # LEFT TO RIGHT, as they are drawn. Built from OmniParser's element order these came out
    # arbitrary — at Svear on 2026-08-26 the strip was walked 2nd, 4th, 1st, 3rd, so the good
    # the mission wanted was tried third instead of first. The category hint below is only a
    # hint, and for a good absent from _GOOD_CATEGORY it is None, which left the order
    # entirely to chance.
    out.sort(key=lambda t: t["cx"])
    return _aim_below_the_banner(_strip_row(out))


def _aim_below_the_banner(tiles: list) -> list:
    """Give every tile a tap point clear of the lock banner, using the ROW's geometry.

    A locked good draws a red banner ACROSS THE MIDDLE of its thumbnail — measured 2026-08-26
    at Svear, y≈420-460 on thumbnails spanning y≈366-514. The icon's centre lands inside it,
    and tapping the banner does not select: it raises an info tip, which then sits over the
    strip and swallows the NEXT tap. That is how Birch Tree was missed twice.

    A locked tile IS still selectable; only its banner is not (user, 2026-08-26). So aim low
    rather than skipping the tile.

    The row decides, not each icon. Tiles share one vertical extent, and an individual box
    can come back short when something overlaps it — on that same frame the info tip clipped
    tile 4's icon, whose own box would have put the tap at y=442, back inside the banner.
    """
    bottoms = [t["y2"] for t in tiles if t.get("y2") is not None]
    tops = [t["y1"] for t in tiles if t.get("y1") is not None]
    for t in tiles:
        if bottoms and tops:
            y1 = sorted(tops)[len(tops) // 2]
            y2 = sorted(bottoms)[len(bottoms) // 2]
            t["tap_y"] = int(y2 - (y2 - y1) * 0.15)
        else:
            t["tap_y"] = t["cy"]
    return tiles


def _strip_row(tiles: list) -> list:
    """Keep only the tiles that form THE STRIP — one evenly-spaced row.

    THE BOT MUST TAP INSIDE THE STRIP AND NOWHERE ELSE (user, 2026-08-26). The detail panel
    on the right carries labels at the same height as the category row, and at Svear one of
    them — "Negotiate" — was paired with a nearby icon, accepted as a fifth goods tile, and
    tapped at (1959, 486).

    A run of goods tiles is regular: measured there, cx 423 / 558 / 693 / 829, gaps of
    135, 135, 136. "Negotiate" sat at 1959 — a gap of 1130. So the strip identifies itself by
    its own spacing, and nothing here is an absolute coordinate (CLAUDE.md: find the element,
    do not write down where it is). Same reasoning as the world-map tab strip in
    `sail_actions._tab_strip_candidates`: a row is a row because it is evenly spaced, not
    because it is near the top.
    """
    if len(tiles) < 3:
        return tiles
    gaps = [b["cx"] - a["cx"] for a, b in zip(tiles, tiles[1:])]
    typical = sorted(gaps)[len(gaps) // 2]           # the median gap IS the strip's pitch
    if typical <= 0:
        return tiles
    runs, current = [], [tiles[0]]
    for gap, tile in zip(gaps, tiles[1:]):
        if abs(gap - typical) <= max(_STRIP_PITCH_TOL_FRACTION * typical, 12):
            current.append(tile)
        else:
            runs.append(current)
            current = [tile]
    runs.append(current)
    best = max(runs, key=len)
    if len(best) < len(tiles):
        dropped = [t["category"] for t in tiles if t not in best]
        logger.info(f"[mission.barter] ignoring {dropped} — outside the goods strip "
                    f"(pitch {typical}px)")
    return best


# Output good -> the category its tile carries. A hint for ORDERING only; the panel read-back
# is what decides. Extend as goods are met.
_GOOD_CATEGORY = {"box of nutmeg": "Spices"}


# Observed on the Svear Village strip, 2026-08-26: the timber tile is Birch Tree (Wares) and
# the basket tile is Naverslojd (Crafts). A hint only re-orders the attempts; correctness
# always comes from reading the panel back.
_GOOD_CATEGORY.setdefault("birch tree", "Wares")
_GOOD_CATEGORY.setdefault("naverslojd", "Crafts")
_GOOD_CATEGORY.setdefault("näverslöjd", "Crafts")


def _category_hint(good: str) -> Optional[str]:
    return _GOOD_CATEGORY.get((good or "").strip().lower())


def _panel_signature(reading) -> tuple:
    """What the panel is showing, as something comparable.

    The good's NAME alone is not enough: it comes back None when the title is unreadable, and
    then every reading compares equal to every other — which would make a perfectly good
    selection look like a tap that never landed. The materials and their per-round needs
    change with the good, so they carry the difference when the name cannot.
    """
    if reading is None:
        return ()
    mats = tuple((getattr(m, "label", ""), getattr(m, "need", None))
                 for m in getattr(reading, "materials", []) or ())
    return (getattr(reading, "selected_good", None), mats)


def _panel_matches(reading, good: str, recipe: Optional[Mapping[str, int]]) -> bool:
    """Is the selected good the one we want? By NAME when the panel gives one, else by the
    RECIPE — the materials and their per-round needs are a fingerprint we already hold."""
    name = (getattr(reading, "selected_good", None) or "").strip().lower()
    if name:
        return name == (good or "").strip().lower()
    mats = {(m.label or "").strip().lower(): m.need for m in getattr(reading, "materials", [])}
    if not mats or not recipe:
        return False
    return all(mats.get(k.lower()) == v for k, v in recipe.items())


def _read_panel_state(frame=None, *, reading=None):
    """The village barter panel as a `PanelBarterState`, or None if it isn't readable.

    ONE place converts the panel into rounds — the arrival bound and the per-round gate
    must not drift apart.

    TAKES THE CALLER'S FRAME. It captured its own, so a tick that also read the raw panel
    paid for two captures of one screen: `read_barter_panel(self._frame())` alongside this.
    `reading` goes further — hand over the raw panel already parsed and nothing is read at
    all (CLAUDE.md: "capture the screenshot once per tick, pass the same Image").
    """
    from brain.barter_quantity import panel_barter_state
    from actions.barter_reader import read_barter_panel
    if reading is None:
        if frame is None:
            from capture.adb_capture import capture_screen
            frame = capture_screen()
        reading = read_barter_panel(frame)
    if reading is None or not reading.materials:
        return None
    return panel_barter_state(reading.materials, reading.output_quantity or 0)
