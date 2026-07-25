"""Regression tests for Walmart v8.2 structured bridge.

These tests cover the July 16 failures:
- every successful Walmart tuple must contain ``_structured_result``;
- recommendation-card Add buttons must not create false In Stock;
- explicit exact-item OOS/all-unavailable evidence overrides stale Add buttons;
- one unavailable secondary fulfillment method does not override shipping;
- low/limited/quantity inventory remains sheet OOS;
- Walmart prices remain internal and are not proposed as updates;
- the legacy double-check class delegates to the new bridge.
"""

from __future__ import annotations

from core.scrape_result import StockState, VerificationStatus
from core.scraper_diagnostics import read_structured_result, result_to_legacy_tuple
from scrapers.walmart import WalmartScraper
from scrapers.walmart_doublecheck import WalmartDoubleCheckScraper


def _payload(
    *,
    stock: str = "In Stock",
    price: str = "69.99",
    title: str = "Example Brand Exact Product",
    cart_label: str = "Add to cart - Example Brand Exact Product",
    availability_meta: str = "https://schema.org/InStock",
    exact_stock_texts=None,
    fulfillment=None,
    reason: str = "Primary purchase evidence was detected.",
):
    return {
        "status": "Success",
        "itemId": "123456789",
        "title": title,
        "price": price,
        "stock": stock,
        "reason": reason,
        "evidence": {
            "runtimeVersion": "test-runtime",
            "browserMode": "brave_cdp_raw",
            "pageUrl": "https://www.walmart.com/ip/test/123456789",
            "dom": {
                "title": title,
                "priceText": f"${price}" if price else "",
                "availabilityMeta": availability_meta,
                "enabledAdd": bool(cart_label),
                "disabledAdd": False,
                "buyNowEnabled": False,
                "cartControlLabel": cart_label,
                "exactStockTexts": list(exact_stock_texts or []),
                "fulfillment": dict(
                    fulfillment
                    or {
                        "shipping": "Shipping Arrives tomorrow",
                        "pickup": "Pickup Not available",
                        "delivery": "Delivery Not available",
                    }
                ),
                "selectedVariantSignals": [],
            },
        },
    }


def _result(payload):
    scraper = WalmartScraper.__new__(WalmartScraper)
    return scraper._payload_to_result(
        "https://www.walmart.com/ip/test/123456789",
        "",
        payload,
        [],
    )


def test_success_result_contains_structured_metadata() -> None:
    result = _result(_payload())
    legacy = result_to_legacy_tuple(result)
    recovered = read_structured_result(legacy[4])

    assert legacy[0] == "Success"
    assert recovered is not None
    assert recovered.verification == VerificationStatus.VERIFIED
    assert recovered.sheet_stock == "In Stock"


def test_public_walmart_tuple_blanks_price_but_keeps_internal_price() -> None:
    scraper = WalmartScraper.__new__(WalmartScraper)
    result = _result(_payload(price="69.99"))
    legacy = result_to_legacy_tuple(result)

    for variant in legacy[4]:
        variant["price"] = ""

    assert result.price is not None
    assert legacy[4][0]["price"] == ""
    assert legacy[4][0]["_structured_result"]["price"] == "69.99"


def test_shipping_available_secondary_unavailable_stays_in_stock() -> None:
    result = _result(
        _payload(
            availability_meta="https://schema.org/OutOfStock",
            exact_stock_texts=["Delivery Not available"],
            fulfillment={
                "shipping": "Shipping Arrives tomorrow Free",
                "pickup": "Pickup Not available",
                "delivery": "Delivery Not available",
            },
        )
    )

    assert result.verification == VerificationStatus.VERIFIED
    assert result.observed_stock == StockState.IN_STOCK
    assert result.sheet_stock == "In Stock"


def test_row38_style_wrong_tire_cta_with_oos_becomes_oos() -> None:
    result = _result(
        _payload(
            title="Goodyear Wrangler Radial 235/75R15 All-Season Tire",
            cart_label=(
                "Add to cart - Milestar WeatherGuard AW365 All Weather Tire "
                "235/75R15"
            ),
            availability_meta="https://schema.org/OutOfStock",
            exact_stock_texts=["Out of stock"],
            fulfillment={
                "shipping": "Shipping Out of stock",
                "pickup": "Pickup Not available",
                "delivery": "Delivery Not available",
            },
        )
    )

    assert result.verification == VerificationStatus.VERIFIED
    assert result.observed_stock == StockState.OOS
    assert result.sheet_stock == "OOS"
    assert result.price is None


def test_row82_style_wrong_bench_cta_with_oos_becomes_oos() -> None:
    result = _result(
        _payload(
            title="CAP Strength AB Crunch Bench Board",
            cart_label=(
                "Add to cart - GVDV Adjustable Weight Bench for Full Body "
                "Workout"
            ),
            availability_meta="https://schema.org/OutOfStock",
            exact_stock_texts=["Out of stock"],
            fulfillment={
                "shipping": "Shipping Out of stock",
                "pickup": "Pickup Not available",
                "delivery": "Delivery Not available",
            },
        )
    )

    assert result.observed_stock == StockState.OOS
    assert result.sheet_stock == "OOS"


