"""Regression tests for Walmart exact selected-offer detection v9.1."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.walmart import WalmartScraper, _ResolvedSnapshot


class WalmartExactSelectedOfferTests(unittest.TestCase):
    ITEM_ID = "123456789"
    URL = "https://www.walmart.com/ip/Test-Product/123456789"

    def resolve(self, **changes):
        snapshot = {
            "pageUrl": self.URL,
            "pageItemId": self.ITEM_ID,
            "identityIds": [self.ITEM_ID],
            "identityConflict": False,
            "title": "Test Product",
            "captcha": False,
            "enabledPurchase": False,
            "purchaseControlExact": True,
            "purchaseControl": {},
            "priceText": "",
            "lowStockTexts": [],
            "selectedOptionOos": False,
            "productOos": False,
            "productOosTexts": [],
            "fulfillment": {},
            "allFulfillmentUnavailable": False,
            "jsonLdExact": {},
            "jsonLdConflict": False,
            "locationText": "",
        }
        snapshot.update(changes)
        return WalmartScraper._resolve_snapshot(snapshot, self.ITEM_ID)

    def test_enabled_exact_purchase_ignores_alternate_offer_oos(self):
        result = self.resolve(
            enabledPurchase=True,
            purchaseControlExact=True,
            priceText="Current price $49.88",
            productOos=False,
            productOosTexts=["Open Box Out of stock"],
            jsonLdExact={"availability": "OutOfStock", "price": "49.88"},
        )
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.stock, "In Stock")
        self.assertEqual(result.price, "49.88")
        self.assertIn("ignored", result.reason.lower())

    def test_inexact_purchase_control_is_not_trusted(self):
        result = self.resolve(
            enabledPurchase=True,
            purchaseControlExact=False,
            priceText="$49.88",
        )
        self.assertEqual(result.status, "Error")
        self.assertEqual(result.stock, "Unable to Verify")

    def test_selected_option_oos_without_purchase_is_oos(self):
        result = self.resolve(
            selectedOptionOos=True,
            productOos=True,
            productOosTexts=["Out of stock"],
        )
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.stock, "OOS")
        self.assertEqual(result.price, "")

    def test_low_stock_is_preserved_for_engine_business_rule(self):
        result = self.resolve(
            enabledPurchase=True,
            priceText="$98.00",
            lowStockTexts=["Low stock"],
        )
        self.assertEqual(result.stock, "Low Stock")
        self.assertEqual(result.price, "98.00")

    def test_limited_stock_is_preserved(self):
        result = self.resolve(
            enabledPurchase=True,
            priceText="$79.00",
            lowStockTexts=["Limited stock"],
        )
        self.assertEqual(result.stock, "Limited Stock")

    def test_quantity_remaining_is_preserved(self):
        result = self.resolve(
            enabledPurchase=True,
            priceText="$89.98",
            lowStockTexts=["Only 1 left in stock"],
        )
        self.assertEqual(result.stock, "Only 1 Remaining")

    def test_product_oos_beats_stale_json_ld_in_stock(self):
        result = self.resolve(
            productOos=True,
            productOosTexts=["Out of stock"],
            jsonLdExact={"availability": "InStock", "price": "47.49"},
        )
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.stock, "OOS")

    def test_json_ld_alone_is_held_instead_of_updating(self):
        result = self.resolve(
            jsonLdExact={"availability": "InStock", "price": "34.88"},
        )
        self.assertEqual(result.status, "Error")
        self.assertEqual(result.stock, "Unable to Verify")
        self.assertIn("structured data", result.reason.lower())

    def test_conflicting_json_ld_does_not_override_exact_dom(self):
        result = self.resolve(
            enabledPurchase=True,
            priceText="$34.88",
            jsonLdConflict=True,
            jsonLdExact={},
        )
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.stock, "In Stock")
        self.assertEqual(result.price, "34.88")

    def test_all_fulfillment_unavailable_is_location_sensitive(self):
        result = self.resolve(
            fulfillment={
                "shipping": "Not available",
                "pickup": "Not available",
                "delivery": "Not available",
            },
            allFulfillmentUnavailable=True,
        )
        self.assertEqual(result.status, "Error")
        self.assertEqual(result.stock, "Unable to Verify")
        self.assertIn("location", result.reason.lower())

    def test_identity_conflict_is_held(self):
        result = self.resolve(
            identityIds=[self.ITEM_ID, "999999999"],
            identityConflict=True,
            enabledPurchase=True,
            priceText="$10.00",
        )
        self.assertEqual(result.status, "Error")
        self.assertEqual(result.stock, "Unable to Verify")

    def test_wrong_item_redirect_is_error(self):
        result = self.resolve(
            pageUrl="https://www.walmart.com/ip/Other/999999999",
            pageItemId="999999999",
            identityIds=["999999999"],
            enabledPurchase=True,
            priceText="$10.00",
        )
        self.assertEqual(result.status, "Error")
        self.assertEqual(result.stock, "Unable to Verify")

    def test_item_id_parser(self):
        self.assertEqual(
            WalmartScraper._extract_item_id(self.URL),
            self.ITEM_ID,
        )

    def test_two_matching_snapshots_are_required(self):
        first = _ResolvedSnapshot(
            status="Success",
            stock="In Stock",
            price="10.00",
            title="Test",
            item_id=self.ITEM_ID,
            reason="",
            evidence={},
        )
        second = _ResolvedSnapshot(
            status="Success",
            stock="In Stock",
            price="10.00",
            title="Test",
            item_id=self.ITEM_ID,
            reason="",
            evidence={},
        )
        self.assertTrue(
            WalmartScraper._has_two_matching_successes([first, second])
        )

    def test_conflicting_snapshots_do_not_form_consensus(self):
        first = _ResolvedSnapshot(
            status="Success",
            stock="In Stock",
            price="10.00",
            title="Test",
            item_id=self.ITEM_ID,
            reason="",
            evidence={},
        )
        second = _ResolvedSnapshot(
            status="Success",
            stock="OOS",
            price="",
            title="Test",
            item_id=self.ITEM_ID,
            reason="",
            evidence={},
        )
        self.assertFalse(
            WalmartScraper._has_two_matching_successes([first, second])
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
