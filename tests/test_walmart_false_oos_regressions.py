"""Regression tests for the final conservative Walmart repair."""

from __future__ import annotations

import unittest

from core.scrape_result import StockState
from scrapers.walmart import WALMART_VISUAL_FIX_VERSION, WalmartScraper
from scrapers.walmart_doublecheck import WalmartDoubleCheckScraper


class WalmartConservativeRegressionTests(unittest.TestCase):
    def classify_json_item(self, item):
        return WalmartScraper._item_stock(None, item)


    def test_engine_doublecheck_wrapper_uses_same_scraper(self):
        self.assertTrue(
            issubclass(WalmartDoubleCheckScraper, WalmartScraper)
        )

    def test_fix_version_is_current(self):
        self.assertEqual(
            WALMART_VISUAL_FIX_VERSION,
            "2026.07.13.5",
        )

    def test_one_unavailable_fulfillment_method_is_not_whole_product_oos(self):
        stock, reason, conflict = self.classify_json_item(
            {
                "usItemId": "799609741",
                "fulfillmentOptions": [
                    {"availabilityStatus": "OUT_OF_STOCK"},
                ],
            }
        )
        self.assertEqual(stock, StockState.UNKNOWN)
        self.assertFalse(conflict)
        self.assertIn("inconclusive", reason.lower())

    def test_all_unavailable_fulfillment_methods_can_confirm_json_oos(self):
        stock, _, conflict = self.classify_json_item(
            {
                "usItemId": "3006322739",
                "fulfillmentOptions": [
                    {"availabilityStatus": "OUT_OF_STOCK"},
                    {"availabilityStatus": "NOT_AVAILABLE"},
                ],
            }
        )
        self.assertEqual(stock, StockState.OOS)
        self.assertFalse(conflict)

    def test_available_method_prevents_json_oos(self):
        stock, _, conflict = self.classify_json_item(
            {
                "usItemId": "2032170767",
                "fulfillmentOptions": [
                    {"availabilityStatus": "AVAILABLE"},
                    {"availabilityStatus": "OUT_OF_STOCK"},
                ],
            }
        )
        self.assertEqual(stock, StockState.IN_STOCK)
        self.assertFalse(conflict)

    def test_add_to_cart_and_oos_json_is_internal_conflict(self):
        stock, _, conflict = self.classify_json_item(
            {
                "usItemId": "1300095544",
                "availabilityStatus": "OUT_OF_STOCK",
                "buyBox": {"cta": {"text": "Add to cart"}},
            }
        )
        self.assertEqual(stock, StockState.UNKNOWN)
        self.assertTrue(conflict)

    def test_standalone_product_oos_phrases_are_strong(self):
        for text in (
            "Out of stock",
            "This item is out of stock.",
            "Sold out",
            "Currently unavailable",
            "Not available",
            "No longer available.",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    WalmartScraper._is_strong_product_oos_text(text)
                )

    def test_fulfillment_variant_and_location_oos_are_not_product_oos(self):
        for text in (
            "Delivery: Out of stock",
            "Pickup is unavailable at this store",
            "Blue selected option is out of stock",
            "Other sellers are out of stock",
            "Out of stock in Sacramento",
            "Shipping out of stock; pickup available",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    WalmartScraper._is_strong_product_oos_text(text)
                )

    def test_nested_other_variant_low_stock_is_ignored(self):
        item = {
            "usItemId": "2032170767",
            "availabilityStatus": "IN_STOCK",
            "buyBox": {"cta": {"text": "Add to cart"}},
            "variants": [
                {
                    "usItemId": "different-option",
                    "availabilityText": "Limited stock",
                }
            ],
        }
        text = WalmartScraper._item_inventory_text(item)
        self.assertNotIn("Limited stock", text)
        self.assertIn("IN_STOCK", text)

    def test_selected_fulfillment_low_stock_is_preserved(self):
        item = {
            "usItemId": "799609741",
            "availabilityStatus": "IN_STOCK",
            "buyBox": {"cta": {"text": "Add to cart"}},
            "fulfillmentOptions": [
                {
                    "availabilityStatus": "AVAILABLE",
                    "availabilityText": "Low stock",
                }
            ],
        }
        text = WalmartScraper._item_inventory_text(item)
        self.assertIn("Low stock", text)

    def test_ambiguous_rendered_prices_are_rejected(self):
        self.assertIsNone(
            WalmartScraper._candidate_price([56.99, 51.29])
        )

    def test_one_rendered_price_is_accepted(self):
        self.assertEqual(
            str(WalmartScraper._candidate_price([56.99])),
            "56.99",
        )


if __name__ == "__main__":
    unittest.main()
