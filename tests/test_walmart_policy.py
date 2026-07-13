"""Tests for the conservative Walmart consensus policy."""

from __future__ import annotations

import unittest
from decimal import Decimal

from core.scrape_result import StockState, VerificationStatus
from scrapers.walmart_policy import (
    WalmartObservation,
    WalmartPolicy,
    parse_inventory_detail,
)


class WalmartPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = WalmartPolicy()

    def resolve(
        self,
        json_observation: WalmartObservation,
        visual_observation: WalmartObservation,
    ):
        return self.policy.resolve(
            url="https://www.walmart.com/ip/example/123",
            item_id="123",
            title="Example Product",
            final_url="https://www.walmart.com/ip/example/123",
            target_variation="",
            json_observation=json_observation,
            visual_observation=visual_observation,
        )

    def classify(self, **signals):
        payload = {
            "regionFound": True,
            "exactItemAnchor": True,
            "enabledCta": False,
            "disabledCta": False,
            "selectedOptionOos": False,
            "productOos": False,
            "inventoryText": "",
            "fulfillment": {},
            **signals,
        }
        return self.policy.classify_rendered_signals(payload)

    @staticmethod
    def json_observation(
        stock: StockState,
        *,
        price: Decimal | None = None,
        quantity: int | None = None,
        internal_conflict: bool = False,
    ) -> WalmartObservation:
        return WalmartObservation(
            source="json",
            stock=stock,
            price=price,
            quantity=quantity,
            exact_item_match=True,
            confidence=0.98,
            details={"internal_conflict": internal_conflict},
        )

    @staticmethod
    def visual_observation(
        stock: StockState,
        *,
        price: Decimal | None = None,
        quantity: int | None = None,
        internal_conflict: bool = False,
    ) -> WalmartObservation:
        return WalmartObservation(
            source="visual",
            stock=stock,
            price=price,
            quantity=quantity,
            title_match=True,
            confidence=0.98,
            details={"internal_conflict": internal_conflict},
        )

    def test_matching_in_stock_and_price_is_verified(self):
        result = self.resolve(
            self.json_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
            self.visual_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.sheet_stock, "In Stock")
        self.assertEqual(result.price, Decimal("20.00"))
        self.assertTrue(
            result.metadata["walmart_price_change_requires_review"]
        )

    def test_in_stock_price_disagreement_is_conflict(self):
        result = self.resolve(
            self.json_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
            self.visual_observation(
                StockState.IN_STOCK,
                price=Decimal("25.00"),
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)
        self.assertIsNone(result.sheet_stock)

    def test_in_stock_missing_one_price_is_partial(self):
        result = self.resolve(
            self.json_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
            self.visual_observation(
                StockState.IN_STOCK,
                price=None,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.PARTIAL)
        self.assertIsNone(result.sheet_stock)

    def test_matching_oos_is_verified_without_price(self):
        result = self.resolve(
            self.json_observation(StockState.OOS),
            self.visual_observation(StockState.OOS),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.sheet_stock, "OOS")
        self.assertIsNone(result.price)

    def test_json_oos_visual_in_stock_is_conflict(self):
        result = self.resolve(
            self.json_observation(
                StockState.OOS,
                price=Decimal("20.00"),
            ),
            self.visual_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)
        self.assertIsNone(result.sheet_stock)

    def test_json_in_stock_visual_oos_is_conflict(self):
        result = self.resolve(
            self.json_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
            self.visual_observation(StockState.OOS),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)
        self.assertIsNone(result.sheet_stock)

    def test_low_stock_must_agree_exactly(self):
        result = self.resolve(
            self.json_observation(
                StockState.LOW_STOCK,
                price=Decimal("20.00"),
            ),
            self.visual_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)
        self.assertIsNone(result.sheet_stock)

    def test_matching_low_stock_is_verified_and_treated_as_oos(self):
        result = self.resolve(
            self.json_observation(
                StockState.LOW_STOCK,
                price=Decimal("20.00"),
            ),
            self.visual_observation(
                StockState.LOW_STOCK,
                price=Decimal("20.00"),
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.observed_stock, StockState.LOW_STOCK)
        self.assertEqual(result.sheet_stock, "OOS")
        self.assertIsNone(result.price)

    def test_matching_quantity_remaining_is_oos(self):
        result = self.resolve(
            self.json_observation(
                StockState.QUANTITY_REMAINING,
                quantity=1,
                price=Decimal("20.00"),
            ),
            self.visual_observation(
                StockState.QUANTITY_REMAINING,
                quantity=1,
                price=Decimal("20.00"),
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.VERIFIED)
        self.assertEqual(result.sheet_stock, "OOS")
        self.assertEqual(result.quantity, 1)

    def test_internal_json_conflict_is_not_overridden(self):
        result = self.resolve(
            self.json_observation(
                StockState.UNKNOWN,
                internal_conflict=True,
            ),
            self.visual_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)
        self.assertIsNone(result.sheet_stock)

    def test_internal_visual_conflict_is_not_overridden(self):
        result = self.resolve(
            self.json_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
            self.visual_observation(
                StockState.UNKNOWN,
                internal_conflict=True,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.CONFLICT)
        self.assertIsNone(result.sheet_stock)

    def test_json_only_is_partial(self):
        result = self.resolve(
            self.json_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
            WalmartObservation(source="visual"),
        )
        self.assertEqual(result.verification, VerificationStatus.PARTIAL)
        self.assertIsNone(result.sheet_stock)

    def test_visual_only_is_partial(self):
        result = self.resolve(
            WalmartObservation(source="json"),
            self.visual_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.PARTIAL)
        self.assertIsNone(result.sheet_stock)

    def test_missing_exact_item_anchor_is_partial(self):
        result = self.resolve(
            self.json_observation(
                StockState.IN_STOCK,
                price=Decimal("20.00"),
            ),
            WalmartObservation(
                source="visual",
                stock=StockState.IN_STOCK,
                price=Decimal("20.00"),
                title_match=False,
            ),
        )
        self.assertEqual(result.verification, VerificationStatus.PARTIAL)
        self.assertIsNone(result.sheet_stock)

    def test_enabled_cta_ignores_fulfillment_only_oos(self):
        stock, quantity, reason, conflict, scope = self.classify(
            enabledCta=True,
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

    def test_enabled_cta_and_product_oos_is_conflict(self):
        stock, _, _, conflict, scope = self.classify(
            enabledCta=True,
            productOos=True,
        )
        self.assertEqual(stock, StockState.UNKNOWN)
        self.assertTrue(conflict)
        self.assertEqual(scope, "product")

    def test_one_unavailable_fulfillment_method_is_unknown(self):
        stock, _, reason, conflict, scope = self.classify(
            fulfillment={
                "delivery": {"state": "UNAVAILABLE"},
            },
        )
        self.assertEqual(stock, StockState.UNKNOWN)
        self.assertFalse(conflict)
        self.assertEqual(scope, "")
        self.assertIn("alone", reason)

    def test_all_fulfillment_methods_unavailable_is_oos(self):
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

    def test_missing_exact_item_anchor_is_unknown(self):
        stock, _, reason, conflict, scope = self.classify(
            exactItemAnchor=False,
            enabledCta=True,
        )
        self.assertEqual(stock, StockState.UNKNOWN)
        self.assertFalse(conflict)
        self.assertEqual(scope, "")
        self.assertIn("requested item", reason)

    def test_inventory_parser_keeps_low_stock(self):
        stock, quantity = parse_inventory_detail(
            "Shipping arrives tomorrow. Low stock",
            available=True,
        )
        self.assertEqual(stock, StockState.LOW_STOCK)
        self.assertIsNone(quantity)

    def test_inventory_parser_keeps_quantity(self):
        stock, quantity = parse_inventory_detail(
            "Only 6 remaining",
            available=True,
        )
        self.assertEqual(stock, StockState.QUANTITY_REMAINING)
        self.assertEqual(quantity, 6)


if __name__ == "__main__":
    unittest.main()
