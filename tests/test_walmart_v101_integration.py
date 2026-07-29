from __future__ import annotations

from core.engine_guard import EngineResultGuard
from core.scraper_diagnostics import read_structured_result
from core.simple_runtime_log import build_runtime_row_summary
from scrapers.walmart import WalmartScraper


def _payload(item_id: str, *, title: str, price: str = "98.00", stock: str = "In Stock"):
    return {
        "status": "Success",
        "itemId": item_id,
        "title": title,
        "price": price,
        "stock": stock,
        "reason": "Exact linked item verified.",
        "evidence": {
            "pageUrl": f"https://www.walmart.com/ip/example/{item_id}",
            "dom": {
                "title": title,
                "priceText": f"${price}" if price else "",
                "enabledAdd": True,
                "cartControlLabel": f"Add to cart - {title}",
                "exactStockTexts": [],
                "fulfillment": {"shipping": "Shipping Arrives tomorrow"},
            },
        },
    }


def _legacy(item_id: str, title: str):
    scraper = WalmartScraper.__new__(WalmartScraper)
    url = f"https://www.walmart.com/ip/example/{item_id}"
    result = scraper._payload_to_result(url, "", _payload(item_id, title=title), [])
    return result, scraper._result_to_public_tuple(result)


def test_stock_only_tuple_keeps_internal_price_but_exposes_no_price() -> None:
    result, legacy = _legacy("746021606", "Expert Grill Heavy Duty Charcoal Grill")
    assert str(result.price) == "98.00"
    assert legacy[1] == ""
    assert legacy[4][0]["price"] == ""
    assert legacy[4][0]["_structured_result"]["price"] == "98.00"


def test_engine_guard_accepts_verified_stock_but_not_walmart_price() -> None:
    _result, legacy = _legacy("746021606", "Expert Grill Heavy Duty Charcoal Grill")
    status, price, stock, title, variants, _logs = legacy
    decision = EngineResultGuard().evaluate(
        supplier="WAL",
        url="https://www.walmart.com/ip/example/746021606",
        row_num=80,
        price=price,
        stock=stock,
        status=status,
        title=title,
        variants=variants,
        is_verify=False,
    )
    assert decision.accept is True
    assert decision.stock == "In Stock"
    assert decision.price == ""


def test_runtime_summary_never_reports_walmart_price_change() -> None:
    _result, legacy = _legacy("746021606", "Expert Grill Heavy Duty Charcoal Grill")
    status, price, stock, title, variants, _logs = legacy
    row = [""] * 16
    row[2] = "OOS"
    row[15] = "$124.00"
    item = (
        "WAL",
        "https://www.walmart.com/ip/example/746021606",
        price,
        stock,
        status,
        title,
        row,
        80,
        variants,
        False,
    )
    summary = build_runtime_row_summary(item, {"stock_status": 2, "price": 15})
    assert "Stock OOS → In Stock" in summary.notice
    assert "Price" not in summary.notice


def test_sequential_items_keep_distinct_structured_identity() -> None:
    _result_a, legacy_a = _legacy("746021606", "Expert Grill Heavy Duty Charcoal Grill")
    _result_b, legacy_b = _legacy("803538514", "Expert Grill Three Burner Propane Gas Grill")
    structured_a = read_structured_result(legacy_a[4])
    structured_b = read_structured_result(legacy_b[4])
    assert structured_a is not None and structured_b is not None
    assert structured_a.item_id == "746021606"
    assert structured_b.item_id == "803538514"
    assert structured_a.item_id != structured_b.item_id


def test_live_conflict_is_public_review_not_error() -> None:
    from scrapers.walmart import _ResolvedSnapshot

    resolved = _ResolvedSnapshot(
        status="Error",
        stock="Unable to Verify",
        price="",
        title="Requested Walmart item",
        item_id="746021606",
        reason="Conflicting exact-item snapshots did not form consensus.",
        evidence={"page_url": "https://www.walmart.com/ip/example/746021606"},
    )
    result = WalmartScraper._resolved_to_result(
        url="https://www.walmart.com/ip/example/746021606",
        target_variation="",
        resolved=resolved,
    )
    legacy = WalmartScraper._result_to_public_tuple(result)
    assert result.verification.value == "CONFLICT"
    assert legacy[0] == "Review"
    assert legacy[1] == ""
    assert legacy[2] == "UNKNOWN"


def test_live_captcha_stays_error_and_never_updates() -> None:
    from scrapers.walmart import _ResolvedSnapshot

    resolved = _ResolvedSnapshot(
        status="Error",
        stock="Captcha",
        price="",
        title="Walmart item",
        item_id="746021606",
        reason="Walmart displayed a CAPTCHA human verification challenge.",
        evidence={},
    )
    result = WalmartScraper._resolved_to_result(
        url="https://www.walmart.com/ip/example/746021606",
        target_variation="",
        resolved=resolved,
    )
    legacy = WalmartScraper._result_to_public_tuple(result)
    assert result.verification.value == "BLOCKED"
    assert legacy[0] == "Error"
    assert legacy[1] == ""
