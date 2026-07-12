"""Application-wide stock and update-safety policy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from core.scrape_result import ScrapeResult, StockState, VerificationStatus


@dataclass(frozen=True, slots=True)
class UpdateSafety:
    allowed: bool
    reason: str
    sheet_stock: Optional[str]
    price: Optional[Decimal]


def apply_stock_policy(result: ScrapeResult) -> ScrapeResult:
    """Apply business rules without changing the raw observed stock.

    Only VERIFIED results may produce sheet values.  Partial, conflicting,
    blocked, unknown, and error results keep ``sheet_stock`` unset.
    """

    result.sheet_stock = None

    if result.verification != VerificationStatus.VERIFIED:
        if not result.policy_reason:
            result.policy_reason = (
                f"{result.verification.value} result cannot update the sheet."
            )
        return result

    if result.observed_stock == StockState.IN_STOCK:
        if result.price is None:
            result.verification = VerificationStatus.ERROR
            result.error_message = (
                "Verified In Stock result has no trustworthy price."
            )
            result.policy_reason = result.error_message
            return result
        result.sheet_stock = "In Stock"
        result.policy_reason = "Verified normal inventory."
        return result

    if result.observed_stock == StockState.OOS:
        result.sheet_stock = "OOS"
        result.price = None
        result.policy_reason = "Explicit product out-of-stock evidence."
        return result

    if result.observed_stock == StockState.LOW_STOCK:
        result.sheet_stock = "OOS"
        result.price = None
        result.policy_reason = "Low Stock is treated as OOS."
        return result

    if result.observed_stock == StockState.LIMITED_STOCK:
        result.sheet_stock = "OOS"
        result.price = None
        result.policy_reason = "Limited Stock is treated as OOS."
        return result

    if result.observed_stock == StockState.QUANTITY_REMAINING:
        result.sheet_stock = "OOS"
        result.price = None
        quantity_text = (
            str(result.quantity)
            if result.quantity is not None
            else "an explicitly limited quantity"
        )
        result.policy_reason = (
            f"Only {quantity_text} remaining is treated as OOS."
        )
        return result

    result.verification = VerificationStatus.UNKNOWN
    result.policy_reason = "Unknown inventory must not update the sheet."
    return result


def evaluate_update_safety(result: ScrapeResult) -> UpdateSafety:
    """Return the final sheet-update gate decision."""

    if result.verification != VerificationStatus.VERIFIED:
        return UpdateSafety(
            allowed=False,
            reason=(
                f"Verification is {result.verification.value}; "
                "manual review is required."
            ),
            sheet_stock=None,
            price=None,
        )

    if result.sheet_stock not in {"In Stock", "OOS"}:
        return UpdateSafety(
            allowed=False,
            reason="No valid sheet stock was produced.",
            sheet_stock=None,
            price=None,
        )

    if result.sheet_stock == "In Stock" and result.price is None:
        return UpdateSafety(
            allowed=False,
            reason="In Stock requires a verified price.",
            sheet_stock=None,
            price=None,
        )

    if result.sheet_stock == "OOS" and result.price is not None:
        return UpdateSafety(
            allowed=False,
            reason="OOS must not carry a sheet price.",
            sheet_stock=None,
            price=None,
        )

    return UpdateSafety(
        allowed=True,
        reason=result.policy_reason or "Verified result.",
        sheet_stock=result.sheet_stock,
        price=result.price,
    )


def is_low_inventory_state(stock: StockState) -> bool:
    return stock in {
        StockState.LOW_STOCK,
        StockState.LIMITED_STOCK,
        StockState.QUANTITY_REMAINING,
    }