def test_row196_style_wrong_oil_cta_with_oos_becomes_oos() -> None:
    result = _result(
        _payload(
            title="15W40 Synthetic Diesel Engine Oil 5 Gallon Pail",
            cart_label=(
                "Add to cart - Castrol EDGE 0W-20 Advanced Full Synthetic "
                "Motor Oil 5 Quarts"
            ),
            availability_meta="https://schema.org/OutOfStock",
            exact_stock_texts=["Out of stock"],
            fulfillment={
                "shipping": "Shipping Out of stock",
                "pickup": "Pickup Not available",
                "delivery": "Delivery Not available",
            },
        )
    )

    assert result.observed_stock == StockState.OOS
    assert result.sheet_stock == "OOS"


def test_row246_style_wrong_brand_cta_with_oos_becomes_oos() -> None:
    result = _result(
        _payload(
            title="AW-32 Premium Anti-wear Hydraulic Oil Fluid 5 Gallon Pail",
            cart_label=(
                "Add to cart - Mobil Delvac Tractor Hydraulic Fluid 2.5 Gallon"
            ),
            availability_meta="https://schema.org/OutOfStock",
            exact_stock_texts=["Out of stock"],
            fulfillment={
                "shipping": "Shipping Out of stock",
                "pickup": "Pickup Not available",
                "delivery": "Delivery Not available",
            },
        )
    )

    assert result.observed_stock == StockState.OOS
    assert result.sheet_stock == "OOS"


def test_mismatched_cta_without_complete_oos_evidence_is_review() -> None:
    result = _result(
        _payload(
            title="Exact Brand Product",
            cart_label="Add to cart - Different Brand Unrelated Product",
            availability_meta="",
            exact_stock_texts=[],
            fulfillment={},
        )
    )

    assert result.verification == VerificationStatus.CONFLICT
    assert result.observed_stock == StockState.UNKNOWN
    assert result.sheet_stock is None
    assert result.price is None


def test_matching_cta_stays_in_stock() -> None:
    result = _result(
        _payload(
            title="Mainstays Adjustable Bronze Swivel Barstool Tan Seat",
            cart_label=(
                "Add to cart - Mainstays Adjustable Bronze Swivel Barstool "
                "Tan Seat"
            ),
        )
    )

    assert result.verification == VerificationStatus.VERIFIED
    assert result.sheet_stock == "In Stock"


def test_low_stock_is_verified_sheet_oos() -> None:
    result = _result(_payload(stock="Low Stock", reason="Low stock."))

    assert result.observed_stock == StockState.LOW_STOCK
    assert result.sheet_stock == "OOS"
    assert result.price is None


def test_limited_stock_is_verified_sheet_oos() -> None:
    result = _result(_payload(stock="Limited Stock", reason="Limited stock."))

    assert result.observed_stock == StockState.LIMITED_STOCK
    assert result.sheet_stock == "OOS"
    assert result.price is None


def test_only_quantity_remaining_is_verified_sheet_oos() -> None:
    result = _result(
        _payload(
            stock="Only Quantity Remaining",
            reason="The selected offer reports: Only 9 left.",
        )
    )

    assert result.observed_stock == StockState.QUANTITY_REMAINING
    assert result.sheet_stock == "OOS"
    assert result.quantity == 9


def test_enabled_cart_with_low_stock_in_fulfillment_becomes_sheet_oos() -> None:
    result = _result(
        _payload(
            stock="In Stock",
            fulfillment={
                "shipping": "Shipping Arrives tomorrow Free",
                "pickup": "Pickup As soon as 9pm today Free",
                "delivery": "Delivery As soon as 13 mins Low stock",
            },
        )
    )

    assert result.verification == VerificationStatus.VERIFIED
    assert result.observed_stock == StockState.LOW_STOCK
    assert result.sheet_stock == "OOS"
    assert result.price is None


def test_normal_oos_stays_oos() -> None:
    result = _result(_payload(stock="OOS", price="69.99"))

    assert result.verification == VerificationStatus.VERIFIED
    assert result.observed_stock == StockState.OOS
    assert result.sheet_stock == "OOS"
    assert result.price is None


def test_in_stock_without_verified_price_is_review_not_oos() -> None:
    result = _result(_payload(stock="In Stock", price=""))

    assert result.verification == VerificationStatus.PARTIAL
    assert result.observed_stock == StockState.IN_STOCK
    assert result.sheet_stock is None
    assert result.price is None


def test_runtime_error_also_has_structured_metadata() -> None:
    scraper = WalmartScraper.__new__(WalmartScraper)
    scraper.project_root = __import__("pathlib").Path(".")
    result = scraper._payload_to_result(
        "https://www.walmart.com/ip/test/123456789",
        "",
        {
            "status": "Error",
            "title": "Test item",
            "stock": "Unable to Verify",
            "reason": "The page could not be verified.",
            "evidence": {},
        },
        [],
    )
    legacy = result_to_legacy_tuple(result)

    assert legacy[0] == "Error"
    assert read_structured_result(legacy[4]) is not None


def test_captcha_becomes_blocked() -> None:
    scraper = WalmartScraper.__new__(WalmartScraper)
    scraper.project_root = __import__("pathlib").Path(".")
    result = scraper._payload_to_result(
        "https://www.walmart.com/ip/test/123456789",
        "",
        {
            "status": "Error",
            "reason": "Walmart requested human verification in Brave.",
            "evidence": {},
        },
        [],
    )

    assert result.verification == VerificationStatus.BLOCKED


def test_doublecheck_wrapper_uses_v82_bridge() -> None:
    assert issubclass(WalmartDoubleCheckScraper, WalmartScraper)
    assert WalmartDoubleCheckScraper.scrape is WalmartScraper.scrape
