"""Phase 2 validator backed by the optional local AI verifier.

The deterministic supplier scraper remains the first source of evidence.
Local AI runs only after:
1. Phase 1 detected a sheet change.
2. Phase 2 repeated the supplier scrape.
3. Phase 1 and Phase 2 agree.

A model failure, malformed response, blocked page, or low-confidence answer
returns ``None`` so the change remains held for manual review.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional

from config.settings import SHEET_COLS
from core.local_ai_verifier import LocalAIChangeVerifier


class SuspicionValidator:
    """Queue changed rows and cross-check only those rows in Phase 2."""

    def __init__(self) -> None:
        self.pending_items: list[dict[str, Any]] = []
        self.pass1_data: dict[int, dict[str, Any]] = {}
        self.last_reason = ""
        self.last_result: dict[str, Any] | None = None
        self._lock = threading.RLock()
        self.verifier = LocalAIChangeVerifier()

    def flag(self, item_dict: dict[str, Any]) -> None:
        """Queue one changed row without creating duplicate row entries."""
        row_num = int(item_dict.get("row_num", -1))

        with self._lock:
            self.pending_items = [
                item
                for item in self.pending_items
                if int(item.get("row_num", -2)) != row_num
            ]
            self.pending_items.append(dict(item_dict))

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self.pending_items)

    def pop_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.pending_items)
            self.pending_items.clear()
            return items

    @staticmethod
    def _normalise_price(value: object) -> str:
        text = (
            str(value or "")
            .strip()
            .upper()
            .replace(",", "")
        )

        if text in {
            "",
            "N/A",
            "NA",
            "NONE",
            "NULL",
        }:
            return ""

        match = re.search(
            r"-?\d+(?:\.\d{1,2})?",
            text,
        )
        return match.group(0) if match else text

    @staticmethod
    def _normalise_stock(value: object) -> str:
        text = re.sub(
            r"\s+",
            " ",
            str(value or "").strip().upper(),
        )

        if (
            "LIMITED STOCK" in text
            or "LOW STOCK" in text
            or "BACKORDER" in text
            or re.search(
                r"\bONLY\s+\d+\s+"
                r"(?:REMAINING|LEFT|IN STOCK)\b",
                text,
            )
        ):
            return "OOS"

        aliases = {
            "INSTOCK": "IN STOCK",
            "AVAILABLE": "IN STOCK",
            "OUT OF STOCK": "OOS",
            "UNAVAILABLE": "OOS",
            "SOLD OUT": "OOS",
        }
        return aliases.get(text, text)

    def verify(
        self,
        row_num: int,
        url: str,
        price: str,
        stock: str,
        ai_mode: str,
        supplier: str = "",
        *,
        title: str = "",
        variants: list[Any] | None = None,
        row: list[Any] | None = None,
    ) -> Optional[bool]:
        """Confirm, reject, or abstain from one detected sheet change."""

        del ai_mode  # Retained for compatibility with the current engine.

        try:
            with self._lock:
                phase1 = dict(self.pass1_data.get(row_num) or {})

            if not phase1:
                self.last_reason = "Phase 1 data is missing."
                self.last_result = None
                return None

            phase1_price = self._normalise_price(
                phase1.get("price")
            )
            phase2_price = self._normalise_price(price)
            phase1_stock = self._normalise_stock(
                phase1.get("stock")
            )
            phase2_stock = self._normalise_stock(stock)

            # AI is never asked to rescue an inconsistent deterministic run.
            if (
                phase1_price != phase2_price
                or phase1_stock != phase2_stock
            ):
                self.last_reason = (
                    "Phase 1 and Phase 2 disagree: "
                    f"price {phase1_price!r} -> {phase2_price!r}, "
                    f"stock {phase1_stock!r} -> {phase2_stock!r}."
                )
                self.last_result = None
                return False

            sheet_row = list(row or [])
            stock_col = SHEET_COLS.get("stock_status", 2)
            price_col = SHEET_COLS.get("price", 15)
            variation_col = SHEET_COLS.get("variation", 8)

            previous_stock = (
                str(sheet_row[stock_col]).strip()
                if len(sheet_row) > stock_col
                else ""
            )
            previous_price = (
                str(sheet_row[price_col]).strip()
                if len(sheet_row) > price_col
                else ""
            )
            selected_variation = (
                str(sheet_row[variation_col]).strip()
                if len(sheet_row) > variation_col
                else ""
            )

            result = self.verifier.verify_change(
                row_num=row_num,
                supplier=supplier,
                url=url,
                detected_price=price,
                detected_stock=stock,
                previous_price=previous_price,
                previous_stock=previous_stock,
                title=title,
                selected_variation=selected_variation,
                variants=variants,
            )

            self.last_result = result.to_dict()
            self.last_reason = (
                f"Local AI: {result.verdict} "
                f"({result.confidence:.2f}) — {result.reason}"
            )

            if result.is_confirmed:
                return True

            if result.is_rejected:
                return False

            return None

        except Exception as exc:
            self.last_result = None
            self.last_reason = (
                "Validator error: "
                f"{type(exc).__name__}: {exc}"
            )
            return None
