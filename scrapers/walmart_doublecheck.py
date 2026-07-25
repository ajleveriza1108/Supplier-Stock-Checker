"""Compatibility wrapper for the Walmart v8.2 Node/Brave scraper.

The engine historically replaces the configured Walmart scraper with
``WalmartDoubleCheckScraper``.  Walmart v8 uses a Node/Brave CDP runtime and
already performs exact-item collection and final Python reconciliation, so the
wrapper must not call the removed legacy ``scrape_structured`` implementation.
"""

from __future__ import annotations

from scrapers.walmart import WalmartScraper


WALMART_DOUBLECHECK_VERSION = "2026.07.16.structured-bridge-v8.2"


class WalmartDoubleCheckScraper(WalmartScraper):
    """Backward-compatible class name using the v8.2 implementation."""

    pass
