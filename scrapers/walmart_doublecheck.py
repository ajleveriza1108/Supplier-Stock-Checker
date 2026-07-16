"""Walmart compatibility wrapper.

The engine instantiates ``WalmartDoubleCheckScraper`` for Walmart rows.
All stock decisions now come from the single structured ``WalmartScraper``
implementation and ``WalmartPolicy``.

This wrapper intentionally does not run another DOM probe. The previous extra
probe scanned fulfillment text a second time and incorrectly promoted messages
such as "Pickup: Not available" or "Delivery: Not available" to whole-product
unavailability.

Walmart remains stock-only for automatic updates. Prices are retained only as
internal evidence and are removed from the legacy tuple before the result
reaches the update engine.
"""

from __future__ import annotations

from typing import Optional

from core.scraper_diagnostics import result_to_legacy_tuple
from scrapers.walmart import WalmartScraper


WALMART_FINAL_GUARD_VERSION = "2026.07.16.5"


class WalmartDoubleCheckScraper(WalmartScraper):
    """Use the unified Walmart consensus scraper without a second DOM scan."""

    def __init__(
        self,
        browser_manager=None,
        logger_func=None,
        **kwargs,
    ) -> None:
        kwargs.pop("render_timeout", None)
        kwargs.pop("post_ready_delay", None)
        kwargs.pop("log_prefix", None)

        super().__init__(
            browser_manager=browser_manager,
            logger_func=logger_func,
            render_timeout=18,
            post_ready_delay=2.5,
            log_prefix="WAL",
            **kwargs,
        )

    def scrape(
        self,
        url: str,
        target_variation: Optional[str] = None,
    ):
        logs: list[tuple[str, str, str]] = []

        result = super().scrape_structured(
            url,
            target_variation=target_variation,
            logs=logs,
        )

        result.metadata.update(
            {
                "walmart_stock_only_mode": True,
                "automatic_walmart_price_updates": False,
                "manual_review_walmart_price_changes": True,
                "walmart_final_guard_version": (
                    WALMART_FINAL_GUARD_VERSION
                ),
                "walmart_secondary_dom_probe": False,
            }
        )

        with self._result_lock:
            self._last_results[url] = result

        (
            status,
            _price,
            stock,
            title,
            variants,
            output_logs,
        ) = result_to_legacy_tuple(
            result,
            target_variation=target_variation,
            logs=logs,
        )

        # Never expose Walmart prices as proposed automatic updates.
        for variant in variants or []:
            if isinstance(variant, dict):
                variant["price"] = ""

        output_logs.append(
            (
                (
                    "WAL: Unified stock-consensus mode active. "
                    "No secondary DOM probe and no automatic "
                    "Walmart price update."
                ),
                "info",
                url,
            )
        )

        return (
            status,
            "",
            stock,
            title,
            variants,
            output_logs,
        )
