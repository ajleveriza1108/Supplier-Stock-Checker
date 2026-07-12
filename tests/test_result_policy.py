from decimal import Decimal
import unittest

from core.result_policy import apply_stock_policy, evaluate_update_safety
from core.scrape_result import ScrapeResult, StockState, VerificationStatus


class ResultPolicyTests(unittest.TestCase):
    def make_result(self, stock, verification=VerificationStatus.VERIFIED):
        return ScrapeResult(
            supplier="WAL",
            url="https://www.walmart.com/ip/example/123",
            verification=verification,
            observed_stock=stock,
            price=Decimal("19.99"),
        )

    def test_verified_in_stock_requires_price(self):
        result = self.make_result(StockState.IN_STOCK)
        apply_stock_policy(result)
        self.assertEqual(result.sheet_stock, "In Stock")
        self.assertTrue(evaluate_update_safety(result).allowed)

    def test_low_stock_becomes_oos(self):
        result = self.make_result(StockState.LOW_STOCK)
        apply_stock_policy(result)
        self.assertEqual(result.sheet_stock, "OOS")
        self.assertIsNone(result.price)
        self.assertTrue(evaluate_update_safety(result).allowed)

    def test_limited_stock_becomes_oos(self):
        result = self.make_result(StockState.LIMITED_STOCK)
        apply_stock_policy(result)
        self.assertEqual(result.sheet_stock, "OOS")
        self.assertIsNone(result.price)

    def test_quantity_remaining_becomes_oos(self):
        result = self.make_result(StockState.QUANTITY_REMAINING)
        result.quantity = 3
        apply_stock_policy(result)
        self.assertEqual(result.sheet_stock, "OOS")
        self.assertIn("Only 3 remaining", result.policy_reason)

    def test_partial_result_never_updates(self):
        result = self.make_result(
            StockState.IN_STOCK,
            VerificationStatus.PARTIAL,
        )
        apply_stock_policy(result)
        self.assertIsNone(result.sheet_stock)
        self.assertFalse(evaluate_update_safety(result).allowed)

    def test_in_stock_without_price_becomes_error(self):
        result = self.make_result(StockState.IN_STOCK)
        result.price = None
        apply_stock_policy(result)
        self.assertEqual(result.verification, VerificationStatus.ERROR)
        self.assertFalse(evaluate_update_safety(result).allowed)


if __name__ == "__main__":
    unittest.main()
