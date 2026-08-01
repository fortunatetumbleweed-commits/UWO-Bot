"""Tests for vision/element_postprocess.py — semantic tagging of
OmniParser detections.

Each test pins one classifier against a representative element (positions
and labels lifted from real labelled frames: Bergen, Socotra, the 7
world-map captures).
"""

import unittest
from dataclasses import dataclass

from vision.element_postprocess import (
    tag_elements, group_by_role, filter_non_noise,
    ROLE_PORT_NAME, ROLE_BUILDING_TITLE, ROLE_MODE_TAB,
    ROLE_CHROME_ICON, ROLE_CURRENCY_LABEL,
    ROLE_RIGHT_PANEL_TAB, ROLE_RIGHT_PANEL_ROW, ROLE_DATE_TIME,
    ROLE_APPELLATION, ROLE_PLAYER_NAMEPLATE, ROLE_BUILDING_NAMEPLATE,
    ROLE_NPC_BUBBLE, ROLE_PHONE_OS, ROLE_BUILD_INFO,
    ROLE_EVENT_BANNER, ROLE_BUTTON, ROLE_TEXT, ROLE_ICON,
    ROLE_NOTIFICATION_DOT, ROLE_PROGRESS_BAR, ROLE_LOCKED_INDICATOR,
    NOISE_ROLES,
)


@dataclass
class _FakeEl:
    """Minimal DetectedElement-shape stub for unit testing."""
    label: str
    element_type: str
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def cx(self) -> int:    return (self.x1 + self.x2) // 2
    @property
    def cy(self) -> int:    return (self.y1 + self.y2) // 2
    @property
    def width(self) -> int: return self.x2 - self.x1
    @property
    def height(self) -> int: return self.y2 - self.y1


def _at(label, etype, cx, cy, w, h):
    return _FakeEl(label=label, element_type=etype,
                    x1=cx - w//2, y1=cy - h//2,
                    x2=cx + w//2, y2=cy + h//2)


class FixedUIRoleTests(unittest.TestCase):
    """Tagger correctly identifies fixed UI elements by position."""

    def test_port_name_top_left(self):
        # Bergen frame: 'Bergen' at (392, 58), 127x45, text
        el = _at("Bergen", "text", 392, 58, 127, 45)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_PORT_NAME)

    def test_building_title_for_building_nav_state(self):
        # Same position+content classifier; nav_state changes the role.
        # 'Union' frame: top-left title on a building interior.
        el = _at("Union", "text", 290, 47, 130, 50)
        tagged = tag_elements([el], nav_state="building")
        self.assertEqual(tagged[0].role, ROLE_BUILDING_TITLE)

    def test_building_title_for_submenu_nav_state(self):
        # 'Invest' top-left on a Bureau sub_menu screen.
        el = _at("Invest", "text", 220, 56, 120, 40)
        tagged = tag_elements([el], nav_state="sub_menu")
        self.assertEqual(tagged[0].role, ROLE_BUILDING_TITLE)

    def test_port_name_not_emitted_for_building(self):
        el = _at("Bank", "text", 268, 46, 100, 40)
        tagged = tag_elements([el], nav_state="building")
        self.assertNotEqual(tagged[0].role, ROLE_PORT_NAME)

    def test_mode_tab_world_map(self):
        el = _at("Explore", "button", 1167, 47, 179, 60)
        tagged = tag_elements([el], nav_state="world_map")
        self.assertEqual(tagged[0].role, ROLE_MODE_TAB)

    def test_mode_tab_only_fires_on_world_map(self):
        # Same element on port_overworld should NOT be a mode_tab
        el = _at("Explore", "button", 1167, 47, 179, 60)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertNotEqual(tagged[0].role, ROLE_MODE_TAB)

    def test_chrome_icon_top_right(self):
        el = _at("icon", "icon", 2335, 46, 93, 92)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_CHROME_ICON)

    def test_currency_label_top_right_numeric(self):
        # From Market frame: gold counter '14,001,701,263' at (1587, 36).
        # Numeric content in top-right zone → currency_label.
        el = _at("14,001,701,263", "text", 1587, 36, 200, 30)
        tagged = tag_elements([el], nav_state="building")
        self.assertEqual(tagged[0].role, ROLE_CURRENCY_LABEL)

    def test_currency_label_smaller_value(self):
        # Bureau frame: '5,952' at (1761, 33).
        el = _at("5,952", "text", 1761, 33, 100, 30)
        tagged = tag_elements([el], nav_state="building")
        self.assertEqual(tagged[0].role, ROLE_CURRENCY_LABEL)

    def test_currency_label_rejects_non_numeric_top_right(self):
        # 'Pass' is in the top-right zone but it's a button word, not
        # numeric.  Should fall through to chrome_icon (button branch).
        el = _at("Pass", "button", 1775, 49, 89, 97)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertNotEqual(tagged[0].role, ROLE_CURRENCY_LABEL)

    def test_pass_button_is_chrome_icon(self):
        # 'Pass' at (1775, 49) is a chrome button in top-right cluster
        el = _at("Pass", "button", 1775, 49, 89, 97)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_CHROME_ICON)

    def test_phone_os_bar_clock(self):
        el = _at("22.52", "text", 186, 1064, 60, 24)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_PHONE_OS)

    def test_phone_os_battery_right_side(self):
        # Battery indicator '6.11%' at (1409, 1052) — was missed when
        # the x range was too narrow (< 600 only).
        el = _at("6.11%", "text", 1409, 1052, 66, 24)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_PHONE_OS)

    def test_build_info_bottom_right(self):
        el = _at("4.0401.081.285 2604141540 Atlantic Ocean",
                  "text", 2172, 1068, 372, 24)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_BUILD_INFO)


