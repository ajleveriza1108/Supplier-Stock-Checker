"""Compatibility wrapper for the unified Walmart exact-item scraper."""
from __future__ import annotations
from scrapers.walmart import WalmartScraper

WALMART_DOUBLECHECK_VERSION = "2026.07.25.exact-offer-structured-v10.1.0"

class WalmartDoubleCheckScraper(WalmartScraper):
    """Backward-compatible class name using the unified implementation."""
    pass
