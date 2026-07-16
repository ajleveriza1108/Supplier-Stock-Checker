from decimal import Decimal
import unittest

from core.scrape_result import StockState, VerificationStatus
from scrapers.walmart_policy import (
    WalmartObservation,
    WalmartPolicy,
    parse_inventory_detail,
)


class WalmartPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = WalmartPolicy()

    def resolve(self, json_obs, visual_obs):
        return self.policy.resolve(
            url="https://www.walmart.com/ip/example/123",
            item_id="123",
            title="Example Product",
            final_url="https://www.walmart.com/ip/example/123",
            target_variation="",
            json_observation=json_obs,
            visual_observation=visual_obs,
        )

    def classify(self, **signals):
        payload = {
            "regionFound": True,
            "enabledCta": False,
            "disabledCta": False,
            "selectedOptionOos": False,
            "productOos": False,
            "inventoryText": "",
            "fulfillment": {},
            **signals,
        }
        return self.policy.classify_rendered_signals(payload)

    def test_agreeing_in_stock_and_price_is_verified(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                exact_item_match=True,
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                title_match=True,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.sheet_stock, "In Stock")

    def test_normal_in_stock_missing_visual_price_is_partial(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                exact_item_match=True,
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.IN_STOCK,
                price=None,
                title_match=True,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.PARTIAL)
        self.assertIsNone(result.sheet_stock)

    def test_agreeing_oos_does_not_require_price(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.OOS,
                exact_item_match=True,
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.OOS,
                title_match=True,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.sheet_stock, "OOS")

    def test_json_oos_and_visual_in_stock_remains_conflict(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.OOS,
                price=Decimal("20.00"),
                exact_item_match=True,
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                title_match=True,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)
        self.assertIsNone(result.sheet_stock)

    def test_internal_source_conflict_is_conflict(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.UNKNOWN,
                exact_item_match=True,
                details={"internal_conflict": True},
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                title_match=True,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)
        self.assertIsNone(result.sheet_stock)

    def test_price_disagreement_is_conflict(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                exact_item_match=True,
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.IN_STOCK,
                price=Decimal("25.00"),
                title_match=True,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)

    def test_json_only_is_partial(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                exact_item_match=True,
            ),
            WalmartObservation(source="visual"),
        )
        self.assertEqual(result.verification, VerificationStatus.PARTIAL)
        self.assertIsNone(result.sheet_stock)

    def test_low_stock_and_visual_in_stock_becomes_verified_oos(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.LOW_STOCK,
                price=Decimal("20.00"),
                exact_item_match=True,
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                title_match=True,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.observed_stock, StockState.LOW_STOCK)
        self.assertEqual(result.sheet_stock, "OOS")

    def test_low_stock_and_strong_visual_oos_are_compatible(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.LOW_STOCK,
                price=Decimal("20.00"),
                exact_item_match=True,
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.OOS,
                title_match=True,
                details={"oos_scope": "product"},
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.observed_stock, StockState.LOW_STOCK)
        self.assertEqual(result.sheet_stock, "OOS")

    def test_quantity_and_all_fulfillment_oos_are_compatible(self):
        result = self.resolve(
            WalmartObservation(
                source="json",
                stock=StockState.QUANTITY_REMAINING,
                quantity=1,
                price=Decimal("20.00"),
                exact_item_match=True,
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.OOS,
                title_match=True,
                details={"oos_scope": "all_fulfillment"},
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.observed_stock, StockState.QUANTITY_REMAINING)
        self.assertEqual(result.quantity, 1)
        self.assertEqual(result.sheet_stock, "OOS")

    def test_enabled_cta_ignores_one_unavailable_fulfillment_method(self):
        stock, quantity, reason, conflict, scope = self.classify(
            enabledCta=True,
            productOos=True,
            fulfillment={
                "shipping": {"state": "AVAILABLE"},
                "delivery": {"state": "UNAVAILABLE"},
            },
        )
        self.assertEqual(stock, StockState.IN_STOCK)
        self.assertIsNone(quantity)
        self.assertFalse(conflict)
        self.assertEqual(scope, "")
        self.assertIn("Add to cart", reason)

    def test_enabled_cta_preserves_low_stock(self):
        stock, quantity, _, conflict, _ = self.classify(
            enabledCta=True,
            inventoryText="Shipping arrives tomorrow. Low stock",
        )
        self.assertEqual(stock, StockState.LOW_STOCK)
        self.assertIsNone(quantity)
        self.assertFalse(conflict)

    def test_enabled_cta_preserves_quantity(self):
        stock, quantity, _, conflict, _ = self.classify(
            enabledCta=True,
            inventoryText="Only 6 remaining",
        )
        self.assertEqual(stock, StockState.QUANTITY_REMAINING)
        self.assertEqual(quantity, 6)
        self.assertFalse(conflict)

    def test_enabled_cta_and_selected_option_oos_is_conflict(self):
        stock, _, _, conflict, scope = self.classify(
            enabledCta=True,
            selectedOptionOos=True,
        )
        self.assertEqual(stock, StockState.UNKNOWN)
        self.assertTrue(conflict)
        self.assertEqual(scope, "selected_option")

    def test_selected_option_oos_without_cta_is_oos(self):
        stock, _, _, conflict, scope = self.classify(
            selectedOptionOos=True,
        )
        self.assertEqual(stock, StockState.OOS)
        self.assertFalse(conflict)
        self.assertEqual(scope, "selected_option")

    def test_product_level_oos_without_cta_is_oos(self):
        stock, _, _, conflict, scope = self.classify(
            productOos=True,
        )
        self.assertEqual(stock, StockState.OOS)
        self.assertFalse(conflict)
        self.assertEqual(scope, "product")

    def test_all_detected_fulfillment_methods_unavailable_is_oos(self):
        stock, _, _, conflict, scope = self.classify(
            fulfillment={
                "shipping": {"state": "UNAVAILABLE"},
                "pickup": {"state": "UNAVAILABLE"},
                "delivery": {"state": "UNAVAILABLE"},
            },
        )
        self.assertEqual(stock, StockState.OOS)
        self.assertFalse(conflict)
        self.assertEqual(scope, "all_fulfillment")

    def test_one_unavailable_fulfillment_method_is_not_product_oos(self):
        stock, _, _, conflict, scope = self.classify(
            fulfillment={
                "delivery": {"state": "UNAVAILABLE"},
            },
        )
        self.assertEqual(stock, StockState.UNKNOWN)
        self.assertFalse(conflict)
        self.assertEqual(scope, "")

    def test_available_fulfillment_without_cta_remains_unknown(self):
        stock, _, _, conflict, _ = self.classify(
            fulfillment={
                "shipping": {"state": "AVAILABLE"},
                "delivery": {"state": "UNAVAILABLE"},
            },
        )
        self.assertEqual(stock, StockState.UNKNOWN)
        self.assertFalse(conflict)

    def test_inventory_parser_keeps_quantity(self):
        stock, quantity = parse_inventory_detail(
            "Only 6 remaining",
            available=True,
        )
        self.assertEqual(stock, StockState.QUANTITY_REMAINING)
        self.assertEqual(quantity, 6)



if __name__ == "__main__":
    unittest.main()
