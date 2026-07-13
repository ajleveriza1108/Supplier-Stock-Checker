"""Compatibility wrapper for the conservative Walmart scraper.

The engine historically replaces the configured ``WAL`` scraper with
``WalmartDoubleCheckScraper``.  Keeping a second independent implementation
caused fixes in ``scrapers/walmart.py`` to be bypassed.  This wrapper now uses
the same exact-item consensus implementation so Walmart has one source of
truth.
"""

from __future__ import annotations

from scrapers.walmart import WalmartScraper


class WalmartDoubleCheckScraper(WalmartScraper):
    """Backward-compatible name for the single Walmart implementation."""

    pass
