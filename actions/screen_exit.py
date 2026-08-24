"""Canonical 'exit / close the current screen' helper.

CLAUDE.md "back-press hygiene" rule:
    press_back() is a last-resort fallback.  When an on-screen close
    indicator (Home button / dialog X / in-game Back arrow) is
    detected, tap that instead.

This module exposes one function — exit_current_screen() — that
implements the priority order so every caller funnels through the same
decision tree.

Priority order:
  1. Dialog close-X when a DialogModel is detected with a close button.
     (Tightly scoped to the dialog overlay; closes overlays without
     touching the underlying scene.)
  2. Home button when chrome reports it.  The same top-right slot also
     hosts the main-menu X icon (the Home glyph swaps to an X when the
     hamburger menu is open) — see memory/project_main_menu_close_icon.md.
     Tap the slot; the underlying logic is the same.
  3. In-game back arrow (top-left) when chrome reports it.
  4. System press_back() as a last resort.  Logged at WARNING.

Hard refusals:
  - port_overworld + system back → would open the "Exit Game?" dialog.
    refuse_on_overworld guards this (default True).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal

from loguru import logger
from PIL import Image

ExitMethod = Literal["dialog_close", "home_or_menu_x", "back_arrow", "press_back", "refused"]


# Stable tap coordinates for the chrome icons (frame is 2400 × 1080).
_HOME_SLOT_XY        = (2300,  45)   # top-right Home / main-menu X
_BACK_ARROW_SLOT_XY  = ( 110,  40)   # top-left in-game back arrow


@dataclass(frozen=True)
class ExitResult:
    ok:     bool
    method: ExitMethod
    note:   str = ""


def exit_current_screen(
    frame: Optional[Image.Image] = None,
    *,
    allow_back_fallback: bool = True,
    refuse_on_overworld: bool = True,
    dry_run: bool = False,
) -> ExitResult:
    """Exit / close the current screen, preferring on-screen indicators
    over system back-press.

    Args:
        frame: optional pre-captured frame.  If None, captures one.
        allow_back_fallback: when False, return ExitResult(ok=False,
            method="refused") instead of firing press_back().  Use when
            the caller wants to know we couldn't find an on-screen exit
            without firing a risky system back.
        refuse_on_overworld: when True, never fire system back if the
            perceived state is port_overworld (would open Exit Game?).
            The on-screen taps are still attempted; only the press_back
            fallback is gated.
        dry_run: when True, decide what to do but do NOT actually tap
            or press_back.  ExitResult.method reports what WOULD have
            fired.  Useful for probe scripts and tests.

    Returns:
        ExitResult describing which method was used.

    Side effects:
        Taps the chosen target via ADB.  Does NOT poll for the resulting
        state — caller is expected to perceive() afterwards.
    """
    from capture.adb_capture import capture_screen
    from actions.adb_actions import tap, press_back
    from vision.chrome_detector import get_chrome_detector

    if frame is None:
        frame = capture_screen()

    # 1) Dialog close-X (most specific — operates on the overlay alone).
    try:
        from vision.region_detectors.dialog import detect_dialog
        from vision.omniparser import parse_fast_cached
        elements = parse_fast_cached(frame)
        dlg = detect_dialog(elements, frame.width, frame.height)
        if dlg is not None and dlg.close_button is not None:
            x1, y1, x2, y2 = dlg.close_button
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            logger.info(
                f"[exit_current_screen] dialog close-X at ({cx},{cy}) "
                f"(kind={dlg.kind()!r}) — "
                f"{'would tap' if dry_run else 'tapping'}"
            )
            if not dry_run:
                tap(cx, cy)
            return ExitResult(True, "dialog_close",
                              note=f"dialog kind={dlg.kind()} @ ({cx},{cy})")
    except Exception as e:
        logger.debug(f"[exit_current_screen] dialog probe failed: {e!r}")

    # 2) Chrome detector — Home / Back-arrow flags.
    try:
        chrome = get_chrome_detector().detect(frame)
    except Exception as e:
        logger.warning(f"[exit_current_screen] chrome detect failed: {e!r}")
        chrome = None

    # The Home slot (2300,45) means "back to overworld" ONLY on chromed screens
    # (buildings / sub-menus / world map). On the OVERWORLDS themselves it is the ☰
    # hamburger → tapping it OPENS Company Overview (the bug that stranded the gather
    # run 2026-08-17). has_hamburger flags an overworld → never tap Home there.
    # See memory project_home_button_is_chromed_only_escape.
    if chrome is not None and chrome.has_home and not chrome.has_hamburger:
        # Tap where the icon WAS DETECTED, falling back to the calibrated slot only when
        # the detector could not say. The game re-bakes its camera-cutout offset per
        # screen: on Jakarta's Market the Home icon was at (2219,44) while this slot says
        # (2300,45) — 81px away, on nothing.
        xy = (chrome.positions or {}).get("home") or _HOME_SLOT_XY
        logger.info(
            f"[exit_current_screen] Home (or main-menu X) at {xy}"
            f"{'' if (chrome.positions or {}).get('home') else ' (calibrated slot — not detected)'}"
            f" — {'would tap' if dry_run else 'tapping'}"
        )
        if not dry_run:
            tap(*xy)
        return ExitResult(True, "home_or_menu_x")
    if chrome is not None and chrome.has_home and chrome.has_hamburger:
        logger.info(
            "[exit_current_screen] Home slot is the ☰ hamburger (overworld) — "
            "refusing to tap it (would open Company Overview)"
        )

    if chrome is not None and chrome.has_back_arrow:
        xy = (chrome.positions or {}).get("back_arrow") or _BACK_ARROW_SLOT_XY
        logger.info(
            f"[exit_current_screen] in-game back arrow at {xy}"
            f"{'' if (chrome.positions or {}).get('back_arrow') else ' (calibrated slot — not detected)'}"
            f" — {'would tap' if dry_run else 'tapping'}"
        )
        if not dry_run:
            tap(*xy)
        return ExitResult(True, "back_arrow")

    # 3) Last resort — system press_back, with guards.
    if not allow_back_fallback:
        logger.warning(
            "[exit_current_screen] no on-screen exit found and "
            "allow_back_fallback=False — refusing"
        )
        return ExitResult(False, "refused",
                          note="no on-screen exit; back disabled")

    if refuse_on_overworld:
        # Cheap perceive to gate the back-press.
        try:
            from brain.perceive import perceive
            state = perceive(frame).state
        except Exception as e:
            logger.debug(f"[exit_current_screen] perceive failed: {e!r}")
            state = None
        if state == "port_overworld":
            logger.warning(
                "[exit_current_screen] refusing press_back on "
                "port_overworld (would open Exit Game?).  "
                "No on-screen close indicator was detected — bot may "
                "be in an unexpected state"
            )
            return ExitResult(False, "refused",
                              note="press_back blocked on port_overworld")

    logger.warning(
        "[exit_current_screen] no Home / back-arrow / dialog close-X "
        f"detected — {'would fall back' if dry_run else 'falling back'} "
        "to system press_back().  This is a smell: usually means we "
        "don't actually understand the current screen.  Consider a "
        "deeper analysis."
    )
    if not dry_run:
        press_back()
    return ExitResult(True, "press_back",
                      note="fallback — no on-screen exit found")
