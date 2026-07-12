"""Walmart verification-pass scraper."""

from __future__ import annotations

from scrapers.walmart import WalmartScraper


class WalmartDoubleCheckScraper(
    WalmartScraper
):
    def __init__(
        self,
        browser_manager=None,
        logger_func=None,
        **kwargs,
    ):
        super().__init__(
            browser_manager=browser_manager,
            logger_func=logger_func,
            render_timeout=16,
            post_ready_delay=2.0,
            log_prefix="WAL-DC",
            **kwargs,
        )