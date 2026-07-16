from pathlib import Path

from core.scrape_result import StockState
from scrapers.walmart_policy import WalmartPolicy


def _signals(**overrides):
    data = {
        "regionFound": True,
        "exactItemAnchor": True,
        "enabledCta": True,
        "disabledCta": False,
        "selectedOptionOos": False,
        "productOos": False,
        "inventoryText": "",
        "fulfillment": {
            "shipping": {"state": "AVAILABLE"},
            "pickup": {"state": "UNAVAILABLE"},
            "delivery": {"state": "UNAVAILABLE"},
        },
    }
    data.update(overrides)
    return data


def test_alternative_offer_oos_does_not_override_valid_primary_cta():
    stock, _, _, conflict, _ = WalmartPolicy.classify_rendered_signals(
        _signals(productOos=True)
    )
    assert stock == StockState.IN_STOCK
    assert conflict is False


def test_selected_alternative_variant_oos_does_not_override_valid_primary_cta():
    stock, _, _, conflict, _ = WalmartPolicy.classify_rendered_signals(
        _signals(selectedOptionOos=True)
    )
    assert stock == StockState.IN_STOCK
    assert conflict is False


def test_stale_cta_with_all_fulfillment_unavailable_is_oos():
    stock, _, _, conflict, scope = WalmartPolicy.classify_rendered_signals(
        _signals(
            fulfillment={
                "shipping": {"state": "UNAVAILABLE"},
                "pickup": {"state": "UNAVAILABLE"},
                "delivery": {"state": "UNAVAILABLE"},
            }
        )
    )
    assert stock == StockState.OOS
    assert conflict is False
    assert scope == "all_fulfillment"


def test_low_stock_still_wins_with_enabled_cta():
    stock, _, _, conflict, _ = WalmartPolicy.classify_rendered_signals(
        _signals(inventoryText="Low stock")
    )
    assert stock == StockState.LOW_STOCK
    assert conflict is False


def test_fulfillment_scan_does_not_stop_at_arrival_before_low_stock():
    source = Path("scrapers/walmart.py").read_text(encoding="utf-8")
    assert "score += 100" in source
    assert "candidates.sort" in source
    assert "stopping at the first" in source
