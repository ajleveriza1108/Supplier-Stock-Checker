WALMART FINAL CONSISTENCY FIX 2026.07.16.4

Replace:
  scrapers/walmart.py
  scrapers/walmart_policy.py
  scrapers/walmart_doublecheck.py

Fixes:
- Alternative variant, Open Box, and other-offer OOS text no longer overrides
  a valid selected-item Add to cart button with available fulfillment.
- If every detected fulfillment method is unavailable, a stale/unrelated CTA
  cannot create a false In Stock result.
- Fulfillment cards are scanned through the full status wrapper so Low stock,
  Limited stock, and Only N remaining are not lost behind Arrives/Today text.
- Walmart remains stock-only for automatic updates.

Validation:
  python -m py_compile scrapers\walmart.py scrapers\walmart_policy.py scrapers\walmart_doublecheck.py
  python -m pytest tests\test_walmart_v4_regressions.py -q

Keep Review Mode ON for the first live retest.