class AppellationNameplateTests(unittest.TestCase):
    """Player title and name above sprite — fixed positions."""

    def test_appellation_at_fixed_position(self):
        el = _at("Eastern Explorer", "text", 1196, 375, 274, 44)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_APPELLATION)

    def test_player_nameplate_below_appellation(self):
        el = _at("TaylorFP", "text", 1235, 507, 192, 60)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_PLAYER_NAMEPLATE)


class BuildingNameplateTests(unittest.TestCase):
    """Building name plates floating above building entrances."""

    def test_building_nameplate_in_gameplay(self):
        # Socotra Harbor name plate at (691, 178), 426x114
        el = _at("Harbor", "button", 691, 178, 426, 114)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_BUILDING_NAMEPLATE)

    def test_building_nameplate_near_right_edge(self):
        # Socotra Inn name plate at (1818, 692) — was outside the
        # original gameplay x-range and got mis-tagged as button.
        el = _at("Inn", "button", 1818, 692, 375, 115)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_BUILDING_NAMEPLATE)


class NpcBubbleTests(unittest.TestCase):
    """The three-signal NPC bubble heuristic."""

    def test_single_line_bubble_at_top(self):
        # Socotra: 'switching Its armor.' at (1223, 18) — at top of
        # screen but with bubble characteristics.
        el = _at("switching Its armor.", "button", 1223, 18, 325, 37)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_NPC_BUBBLE)

    def test_multi_line_bubble(self):
        # Socotra: 'The area around here has been a center of trade
        # for many years;' at (1850, 250), 322x132 — wraps to 3 lines.
        # Per-line height ≈ 44px which is still bubble-sized.
        el = _at("The area around here has been a center of trade "
                  "for many years;", "button", 1850, 250, 322, 132)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_NPC_BUBBLE)

    def test_ui_label_is_not_bubble(self):
        # 'Market' in right panel — should be right_panel_row, not bubble
        el = _at("Market", "button", 2198, 515, 399, 71)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_RIGHT_PANEL_ROW)

    def test_short_text_in_gameplay_is_not_bubble(self):
        # A short proper noun in the gameplay area is more likely a
        # building name plate than a bubble.  TaylorFP is a single
        # token, _looks_like_sentence requires multi-word.
        el = _at("TaylorFP", "text", 800, 400, 200, 40)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertNotEqual(tagged[0].role, ROLE_NPC_BUBBLE)

    def test_bubble_is_noise_role(self):
        self.assertIn(ROLE_NPC_BUBBLE, NOISE_ROLES)


