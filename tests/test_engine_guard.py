from decimal import Decimal
import unittest

from core.engine_guard import EngineResultGuard
from core.result_policy import apply_stock_policy
from core.scrape_result import ScrapeResult, StockState, VerificationStatus
from core.scraper_diagnostics import structured_metadata


class EngineGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = EngineResultGuard()

    def variants_for(self, result):
        return [
            {
                "label": "Default",
                "price": "",
                "stock": result.sheet_stock or "UNKNOWN",
                **structured_metadata(result),
            }
        ]

    def test_verified_structured_result_is_accepted(self):
        result = ScrapeResult(
            supplier="WAL",
            url="https://www.walmart.com/ip/example/123",
            verification=VerificationStatus.VERIFIED,
            observed_stock=StockState.IN_STOCK,
            price=Decimal("10.00"),
        )
        apply_stock_policy(result)
        decision = self.guard.evaluate(
            supplier="WAL",
            url=result.url,
            row_num=10,
            price="$10.00",
            stock="In Stock",
            status="Success",
            title="Example",
            variants=self.variants_for(result),
            is_verify=False,
        )
        self.assertTrue(decision.accept)
        self.assertEqual(decision.stock, "In Stock")

    def test_partial_result_is_rejected(self):
        result = ScrapeResult(
            supplier="WAL",
            url="https://www.walmart.com/ip/example/123",
            verification=VerificationStatus.PARTIAL,
            observed_stock=StockState.IN_STOCK,
            price=Decimal("10.00"),
            policy_reason="Visual confirmation missing.",
        )
        apply_stock_policy(result)
        decision = self.guard.evaluate(
            supplier="WAL",
            url=result.url,
            row_num=10,
            price="",
            stock="UNKNOWN",
            status="Review",
            title="Example",
            variants=self.variants_for(result),
            is_verify=False,
        )
        self.assertFalse(decision.accept)

    def test_partial_phase2_result_is_forwarded_as_error(self):
        result = ScrapeResult(
            supplier="WAL",
            url="https://www.walmart.com/ip/example/123",
            verification=VerificationStatus.PARTIAL,
            observed_stock=StockState.IN_STOCK,
            price=Decimal("10.00"),
            policy_reason="Visual confirmation missing.",
        )
        apply_stock_policy(result)
        decision = self.guard.evaluate(
            supplier="WAL",
            url=result.url,
            row_num=10,
            price="",
            stock="UNKNOWN",
            status="Review",
            title="Example",
            variants=self.variants_for(result),
            is_verify=True,
        )
        self.assertTrue(decision.accept)
        self.assertEqual(decision.status, "Error")
        self.assertEqual(decision.stock, "UNKNOWN")

    def test_walmart_success_without_metadata_is_rejected(self):
        decision = self.guard.evaluate(
            supplier="WAL",
            url="https://www.walmart.com/ip/example/123",
            row_num=10,
            price="$10.00",
            stock="In Stock",
            status="Success",
            title="Example",
            variants=[],
            is_verify=False,
        )
        self.assertFalse(decision.accept)

    def test_other_supplier_legacy_result_passes(self):
        decision = self.guard.evaluate(
            supplier="HF",
            url="https://example.com/item",
            row_num=10,
            price="$10.00",
            stock="In Stock",
            status="Success",
            title="Example",
            variants=[],
            is_verify=False,
        )
        self.assertTrue(decision.accept)


if __name__ == "__main__":
    unittest.main()
