"""Final Walmart verification-pass scraper.

This module adds one narrow safety gate after the normal Walmart consensus
scraper.  It exists to prevent a false In Stock update when Walmart's primary
product area explicitly says the item is unavailable while an unrelated,
stale, or recommendation-card Add to cart button is also present.

The guard is intentionally conservative:

* It runs only after the normal scraper returns VERIFIED + IN_STOCK.
* It never changes a correct OOS or low-inventory result.
* It never converts an uncertain page to OOS.
* Strong unavailable evidence converts the result to CONFLICT, which means
  no Google Sheet update.
* Walmart remains stock-only.  Prices are not exposed to the update engine.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from core.scrape_result import ScrapeResult, StockState, VerificationStatus
from core.scraper_diagnostics import result_to_legacy_tuple
from scrapers.walmart import WalmartScraper


WALMART_FINAL_GUARD_VERSION = "2026.07.13.7"


class WalmartDoubleCheckScraper(WalmartScraper):
    """Use the main Walmart scraper with a final primary-area safety gate."""

    def __init__(
        self,
        browser_manager=None,
        logger_func=None,
        **kwargs,
    ) -> None:
        # Remove caller-provided values that this verification pass owns.
        kwargs.pop("render_timeout", None)
        kwargs.pop("post_ready_delay", None)
        kwargs.pop("log_prefix", None)

        super().__init__(
            browser_manager=browser_manager,
            logger_func=logger_func,
            render_timeout=18,
            post_ready_delay=2.5,
            log_prefix="WAL-DC",
            **kwargs,
        )

    def scrape(
        self,
        url: str,
        target_variation: Optional[str] = None,
    ):
        """Return the existing six-value tuple after the final safety gate."""

        logs: list[tuple[str, str, str]] = []

        result = self.scrape_structured(
            url,
            target_variation=target_variation,
            logs=logs,
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

        # Walmart is intentionally stock-only.  The structured result may
        # retain an internal price because the shared ScrapeResult model
        # requires a price for a safe In Stock result.  Do not expose that
        # price to the engine or Review Window as a proposed update.
        for variant in variants or []:
            if isinstance(variant, dict):
                variant["price"] = ""

        return (
            status,
            "",
            stock,
            title,
            variants,
            output_logs,
        )

    def scrape_structured(
        self,
        url: str,
        *,
        target_variation: Optional[str] = None,
        logs: Optional[list[tuple[str, str, str]]] = None,
    ) -> ScrapeResult:
        """Run the normal consensus scraper and then the narrow final gate."""

        output_logs = logs if logs is not None else []

        result = super().scrape_structured(
            url,
            target_variation=target_variation,
            logs=output_logs,
        )

        result.metadata.update(
            {
                "walmart_stock_only_mode": True,
                "automatic_walmart_price_updates": False,
                "manual_review_walmart_price_changes": True,
                "walmart_final_guard_version": WALMART_FINAL_GUARD_VERSION,
            }
        )

        # The direct page probe is deliberately limited to a result that is
        # about to become an In Stock sheet update.  All OOS, low-stock,
        # unknown, conflict, blocked, and error results pass through exactly
        # as the main Walmart scraper produced them.
        if not self._needs_primary_unavailable_probe(result):
            return result

        probe = self._probe_primary_unavailable_state(result.item_id)
        result.metadata["walmart_primary_unavailable_probe"] = dict(probe)

        reason = self._rejection_reason_from_probe(probe)
        if not reason:
            return result

        result.add_evidence(
            source="visual-final-guard",
            field_name="stock",
            value="UNAVAILABLE_CONFLICT",
            confidence=0.99,
            item_id=result.item_id or None,
            details=dict(probe),
        )

        result.verification = VerificationStatus.CONFLICT
        result.sheet_stock = None
        result.price = None
        result.error_message = reason
        result.policy_reason = reason
        result.metadata.update(
            {
                "walmart_final_guard_rejected": True,
                "walmart_final_guard_reason": reason,
            }
        )

        self._append_log(
            output_logs,
            result.url,
            (
                "FINAL PRIMARY-AREA GUARD -> CONFLICT | "
                f"No sheet update | Reason: {reason}"
            ),
            "warning",
        )

        return result

    @staticmethod
    def _needs_primary_unavailable_probe(result: ScrapeResult) -> bool:
        """Return True only for a verified normal In Stock candidate."""

        return (
            result.verification == VerificationStatus.VERIFIED
            and result.observed_stock == StockState.IN_STOCK
            and result.sheet_stock == "In Stock"
        )

    def _probe_primary_unavailable_state(
        self,
        item_id: str,
    ) -> dict[str, Any]:
        """Inspect only the primary product area for strong unavailable data.

        This probe does not decide stock by itself.  It only detects a
        contradiction strong enough to block an In Stock update.  Failure to
        run the probe leaves the already verified result unchanged, avoiding
        regressions on otherwise healthy rows.
        """

        if not self.browser_manager:
            return {
                "probe_completed": False,
                "reason": "No browser manager was available for final guard.",
            }

        try:
            driver = self.browser_manager.get_driver()
        except Exception as exc:
            return {
                "probe_completed": False,
                "reason": (
                    "Could not obtain the browser for final guard: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }

        script = r"""
            const targetItemId = String(arguments[0] || "").trim();

            const unavailableRegex = /^(?:this item is )?(?:out of stock|sold out|currently unavailable|unavailable|not available|no longer available)[.!]?$/i;
            const addRegex = /\badd to (?:cart|basket|bag)\b/i;

            const badTokens = [
                "carousel", "recommend", "related", "sponsor",
                "search-result", "searchresult", "similar",
                "recently-viewed", "product-card", "product-tile",
                "product-grid", "module-product", "shelf", "review",
                "feedback", "footer", "header", "protection-plan",
                "warranty", "ad-container", "more-seller",
                "other-seller", "seller-offer"
            ];

            function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" &&
                    style.visibility !== "hidden" &&
                    Number(style.opacity || "1") > 0 &&
                    rect.width > 1 && rect.height > 1;
            }

            function pageY(el) {
                const rect = el.getBoundingClientRect();
                return rect.top + window.scrollY;
            }

            function ownText(el) {
                if (!el) return "";
                return (el.innerText || el.textContent || "")
                    .replace(/\s+/g, " ")
                    .trim();
            }

            function directText(el) {
                if (!el) return "";
                return Array.from(el.childNodes || [])
                    .filter(node => node.nodeType === Node.TEXT_NODE)
                    .map(node => node.textContent || "")
                    .join(" ")
                    .replace(/\s+/g, " ")
                    .trim();
            }

            function descriptors(el) {
                if (!el) return "";
                return [
                    el.id || "",
                    typeof el.className === "string" ? el.className : "",
                    el.getAttribute("data-testid") || "",
                    el.getAttribute("data-automation-id") || "",
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("role") || "",
                    el.tagName || ""
                ].join(" ").toLowerCase();
            }

            function normalizedLabel(el) {
                return [
                    ownText(el),
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("title") || "",
                    el.getAttribute("value") || "",
                    el.getAttribute("data-testid") || "",
                    el.getAttribute("data-automation-id") || ""
                ].join(" ").replace(/\s+/g, " ").trim();
            }

            function disabled(el) {
                return Boolean(el.disabled) ||
                    String(el.getAttribute("aria-disabled") || "").toLowerCase() === "true" ||
                    descriptors(el).includes("disabled");
            }

            function itemIdFromHref(href) {
                const match = String(href || "").match(
                    /\/ip\/(?:[^/?#]+\/)?(\d+)(?:[/?#]|$)/i
                );
                return match ? match[1] : "";
            }

            function hasBadAncestor(el) {
                let current = el;
                for (
                    let depth = 0;
                    current && current !== document.body && depth < 10;
                    depth++, current = current.parentElement
                ) {
                    const desc = descriptors(current);
                    if (badTokens.some(token => desc.includes(token))) {
                        return true;
                    }
                }
                return false;
            }

            function belongsToDifferentItem(el) {
                if (!el || !targetItemId) return false;

                const closestLink = el.closest("a[href*='/ip/']");
                if (closestLink) {
                    const closestId = itemIdFromHref(
                        closestLink.getAttribute("href") || closestLink.href
                    );
                    if (closestId && closestId !== targetItemId) {
                        return true;
                    }
                }

                let current = el;
                for (
                    let depth = 0;
                    current && current !== document.body && depth < 9;
                    depth++, current = current.parentElement
                ) {
                    const desc = descriptors(current);
                    const tag = String(current.tagName || "").toLowerCase();
                    const cardLike = tag === "li" || tag === "article" ||
                        /product[-_ ]?(card|tile|item)|carousel|shelf|recommend|related|sponsored/.test(desc);

                    if (!cardLike) continue;

                    const linkedIds = Array.from(
                        current.querySelectorAll("a[href*='/ip/']")
                    ).map(link => itemIdFromHref(
                        link.getAttribute("href") || link.href
                    )).filter(Boolean);

                    if (linkedIds.length && !linkedIds.includes(targetItemId)) {
                        return true;
                    }
                }

                return false;
            }

            const h1 = Array.from(document.querySelectorAll("h1"))
                .find(el => visible(el) && ownText(el).length > 2) || null;

            if (!h1) {
                return {
                    probe_completed: false,
                    reason: "No visible primary product heading was found."
                };
            }

            const headingY = pageY(h1);
            const stopLabels = [
                "about this item", "product details", "similar items",
                "similar items you might like", "based on what customers bought",
                "customers also considered", "customers also bought",
                "you may also like", "recommended for you", "related products",
                "sponsored products", "continue your search",
                "frequently bought together", "more seller options",
                "compare all sellers", "other sellers"
            ];

            const stopHeadings = Array.from(
                document.querySelectorAll("h2,h3,[role='heading']")
            ).filter(el => {
                if (!visible(el)) return false;
                const y = pageY(el);
                if (y <= headingY + 180) return false;
                const label = ownText(el).toLowerCase();
                return stopLabels.some(stop => label.startsWith(stop));
            }).sort((a, b) => pageY(a) - pageY(b));

            const regionTop = Math.max(0, headingY - 120);
            const naturalBottom = stopHeadings.length
                ? pageY(stopHeadings[0]) - 8
                : headingY + 1500;
            const regionBottom = Math.min(naturalBottom, headingY + 1500);

            function inPrimaryRegion(el) {
                if (!el || !visible(el) || hasBadAncestor(el) || belongsToDifferentItem(el)) {
                    return false;
                }
                const y = pageY(el);
                return y >= regionTop && y <= regionBottom;
            }

            const standaloneUnavailableTexts = [];

            for (const node of document.querySelectorAll("main div,main span,main p,main button")) {
                if (!inPrimaryRegion(node)) continue;

                const fullText = ownText(node);
                if (!fullText || fullText.length > 120) continue;

                const atomicText = directText(node) ||
                    (node.children.length === 0 ? fullText : "");

                if (atomicText && unavailableRegex.test(atomicText)) {
                    standaloneUnavailableTexts.push(atomicText);
                }
            }

            const fulfillment = {};
            const fulfillmentLabels = ["shipping", "pickup", "delivery"];

            function fulfillmentState(text) {
                const lower = String(text || "").toLowerCase();

                if (/out of stock|not available|unavailable|sold out/.test(lower)) {
                    return "UNAVAILABLE";
                }

                if (/arrives|delivery date|order within|free shipping|today|tomorrow|get it nearby|ready in/.test(lower)) {
                    return "AVAILABLE";
                }

                return "UNKNOWN";
            }

            for (const node of document.querySelectorAll("main div,main span,main p,main h3,main h4")) {
                if (!inPrimaryRegion(node)) continue;

                const label = (directText(node) || ownText(node)).toLowerCase();
                if (!fulfillmentLabels.includes(label)) continue;

                let box = node.parentElement;
                let chosen = null;

                for (
                    let depth = 0;
                    box && depth < 5;
                    depth++, box = box.parentElement
                ) {
                    if (!inPrimaryRegion(box)) continue;

                    const text = ownText(box);
                    if (text.length < label.length || text.length > 500) continue;

                    chosen = box;
                    if (/out of stock|not available|unavailable|sold out|arrives|order within|today|tomorrow|ready in/i.test(text)) {
                        break;
                    }
                }

                if (!chosen) continue;

                const text = ownText(chosen);
                fulfillment[label] = {
                    state: fulfillmentState(text),
                    text: text.slice(0, 300)
                };
            }

            const validAddControls = Array.from(document.querySelectorAll(
                "button,[role='button'],input[type='button'],input[type='submit']," +
                "[data-automation-id*='add-to-cart'],[data-testid*='add-to-cart']"
            )).filter(el => {
                if (!inPrimaryRegion(el) || disabled(el)) return false;
                return addRegex.test(normalizedLabel(el));
            });

            const states = Object.values(fulfillment)
                .map(value => String(value.state || "UNKNOWN").toUpperCase());

            return {
                probe_completed: true,
                target_item_id: targetItemId,
                region_top: regionTop,
                region_bottom: regionBottom,
                standalone_unavailable: standaloneUnavailableTexts.length > 0,
                standalone_unavailable_texts: Array.from(
                    new Set(standaloneUnavailableTexts)
                ).slice(0, 5),
                fulfillment,
                unavailable_count: states.filter(state => state === "UNAVAILABLE").length,
                available_count: states.filter(state => state === "AVAILABLE").length,
                unknown_count: states.filter(state => state === "UNKNOWN").length,
                valid_primary_add_to_cart: validAddControls.length > 0,
                valid_primary_add_to_cart_count: validAddControls.length
            };
        """

        try:
            data = driver.execute_script(
                script,
                str(item_id or ""),
            )
        except Exception as exc:
            return {
                "probe_completed": False,
                "reason": (
                    "Final primary-area probe failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }

        if not isinstance(data, dict):
            return {
                "probe_completed": False,
                "reason": "Final primary-area probe returned no usable data.",
            }

        return data

    @staticmethod
    def _rejection_reason_from_probe(
        probe: dict[str, Any],
    ) -> str:
        """Return a review reason only for strong unavailable evidence."""

        if not bool(probe.get("probe_completed")):
            return ""

        if bool(probe.get("standalone_unavailable")):
            texts = [
                str(value).strip()
                for value in (
                    probe.get("standalone_unavailable_texts") or []
                )
                if str(value).strip()
            ]
            detail = f" ({' | '.join(texts)})" if texts else ""
            return (
                "The primary Walmart product area explicitly reports that "
                f"the selected item is unavailable{detail}."
            )

        try:
            unavailable_count = int(probe.get("unavailable_count", 0) or 0)
            available_count = int(probe.get("available_count", 0) or 0)
        except (TypeError, ValueError):
            return ""

        # Do not reject an item merely because pickup or delivery is
        # unavailable.  The guard requires at least two unavailable primary
        # methods and no available method.  This protects rows where shipping
        # remains available.
        if unavailable_count >= 2 and available_count == 0:
            return (
                "The primary Walmart product area has multiple unavailable "
                "fulfillment methods and no available fulfillment method. "
                "Any detected Add to cart control is treated as stale or "
                "unrelated, so the row is held for review."
            )

        return ""
