"""Walmart verification-pass scraper.

This class intentionally inherits all extraction, verification, business
policy, and structured logging from :mod:`scrapers.walmart`.  The only
difference is a longer render window for second-pass checks.
"""

from __future__ import annotations

from scrapers.walmart import WalmartScraper


class WalmartDoubleCheckScraper(WalmartScraper):
    def __init__(
        self,
        browser_manager=None,
        logger_func=None,
        **kwargs,
    ) -> None:
        super().__init__(
            browser_manager=browser_manager,
            logger_func=logger_func,
            render_timeout=18,
            post_ready_delay=2.5,
            log_prefix="WAL-DC",
            **kwargs,
        )