class EventBannerTests(unittest.TestCase):
    """The top-banner ticker that contains port names but isn't at
    those ports."""

    def test_event_banner_with_occurred(self):
        el = _at("Maca Boom occurred in Yeongil",
                  "text", 1237, 127, 400, 40)
        tagged = tag_elements([el], nav_state="world_map")
        self.assertEqual(tagged[0].role, ROLE_EVENT_BANNER)

    def test_event_banner_is_noise(self):
        self.assertIn(ROLE_EVENT_BANNER, NOISE_ROLES)


class RightPanelTests(unittest.TestCase):
    """Vertical building list and tab bar on port_overworld."""

    def test_building_row(self):
        el = _at("Bureau", "button", 2194, 918, 411, 68)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_RIGHT_PANEL_ROW)

    def test_tab_bar_icon(self):
        el = _at("icon", "icon", 2146, 156, 90, 63)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_RIGHT_PANEL_TAB)

    def test_date_token_in_date_bar(self):
        el = _at("Spring", "text", 2130, 399, 86, 36)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertEqual(tagged[0].role, ROLE_DATE_TIME)


class FilterNoiseTests(unittest.TestCase):
    """filter_non_noise drops NPC bubbles, phone OS, build info,
    event banners."""

    def test_filter_drops_noise_roles(self):
        elements = [
            _at("Socotra", "text", 400, 56, 138, 38),                       # port_name
            _at("switching Its armor.", "button", 1223, 18, 325, 37),       # bubble
            _at("22.05", "text", 186, 1064, 60, 24),                        # phone_os
        ]
        tagged = tag_elements(elements, nav_state="port_overworld")
        clean = filter_non_noise(tagged)
        roles = sorted(t.role for t in clean)
        self.assertEqual(roles, [ROLE_PORT_NAME])


class GroupByRoleTests(unittest.TestCase):
    def test_groups_by_role(self):
        elements = [
            _at("icon", "icon", 2335, 46, 93, 92),     # chrome_icon
            _at("icon", "icon", 2055, 47, 78, 77),     # chrome_icon
            _at("Bergen", "text", 392, 58, 127, 45),   # port_name
        ]
        tagged = tag_elements(elements, nav_state="port_overworld")
        groups = group_by_role(tagged)
        self.assertEqual(len(groups[ROLE_CHROME_ICON]), 2)
        self.assertEqual(len(groups[ROLE_PORT_NAME]), 1)


class NotificationDotTests(unittest.TestCase):
    """Small red dots on sub-menu rows (Shipyard Build/Repair, Item Shop
    Black Market, Bank Savings Account, Union Requests, etc.)."""

    def test_small_icon_is_notification_dot(self):
        # Tiny icon in the left strip — likely a notification overlay.
        el = _at("icon", "icon", 300, 220, 24, 24)
        tagged = tag_elements([el], nav_state="building")
        self.assertEqual(tagged[0].role, ROLE_NOTIFICATION_DOT)

    def test_chrome_size_icon_is_not_notification_dot(self):
        # Full-size chrome icon (top-right cluster) — NOT a notification.
        # Falls into chrome_top_right since it's in the top-right zone.
        el = _at("icon", "icon", 2335, 46, 90, 90)
        tagged = tag_elements([el], nav_state="building")
        self.assertNotEqual(tagged[0].role, ROLE_NOTIFICATION_DOT)

    def test_text_element_never_notification_dot(self):
        # Text elements never qualify, regardless of size.
        el = _at("357", "text", 300, 220, 24, 24)
        tagged = tag_elements([el], nav_state="building")
        self.assertNotEqual(tagged[0].role, ROLE_NOTIFICATION_DOT)


