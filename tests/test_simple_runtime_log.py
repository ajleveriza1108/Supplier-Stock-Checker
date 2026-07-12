from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.simple_runtime_log import (
    SimpleRuntimeLogQueue,
    build_runtime_row_summary,
    mark_simple_log,
    normalize_sheet_stock,
)


SHEET_COLS = {
    "stock_status": 2,
    "price": 15,
}


def make_row(
    *,
    stock: str = "",
    price: str = "",
):
    row = [""] * 16
    row[2] = stock
    row[15] = price
    return row


def make_item(
    *,
    row_number: int = 80,
    url: str = "https://www.walmart.com/ip/example/123",
    price: str = "$98.00",
    stock: str = "In Stock",
    status: str = "Success",
    row=None,
    verification: str = "VERIFIED",
    observed_stock: str = "IN_STOCK",
    sheet_stock: str = "In Stock",
    quantity=None,
):
    variants = [
        {
            "label": "Default",
            "_verification": verification,
            "_observed_stock": observed_stock,
            "_sheet_stock": sheet_stock,
            "_quantity": quantity,
        }
    ]
    return (
        "WAL",
        url,
        price,
        stock,
        status,
        "Example",
        row if row is not None else make_row(),
        row_number,
        variants,
        False,
    )


class SimpleRuntimeLogTests(unittest.TestCase):
    def test_empty_sheet_cell_means_in_stock(self):
        self.assertEqual(
            normalize_sheet_stock(""),
            "In Stock",
        )

    def test_verified_no_change(self):
        summary = build_runtime_row_summary(
            make_item(
                row=make_row(stock="", price="$98.00"),
            ),
            SHEET_COLS,
        )
        self.assertEqual(summary.sheet_status, "In Stock")
        self.assertEqual(summary.link_status, "In Stock")
        self.assertEqual(summary.comparison, "Same")
        self.assertEqual(summary.notice, "NO CHANGE")

    def test_low_stock_is_displayed_as_limited_stock(self):
        summary = build_runtime_row_summary(
            make_item(
                stock="OOS",
                price="",
                row=make_row(stock="OOS", price="$17.87"),
                observed_stock="LOW_STOCK",
                sheet_stock="OOS",
            ),
            SHEET_COLS,
        )
        self.assertEqual(summary.link_status, "Limited Stock")
        self.assertIn("treated as OOS", summary.comparison)
        self.assertEqual(summary.notice, "NO CHANGE")

    def test_quantity_remaining_keeps_quantity(self):
        summary = build_runtime_row_summary(
            make_item(
                stock="OOS",
                price="",
                row=make_row(stock="OOS", price="$17.99"),
                observed_stock="QUANTITY_REMAINING",
                sheet_stock="OOS",
                quantity=1,
            ),
            SHEET_COLS,
        )
        self.assertEqual(
            summary.link_status,
            "Only 1 Remaining",
        )
        self.assertIn("treated as OOS", summary.comparison)

    def test_stock_and_price_change_notice(self):
        summary = build_runtime_row_summary(
            make_item(
                price="$98.00",
                row=make_row(stock="OOS", price="$124.00"),
            ),
            SHEET_COLS,
            pending_review=True,
        )
        self.assertEqual(summary.comparison, "Different")
        self.assertIn(
            "Stock OOS → In Stock",
            summary.notice,
        )
        self.assertIn(
            "Price $124.00 → $98.00",
            summary.notice,
        )
        self.assertIn(
            "HELD FOR REVIEW",
            summary.notice,
        )

    def test_conflict_never_reports_a_change(self):
        summary = build_runtime_row_summary(
            make_item(
                price="",
                stock="UNKNOWN",
                status="Review",
                row=make_row(stock="", price="$59.00"),
                verification="CONFLICT",
                observed_stock="UNKNOWN",
                sheet_stock="",
            ),
            SHEET_COLS,
        )
        self.assertEqual(
            summary.comparison,
            "Not Compared",
        )
        self.assertIn(
            "NO SHEET CHANGE",
            summary.notice,
        )

    def test_queue_suppresses_technical_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_queue = SimpleRuntimeLogQueue(
                diagnostics_dir=Path(temp_dir),
            )
            runtime_queue.put(
                (
                    "LOG",
                    "WAL-DC: JSON -> Stock=IN_STOCK",
                    "info",
                    "https://example.com",
                )
            )
            self.assertTrue(runtime_queue.empty())
            self.assertTrue(runtime_queue.diagnostic_path.exists())

    def test_queue_allows_marked_simple_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_queue = SimpleRuntimeLogQueue(
                diagnostics_dir=Path(temp_dir),
            )
            runtime_queue.put(
                (
                    "LOG",
                    mark_simple_log("Row: 80"),
                    "info",
                    "https://example.com",
                )
            )
            event = runtime_queue.get_nowait()
            self.assertEqual(event[1], "Row: 80")


if __name__ == "__main__":
    unittest.main()
