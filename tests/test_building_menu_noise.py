"""Building-list reader filters out system-status noise.

Origin: 2026-05-14 explore_port run on London surfaced building-list
entries like:
  'fortune teller 4.0501.041.291 2605141023 atlantic ocean'
  '4,0501,041.291 2605141023 atlantic ocean'
  'atlantic ocean'

These are the server name + player UID + build-version texts in the
bottom strip of the right panel, OCR-merged with the lowest visible
building label.  navigate_to_building's fuzzy match then fails on the
garbled row.

This module pins the `_is_building_list_noise` filter so server names,
UIDs, and version strings are dropped.
"""

from __future__ import annotations

import unittest

from vision.ocr import _is_building_list_noise


class BuildingListNoiseFilterTests(unittest.TestCase):

    def test_server_name_filtered(self):
        self.assertTrue(_is_building_list_noise("atlantic ocean"))
        self.assertTrue(_is_building_list_noise("pacific ocean"))
        self.assertTrue(_is_building_list_noise("indian ocean"))
        self.assertTrue(_is_building_list_noise("arctic ocean"))
        # Mixed case + leading garbage
        self.assertTrue(_is_building_list_noise("xx Atlantic Ocean"))

    def test_player_uid_filtered(self):
        # The dotted UID pattern (e.g. 4.0501.041.291)
        self.assertTrue(_is_building_list_noise("4.0501.041.291"))
        self.assertTrue(_is_building_list_noise("4,0501,041.291"))
        # Long all-digit string (build timestamp / UID)
        self.assertTrue(_is_building_list_noise("2605141023"))
        # Merged-with-label case
        self.assertTrue(_is_building_list_noise(
            "fortune teller 4.0501.041.291 2605141023 atlantic ocean"
        ))

    def test_version_string_filtered(self):
        self.assertTrue(_is_building_list_noise("12.605.141023"))

    def test_real_building_labels_kept(self):
        for label in [
            "harbor", "market", "shipyard", "bank", "inn", "cathedral",
            "venturer association", "item shop", "union", "bureau",
            "palace", "fortune teller",
            # 'wet season' / 'bonsoir summer' used to be in this list as
            # placeholder OCR garbage that we wanted to keep.  Both are
            # now correctly filtered as season header noise (see
            # _BUILDING_LIST_NOISE_PATTERNS).
        ]:
            self.assertFalse(
                _is_building_list_noise(label),
                f"unexpected filter on {label!r}",
            )


class ShopVariantTests(unittest.TestCase):
    """Item Shop's interior title bar reads 'Shop' (not 'Item Shop').
    The KB variant list must include 'shop' so navigate_to_building
    accepts the entry instead of pressing Back."""

    def test_shop_is_item_shop_variant(self):
        from brain.kb import control
        variants = [v.lower() for v in control().building_name_variants("item_shop")]
        self.assertIn("shop", variants)
        self.assertIn("item shop", variants)


if __name__ == "__main__":
    unittest.main()