class ProgressBarTests(unittest.TestCase):
    """Wide horizontal fill bars (trade points, investment progress,
    Shipyard build progress)."""

    def test_wide_button_is_progress_bar(self):
        # 400x20 button — aspect 20:1, classic progress-bar shape.
        el = _at("", "button", 500, 500, 400, 20)
        tagged = tag_elements([el], nav_state="building")
        self.assertEqual(tagged[0].role, ROLE_PROGRESS_BAR)

    def test_wide_icon_also_qualifies(self):
        # Icon variant — same shape, also a progress bar.
        el = _at("icon", "icon", 500, 500, 300, 18)
        tagged = tag_elements([el], nav_state="building")
        self.assertEqual(tagged[0].role, ROLE_PROGRESS_BAR)

    def test_text_element_never_progress_bar(self):
        el = _at("357/1,000", "text", 500, 500, 200, 20)
        tagged = tag_elements([el], nav_state="building")
        self.assertNotEqual(tagged[0].role, ROLE_PROGRESS_BAR)

    def test_tall_button_is_not_progress_bar(self):
        # Tall button — aspect 1:1, can't be a progress bar.
        el = _at("Recruit", "button", 500, 500, 100, 100)
        tagged = tag_elements([el], nav_state="building")
        self.assertNotEqual(tagged[0].role, ROLE_PROGRESS_BAR)

    def test_narrow_short_element_is_not_progress_bar(self):
        # 60x20 — too narrow to be a meaningful progress bar.
        el = _at("", "button", 500, 500, 60, 20)
        tagged = tag_elements([el], nav_state="building")
        self.assertNotEqual(tagged[0].role, ROLE_PROGRESS_BAR)


class LockedIndicatorTests(unittest.TestCase):
    """Text advertising a locked / unavailable feature.  Used by the
    bot to skip currently-ungated features (Auction, Assault, Union
    high-tier slots, Bureau Manage Market Event when not Mayor, etc.)."""

    def test_unavailable_is_locked_indicator(self):
        el = _at("Unavailable", "text", 240, 90, 130, 30)
        tagged = tag_elements([el], nav_state="sub_menu")
        self.assertEqual(tagged[0].role, ROLE_LOCKED_INDICATOR)

    def test_locked_is_locked_indicator(self):
        el = _at("Locked", "text", 240, 90, 80, 30)
        tagged = tag_elements([el], nav_state="building")
        self.assertEqual(tagged[0].role, ROLE_LOCKED_INDICATOR)

    def test_requires_phrase_in_longer_text(self):
        el = _at("Requires Company LV 30", "text", 300, 600, 300, 30)
        tagged = tag_elements([el], nav_state="building")
        self.assertEqual(tagged[0].role, ROLE_LOCKED_INDICATOR)

    def test_player_company_level_is_not_locked(self):
        # 'LV 92' alone (no Requires / Unavailable / Locked) should NOT
        # be flagged — it could be the player's own level display.
        el = _at("LV 92", "text", 1106, 1053, 56, 26)
        tagged = tag_elements([el], nav_state="port_overworld")
        self.assertNotEqual(tagged[0].role, ROLE_LOCKED_INDICATOR)

    def test_insurance_level_is_not_locked(self):
        # 'Insurance LV 1' is a tier label, not a locked indicator.
        el = _at("Insurance LV 1", "text", 2156, 545, 250, 30)
        tagged = tag_elements([el], nav_state="building")
        self.assertNotEqual(tagged[0].role, ROLE_LOCKED_INDICATOR)

    def test_company_lv_alone_is_not_locked(self):
        # Bare 'Company LV 15' — ambiguous (could be player level OR
        # unlock condition).  Classifier deliberately does NOT match
        # this; downstream consumers handle disambiguation via context.
        el = _at("Company LV 15", "text", 2200, 600, 200, 30)
        tagged = tag_elements([el], nav_state="sub_menu")
        self.assertNotEqual(tagged[0].role, ROLE_LOCKED_INDICATOR)


if __name__ == "__main__":
    unittest.main()
