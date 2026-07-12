from core.scrape_result import (
    ScrapeResult,
    StockState,
    VerificationStatus,
)


def apply_stock_policy(
    result: ScrapeResult,
) -> ScrapeResult:
    if result.verification not in {
        VerificationStatus.VERIFIED,
        VerificationStatus.PARTIAL,
    }:
        result.sheet_stock = None
        return result

    if result.observed_stock == StockState.IN_STOCK:
        result.sheet_stock = "In Stock"
        result.policy_reason = (
            "Verified normal inventory."
        )
        return result

    if result.observed_stock == StockState.OOS:
        result.sheet_stock = "OOS"
        result.price = None
        result.policy_reason = (
            "Explicit product out-of-stock evidence."
        )
        return result

    if result.observed_stock == StockState.LOW_STOCK:
        result.sheet_stock = "OOS"
        result.price = None
        result.policy_reason = (
            "Low Stock is treated as OOS."
        )
        return result

    if result.observed_stock == StockState.LIMITED_STOCK:
        result.sheet_stock = "OOS"
        result.price = None
        result.policy_reason = (
            "Limited Stock is treated as OOS."
        )
        return result

    if (
        result.observed_stock
        == StockState.QUANTITY_REMAINING
    ):
        result.sheet_stock = "OOS"
        result.price = None
        result.policy_reason = (
            f"Only {result.quantity} remaining "
            "is treated as OOS."
        )
        return result

    result.sheet_stock = None
    result.policy_reason = (
        "Unknown inventory must not update the sheet."
    )
    return result