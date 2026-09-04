"""The scrim tells a modal from a panel — the two wear identical chrome.

Every frame here is a real capture. The point of the suite is the PAIRS: the barter and
market right panels look exactly like the dialogs (same brown title bar) and must come
back CLEAR, because a panel never covers a control.
"""
from pathlib import Path

import pytest
from PIL import Image

from vision.overlay import CLEAR, DARK, SCRIM, Overlay, detect_overlay, scrim_state

FRAMES = Path(__file__).parent / "stage_suite" / "frames"

CASES = [
    # panels + plain screens — same chrome as a dialog, but nothing is dimmed
    ("barter_panel_birch_tree_selected", CLEAR),
    ("barter_panel_wrong_good_selected", CLEAR),
    ("market_restock_blue_gem",          CLEAR),   # Purchase page + right panel, no modal
    ("port_overworld_amsterdam",         CLEAR),
    ("sell_grid_iron_2099",              CLEAR),
    ("sell_grid_page2_neroli",           CLEAR),
    ("sell_grid_page_one",               CLEAR),
    ("village_trade_list_missing_quantity", CLEAR),
    ("world_map_village_list",           CLEAR),
    # centred modals — a 50% scrim caps the gutter's white chrome at 128
    ("market_goods_info_dialog",         SCRIM),
    ("sell_result_dialog_with_owned_counts", SCRIM),
    # full-screen takeovers are NOT scrims over a live screen, and must not read as one
    ("idle_lock_looks_like_a_notice",    DARK),
    ("transient_mate_promotion",         DARK),
]


@pytest.mark.parametrize("name,expected", CASES)
def test_scrim_state(name, expected):
    frame = Image.open(FRAMES / f"{name}.png").convert("RGB")
    assert scrim_state(frame) == expected


def test_a_panel_never_blocks_a_tap():
    """The caveat that motivated the whole design: the barter panel is styled exactly like
    a dialog, and every control on the screen stays live while it is open."""
    frame = Image.open(FRAMES / "barter_panel_birch_tree_selected.png").convert("RGB")
    ov = detect_overlay(frame)
    assert not ov.is_modal
    assert not ov.blocks(387, 1008)      # the Put In Bulk checkbox position


def test_a_modal_blocks_outside_and_allows_inside():
    frame = Image.open(FRAMES / "market_goods_info_dialog.png").convert("RGB")
    ov = detect_overlay(frame)
    assert ov.is_modal and ov.bbox is not None
    assert ov.blocks(387, 1008)          # Put In Bulk, behind the scrim — the live bug
    cx = (ov.bbox[0] + ov.bbox[2]) // 2
    cy = (ov.bbox[1] + ov.bbox[3]) // 2
    assert not ov.blocks(cx, cy)         # inside the dialog is fine


def test_a_modal_with_no_bbox_blocks_everything():
    """Knowing a modal is up but not where means no point can be SHOWN safe."""
    ov = Overlay(kind="modal", state=SCRIM, bbox=None)
    assert ov.blocks(0, 0) and ov.blocks(1200, 540)


def test_a_dark_screen_does_not_block_taps():
    """DARK is not a scrim. A full-screen takeover and a merely dim scene look alike from
    the gutter, and only the 128 cap is measured — blocking on darkness would strand the
    bot on any dim screen, so `blocks` stays False and the family classifier decides."""
    frame = Image.open(FRAMES / "idle_lock_looks_like_a_notice.png").convert("RGB")
    ov = detect_overlay(frame)
    assert ov.state == DARK and ov.kind == "dark"
    assert not ov.is_modal and not ov.blocks(387, 1008)
