"""Regression tests for the final Walmart row-374 safety gate."""

from __future__ import annotations

from decimal import Decimal

from core.scrape_result import ScrapeResult, StockState, VerificationStatus
from scrapers.walmart_doublecheck import WalmartDoubleCheckScraper


def _result(
    *,
    stock: StockState,
    sheet_stock: str | None,
    verification: VerificationStatus = VerificationStatus.VERIFIED,
) -> ScrapeResult:
    return ScrapeResult(
        supplier="WAL",
        url="https://www.walmart.com/ip/test/123",
        verification=verification,
        observed_stock=stock,
        price=Decimal("10.00") if sheet_stock == "In Stock" else None,
        title="Test product",
        item_id="123",
        sheet_stock=sheet_stock,
    )


def test_probe_runs_only_for_verified_normal_in_stock() -> None:
    assert WalmartDoubleCheckScraper._needs_primary_unavailable_probe(
        _result(stock=StockState.IN_STOCK, sheet_stock="In Stock")
    )

    assert not WalmartDoubleCheckScraper._needs_primary_unavailable_probe(
        _result(stock=StockState.OOS, sheet_stock="OOS")
    )

    assert not WalmartDoubleCheckScraper._needs_primary_unavailable_probe(
        _result(
            stock=StockState.LOW_STOCK,
            sheet_stock="OOS",
        )
    )

    assert not WalmartDoubleCheckScraper._needs_primary_unavailable_probe(
        _result(
            stock=StockState.IN_STOCK,
            sheet_stock=None,
            verification=VerificationStatus.CONFLICT,
        )
    )


def test_row374_standalone_not_available_is_rejected() -> None:
    reason = WalmartDoubleCheckScraper._rejection_reason_from_probe(
        {
            "probe_completed": True,
            "standalone_unavailable": True,
            "standalone_unavailable_texts": ["Not Available"],
            "unavailable_count": 3,
            "available_count": 0,
            "valid_primary_add_to_cart": True,
        }
    )

    assert reason
    assert "explicitly reports" in reason


def test_row374_all_fulfillment_unavailable_is_rejected() -> None:
    reason = WalmartDoubleCheckScraper._rejection_reason_from_probe(
        {
            "probe_completed": True,
            "standalone_unavailable": False,
            "unavailable_count": 3,
            "available_count": 0,
            "valid_primary_add_to_cart": True,
        }
    )

    assert reason
    assert "multiple unavailable fulfillment methods" in reason


def test_correct_in_stock_with_shipping_available_is_not_rejected() -> None:
    reason = WalmartDoubleCheckScraper._rejection_reason_from_probe(
        {
            "probe_completed": True,
            "standalone_unavailable": False,
            "unavailable_count": 2,
            "available_count": 1,
            "valid_primary_add_to_cart": True,
        }
    )

    assert reason == ""


def test_one_unavailable_method_does_not_break_correct_rows() -> None:
    reason = WalmartDoubleCheckScraper._rejection_reason_from_probe(
        {
            "probe_completed": True,
            "standalone_unavailable": False,
            "unavailable_count": 1,
            "available_count": 0,
            "unknown_count": 2,
            "valid_primary_add_to_cart": True,
        }
    )

    assert reason == ""


def test_inconclusive_probe_does_not_change_existing_verified_result() -> None:
    reason = WalmartDoubleCheckScraper._rejection_reason_from_probe(
        {
            "probe_completed": False,
            "reason": "Browser probe was unavailable.",
        }
    )

    assert reason == ""


def test_normal_oos_and_low_inventory_rows_are_outside_guard_scope() -> None:
    cases = [
        _result(stock=StockState.OOS, sheet_stock="OOS"),
        _result(stock=StockState.LOW_STOCK, sheet_stock="OOS"),
        _result(stock=StockState.LIMITED_STOCK, sheet_stock="OOS"),
        _result(stock=StockState.QUANTITY_REMAINING, sheet_stock="OOS"),
    ]

    assert all(
        not WalmartDoubleCheckScraper._needs_primary_unavailable_probe(case)
        for case in cases
    )
