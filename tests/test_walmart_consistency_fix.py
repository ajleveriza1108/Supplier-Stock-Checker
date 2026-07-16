from decimal import Decimal
from pathlib import Path

from core.scrape_result import StockState, VerificationStatus
from scrapers.walmart_policy import WalmartObservation, WalmartPolicy


def _observation(*, source, stock, price=None, exact=False, visual=False, details=None):
    return WalmartObservation(
        source=source,
        stock=stock,
        price=Decimal(price) if price is not None else None,
        confidence=0.98,
        exact_item_match=exact,
        title_match=visual,
        reason=source,
        details=details or {},
    )


def test_in_stock_plus_low_stock_uses_specific_state():
    result = WalmartPolicy().resolve(
        url='u', item_id='1', title='t', final_url='u', target_variation='',
        json_observation=_observation(
            source='json', stock=StockState.IN_STOCK,
            price='10.00', exact=True,
        ),
        visual_observation=_observation(
            source='visual', stock=StockState.LOW_STOCK,
            price='10.00', visual=True,
        ),
    )
    assert result.verification == VerificationStatus.VERIFIED
    assert result.observed_stock == StockState.LOW_STOCK
    assert result.sheet_stock == 'OOS'


def test_price_disagreement_does_not_change_stock_consensus():
    result = WalmartPolicy().resolve(
        url='u', item_id='1', title='t', final_url='u', target_variation='',
        json_observation=_observation(
            source='json', stock=StockState.IN_STOCK,
            price='11.00', exact=True,
        ),
        visual_observation=_observation(
            source='visual', stock=StockState.IN_STOCK,
            price='10.00', visual=True,
        ),
    )
    assert result.verification == VerificationStatus.VERIFIED
    assert result.sheet_stock == 'In Stock'
    assert result.metadata['walmart_price_sources_agree'] is False


def test_fulfillment_unavailable_does_not_override_enabled_primary_cta():
    stock, _, _, conflict, _ = WalmartPolicy.classify_rendered_signals({
        'regionFound': True,
        'exactItemAnchor': True,
        'enabledCta': True,
        'disabledCta': False,
        'selectedOptionOos': False,
        'productOos': False,
        'inventoryText': '',
        'fulfillment': {
            'shipping': {'state': 'AVAILABLE'},
            'pickup': {'state': 'UNAVAILABLE'},
            'delivery': {'state': 'UNAVAILABLE'},
        },
    })
    assert stock == StockState.IN_STOCK
    assert conflict is False


def test_variant_oos_above_purchase_anchor_is_not_product_oos_source_rule():
    source = Path('scrapers/walmart.py').read_text(encoding='utf-8')
    assert 'y >= anchorY + 2' in source


def test_secondary_dom_probe_is_removed():
    source = Path('scrapers/walmart_doublecheck.py').read_text(encoding='utf-8')
    assert '_probe_primary_unavailable_state' not in source
    assert 'walmart_secondary_dom_probe": False' in source


def test_low_stock_inside_fulfillment_card_is_preserved():
    source = Path('scrapers/walmart.py').read_text(encoding='utf-8')
    assert 'fulfillmentInventoryTexts' in source
    assert 'const inventoryTexts = [...fulfillmentInventoryTexts];' in source
