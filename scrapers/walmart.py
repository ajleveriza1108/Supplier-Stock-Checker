"""Walmart scraper with conservative exact-item consensus.

The scraper keeps the application's six-value tuple interface. Every
Walmart result carries structured verification metadata. Exact-item JSON
and the requested item's primary rendered purchase block must agree; any
missing or conflicting evidence is held for manual review.
"""

from __future__ import annotations

import json
import re
import threading
import time
from decimal import Decimal
from typing import Any, Optional

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from core.base_scraper import BaseScraper
from core.scrape_result import ScrapeResult, StockState, VerificationStatus
from core.scraper_diagnostics import result_to_legacy_tuple
from core.structured_logger import StructuredLogger
from scrapers.walmart_policy import (
    WalmartObservation,
    WalmartPolicy,
    parse_inventory_detail,
)


WALMART_VISUAL_FIX_VERSION = "2026.07.13.5"


class WalmartScraper(BaseScraper):
    def __init__(
        self,
        browser_manager=None,
        logger_func=None,
        *,
        render_timeout: int = 14,
        post_ready_delay: float = 1.5,
        log_prefix: str = "WAL",
        **kwargs,
    ) -> None:
        super().__init__(browser_manager, logger_func)
        self.render_timeout = max(4, int(render_timeout))
        self.post_ready_delay = max(0.0, float(post_ready_delay))
        self.log_prefix = log_prefix
        self.policy = WalmartPolicy()
        self._result_lock = threading.RLock()
        self._last_results: dict[str, ScrapeResult] = {}
        try:
            self.structured_logger: Optional[StructuredLogger] = (
                StructuredLogger("WAL")
            )
        except OSError:
            self.structured_logger = None

    def scrape(
        self,
        url: str,
        target_variation: Optional[str] = None,
    ):
        logs: list[tuple[str, str, str]] = []
        result = self.scrape_structured(
            url,
            target_variation=target_variation,
            logs=logs,
        )
        with self._result_lock:
            self._last_results[url] = result
        return result_to_legacy_tuple(
            result,
            target_variation=target_variation,
            logs=logs,
        )

    def scrape_structured(
        self,
        url: str,
        *,
        target_variation: Optional[str] = None,
        logs: Optional[list[tuple[str, str, str]]] = None,
    ) -> ScrapeResult:
        output_logs = logs if logs is not None else []
        item_id = self._item_id(url)
        title = "Unknown"
        final_url = url

        if not self.browser_manager:
            return self._error_result(
                url=url,
                item_id=item_id or "",
                title=title,
                target_variation=target_variation or "",
                message="No browser manager is configured.",
                logs=output_logs,
            )

        if not item_id:
            return self._error_result(
                url=url,
                item_id="",
                title=title,
                target_variation=target_variation or "",
                message="Could not extract a Walmart item ID from the URL.",
                logs=output_logs,
            )

        try:
            driver = self.browser_manager.get_driver()
        except Exception as exc:
            return self._error_result(
                url=url,
                item_id=item_id,
                title=title,
                target_variation=target_variation or "",
                message=(
                    "Could not obtain browser driver: "
                    f"{type(exc).__name__}: {exc}"
                ),
                logs=output_logs,
            )

        try:
            driver.set_page_load_timeout(45)
            driver.get(url)
        except TimeoutException:
            self._append_log(
                output_logs,
                url,
                "Page-load timeout; parsing the available rendered page.",
                "warning",
            )
        except Exception as exc:
            return self._error_result(
                url=url,
                item_id=item_id,
                title=title,
                target_variation=target_variation or "",
                message=f"Navigation failed: {type(exc).__name__}: {exc}",
                logs=output_logs,
            )

        self._wait_for_product_page(driver, item_id=item_id)

        try:
            html = driver.page_source or ""
            final_url = driver.current_url or url
        except WebDriverException as exc:
            return self._error_result(
                url=url,
                item_id=item_id,
                title=title,
                target_variation=target_variation or "",
                message=(
                    "Could not read rendered page: "
                    f"{type(exc).__name__}: {exc}"
                ),
                logs=output_logs,
            )

        soup = BeautifulSoup(html, "html.parser")
        title = self._title(soup)
        visible_text = soup.get_text(" ", strip=True)
        blocked_reason = self._blocked_reason(
            visible_text=visible_text,
            title=title,
            final_url=final_url,
        )

        try:
            json_observation = self._json_observation(
                soup=soup,
                item_id=item_id,
            )
        except Exception as exc:
            json_observation = WalmartObservation(
                source="json",
                reason=(
                    "JSON extraction error: "
                    f"{type(exc).__name__}: {exc}"
                ),
                details={"exception": repr(exc)},
            )

        try:
            visual_observation = self._visual_observation(
                driver=driver,
                title=title,
                item_id=item_id,
            )
        except Exception as exc:
            visual_observation = WalmartObservation(
                source="visual",
                reason=(
                    "Visual extraction error: "
                    f"{type(exc).__name__}: {exc}"
                ),
                details={"exception": repr(exc)},
            )

        self._append_log(
            output_logs,
            url,
            self._observation_line("JSON", json_observation),
            "info",
        )
        self._append_log(
            output_logs,
            url,
            self._observation_line("VISUAL", visual_observation),
            "info",
        )

        result = self.policy.resolve(
            url=url,
            item_id=item_id,
            title=title,
            final_url=final_url,
            target_variation=target_variation or "",
            json_observation=json_observation,
            visual_observation=visual_observation,
            blocked_reason=blocked_reason,
        )

        self._append_log(
            output_logs,
            url,
            (
                f"DECISION -> Verification={result.verification.value} | "
                f"Observed={result.observed_stock.value} | "
                f"Sheet={result.sheet_stock or '[none]'} | "
                f"Price={ScrapeResult.format_price(result.price) or '[blank]'} | "
                f"Reason={result.policy_reason or result.error_message}"
            ),
            (
                "error"
                if result.verification in {
                    VerificationStatus.ERROR,
                    VerificationStatus.BLOCKED,
                }
                else "warning"
                if result.verification != VerificationStatus.VERIFIED
                else "info"
            ),
        )
        self._emit_structured(result)
        return result

    def get_last_result(self, url: str) -> Optional[ScrapeResult]:
        with self._result_lock:
            return self._last_results.get(url)

    def _wait_for_product_page(
        self,
        driver,
        *,
        item_id: str,
    ) -> None:
        """Wait for the requested item's primary purchase area.

        Walmart frequently renders recommendation cards before the selected
        product buy box is complete.  Waiting for any Add button or any price
        can therefore lock onto another item.  This wait requires the current
        URL item ID and a primary product anchor.
        """

        try:
            WebDriverWait(driver, self.render_timeout).until(
                lambda current: current.execute_script(
                    "return document.readyState"
                )
                in {"interactive", "complete"}
            )
        except (TimeoutException, WebDriverException):
            pass

        ready_script = r"""
            const targetId = String(arguments[0] || "");

            function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" &&
                       style.visibility !== "hidden" &&
                       Number(style.opacity || "1") > 0 &&
                       rect.width > 1 &&
                       rect.height > 1;
            }

            function text(el) {
                return (el && (el.innerText || el.textContent) || "")
                    .replace(/\s+/g, " ")
                    .trim();
            }

            function itemIdFromUrl(value) {
                const match = String(value || "").match(
                    /\/ip\/(?:[^/?#]+\/)?(\d+)(?:[/?#]|$)/i
                );
                return match ? match[1] : "";
            }

            const currentId = itemIdFromUrl(window.location.href);
            if (targetId && currentId && currentId !== targetId) {
                return false;
            }

            const h1 = Array.from(document.querySelectorAll("h1"))
                .find(el => visible(el) && text(el).length > 2);
            if (!h1) return false;

            const headingY = h1.getBoundingClientRect().top + window.scrollY;
            const anchors = Array.from(
                document.querySelectorAll("div,span,p")
            ).filter(el => {
                if (!visible(el)) return false;
                const value = text(el).toLowerCase();
                const y = el.getBoundingClientRect().top + window.scrollY;
                return value === "price when purchased online" &&
                       y >= headingY - 50 &&
                       y <= headingY + 1600;
            });

            if (anchors.length) return true;

            const primaryStatuses = Array.from(
                document.querySelectorAll("main div,main span,main p")
            ).filter(visible).some(el => {
                const value = text(el).toLowerCase();
                const y = el.getBoundingClientRect().top + window.scrollY;
                return y >= headingY - 50 &&
                       y <= headingY + 1200 &&
                       /^(?:this item is )?(?:out of stock|sold out|currently unavailable|unavailable|not available|no longer available)[.!]?$/
                           .test(value);
            });

            return primaryStatuses;
        """

        try:
            WebDriverWait(driver, self.render_timeout).until(
                lambda current: bool(
                    current.execute_script(ready_script, item_id)
                )
            )
        except (TimeoutException, WebDriverException):
            # The policy will return PARTIAL/UNKNOWN instead of guessing.
            pass

        if self.post_ready_delay:
            time.sleep(self.post_ready_delay)

    def _json_observation(
        self,
        *,
        soup: BeautifulSoup,
        item_id: str,
    ) -> WalmartObservation:
        payloads: list[tuple[str, dict[str, Any]]] = []

        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script is not None:
            decoded = self._decode_json(
                next_script.string or next_script.get_text() or ""
            )
            if decoded:
                payloads.append(("__NEXT_DATA__", decoded))

        for script in soup.find_all("script"):
            script_id = str(script.get("id", ""))
            if script_id != "Items-REDUX_STATE":
                continue
            decoded = self._decode_json(
                script.string or script.get_text() or ""
            )
            if decoded:
                payloads.append(("Items-REDUX_STATE", decoded))

        best_item: Optional[dict[str, Any]] = None
        best_source = ""
        best_score = -1

        for source, payload in payloads:
            for item in self._find_exact_items(payload, item_id):
                score = self._item_score(item)
                if score > best_score:
                    best_item = item
                    best_source = source
                    best_score = score

        if best_item is None:
            return WalmartObservation(
                source="json",
                reason=(
                    "Walmart JSON was present, but no exact target-item "
                    "product object was found."
                    if payloads
                    else "No usable Walmart product JSON was found."
                ),
                details={
                    "payload_sources": [source for source, _ in payloads]
                },
            )

        price = self._item_price(best_item)
        quantity = self._item_quantity(best_item)
        seller = self._item_seller(best_item)
        stock, stock_reason, conflict = self._item_stock(best_item)
        raw_text = json.dumps(
            best_item,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        inventory_text = self._item_inventory_text(best_item)
        if stock in {
            StockState.IN_STOCK,
            StockState.LOW_STOCK,
            StockState.LIMITED_STOCK,
            StockState.QUANTITY_REMAINING,
        }:
            # Do not scan the complete product object for low-stock text.
            # Walmart often nests other variants, seller offers, and
            # recommendations inside the exact item object.  Only direct
            # selected-item and fulfillment fields are allowed to influence
            # the user's low-inventory -> OOS business rule.
            stock, quantity = parse_inventory_detail(
                inventory_text,
                quantity=quantity,
                available=True,
            )

        confidence = 0.98 if stock != StockState.UNKNOWN else 0.45
        if stock != StockState.OOS and price is None:
            confidence = min(confidence, 0.70)
        if conflict:
            confidence = 0.20

        return WalmartObservation(
            source=f"json:{best_source}",
            stock=stock,
            price=price,
            quantity=quantity,
            confidence=confidence,
            exact_item_match=True,
            seller=seller,
            text=inventory_text or raw_text,
            reason=(
                f"Exact item {item_id} found in {best_source}; "
                f"{stock_reason}"
            ),
            details={
                "item_score": best_score,
                "internal_conflict": conflict,
                "inventory_text": inventory_text,
                "raw_item_excerpt": raw_text[:1200],
            },
        )

    def _visual_observation(
        self,
        *,
        driver,
        title: str,
        item_id: str,
    ) -> WalmartObservation:
        """Read only the requested item's primary rendered purchase block."""

        script = r"""
            const title = String(arguments[0] || "").trim();
            const targetId = String(arguments[1] || "").trim();
            const priceRegex =
                /\$\s*([0-9]{1,7}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)/g;

            const badTokens = [
                "carousel", "recommend", "related", "sponsor",
                "search-result", "searchresult", "similar",
                "recently-viewed", "product-card", "product-tile",
                "product-grid", "shelf", "review", "feedback",
                "footer", "header", "protection-plan", "warranty",
                "ad-container", "more-seller", "other-seller",
                "seller-offer"
            ];

            function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" &&
                       style.visibility !== "hidden" &&
                       Number(style.opacity || "1") > 0 &&
                       rect.width > 1 &&
                       rect.height > 1;
            }

            function pageY(el) {
                return el.getBoundingClientRect().top + window.scrollY;
            }

            function ownText(el) {
                return (el && (el.innerText || el.textContent) || "")
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
                    el.getAttribute("data-automation-id") || "",
                    el.getAttribute("data-testid") || ""
                ].join(" ").replace(/\s+/g, " ").trim().toLowerCase();
            }

            function disabled(el) {
                return Boolean(el.disabled) ||
                    (el.getAttribute("aria-disabled") || "").toLowerCase() === "true" ||
                    descriptors(el).includes("disabled");
            }

            function itemIdFromUrl(value) {
                const match = String(value || "").match(
                    /\/ip\/(?:[^/?#]+\/)?(\d+)(?:[/?#]|$)/i
                );
                return match ? match[1] : "";
            }

            function parsePrices(raw) {
                const output = [];
                const value = String(raw || "");
                priceRegex.lastIndex = 0;
                let match;
                while ((match = priceRegex.exec(value)) !== null) {
                    const amount = parseFloat(match[1].replace(/,/g, ""));
                    if (
                        Number.isFinite(amount) &&
                        amount >= 0.01 &&
                        amount <= 1000000
                    ) {
                        output.push(amount);
                    }
                }
                return output;
            }

            function hasTokenAncestor(el, tokens, maxDepth = 9) {
                let current = el;
                for (
                    let depth = 0;
                    current && depth < maxDepth;
                    depth++, current = current.parentElement
                ) {
                    const desc = descriptors(current);
                    if (tokens.some(token => desc.includes(token))) {
                        return true;
                    }
                }
                return false;
            }

            function belongsToDifferentItem(el) {
                if (!el || !targetId) return false;

                const closestLink = el.closest("a[href*='/ip/']");
                if (closestLink) {
                    const linkedId = itemIdFromUrl(
                        closestLink.getAttribute("href") || closestLink.href
                    );
                    if (linkedId && linkedId !== targetId) return true;
                }

                let current = el;
                for (
                    let depth = 0;
                    current &&
                    current !== document.body &&
                    current !== document.documentElement &&
                    depth < 9;
                    depth++, current = current.parentElement
                ) {
                    const desc = descriptors(current);
                    const tag = String(current.tagName || "").toLowerCase();
                    const cardLike =
                        tag === "li" ||
                        tag === "article" ||
                        /product[-_ ]?(card|tile|item)|carousel|shelf|recommend|related|sponsored/
                            .test(desc);

                    if (!cardLike) continue;

                    const ids = Array.from(
                        current.querySelectorAll("a[href*='/ip/']")
                    ).map(link => itemIdFromUrl(
                        link.getAttribute("href") || link.href
                    )).filter(Boolean);

                    if (ids.length && !ids.includes(targetId)) {
                        return true;
                    }
                }
                return false;
            }

            function hasBadAncestor(el) {
                let current = el;
                for (
                    let depth = 0;
                    current &&
                    current !== document.body &&
                    current !== document.documentElement &&
                    depth < 10;
                    depth++, current = current.parentElement
                ) {
                    const desc = descriptors(current);
                    if (badTokens.some(token => desc.includes(token))) {
                        return true;
                    }
                }
                return false;
            }

            const currentItemId = itemIdFromUrl(window.location.href);
            const currentItemMatch =
                !targetId || !currentItemId || currentItemId === targetId;

            const h1 = Array.from(document.querySelectorAll("h1"))
                .find(el => visible(el) && ownText(el).length > 2) || null;

            if (!h1 || !currentItemMatch) {
                return {
                    regionFound: false,
                    exactItemAnchor: false,
                    reason: !currentItemMatch
                        ? "The rendered page URL does not match the requested item ID."
                        : "No visible primary product heading was found."
                };
            }

            const headingY = pageY(h1);
            const stopLabels = [
                "about this item", "product details",
                "similar items", "similar items you might like",
                "based on what customers bought",
                "customers also considered", "customers also bought",
                "you may also like", "recommended for you",
                "related products", "sponsored products",
                "continue your search", "frequently bought together",
                "more seller options", "compare all sellers",
                "other sellers"
            ];

            const stopHeadings = Array.from(
                document.querySelectorAll("h2,h3,[role='heading']")
            ).filter(el => {
                if (!visible(el)) return false;
                const y = pageY(el);
                if (y <= headingY + 220) return false;
                const label = ownText(el).toLowerCase();
                return stopLabels.some(stop => label.startsWith(stop));
            }).sort((a, b) => pageY(a) - pageY(b));

            const regionTop = Math.max(0, headingY - 120);
            const regionBottom = Math.min(
                stopHeadings.length ? pageY(stopHeadings[0]) - 8 : headingY + 1800,
                headingY + 1800
            );

            function inMainRegion(el) {
                if (
                    !el ||
                    !visible(el) ||
                    hasBadAncestor(el) ||
                    belongsToDifferentItem(el)
                ) {
                    return false;
                }
                const y = pageY(el);
                return y >= regionTop && y <= regionBottom;
            }

            const purchaseAnchors = Array.from(
                document.querySelectorAll("div,span,p")
            ).filter(el => {
                if (!inMainRegion(el)) return false;
                const value = (directText(el) || ownText(el)).toLowerCase();
                const y = pageY(el);
                return value === "price when purchased online" &&
                       y >= headingY - 50 &&
                       y <= headingY + 1500;
            }).sort((a, b) => pageY(a) - pageY(b));

            const purchaseAnchor = purchaseAnchors[0] || null;
            if (!purchaseAnchor) {
                return {
                    regionFound: true,
                    exactItemAnchor: false,
                    reason: "Primary 'Price when purchased online' anchor was not found."
                };
            }

            const anchorY = pageY(purchaseAnchor);
            const purchaseTop = Math.max(regionTop, anchorY - 430);
            const purchaseBottom = Math.min(regionBottom, anchorY + 900);

            function inPurchaseWindow(el) {
                if (!inMainRegion(el)) return false;
                const y = pageY(el);
                return y >= purchaseTop && y <= purchaseBottom;
            }

            const priceSelectors = [
                '[data-automation-id="product-price"]',
                '[data-testid="product-price"]',
                '[data-testid*="product-price"]',
                '[data-testid="price-wrap"]',
                '[itemprop="price"]',
                'meta[itemprop="price"]'
            ];

            const priceCandidates = [];
            const seenPriceNodes = new Set();

            for (
                let selectorPriority = 0;
                selectorPriority < priceSelectors.length;
                selectorPriority++
            ) {
                const selector = priceSelectors[selectorPriority];
                for (const node of document.querySelectorAll(selector)) {
                    if (seenPriceNodes.has(node)) continue;
                    seenPriceNodes.add(node);

                    const anchorNode =
                        node.tagName === "META" && node.parentElement
                            ? node.parentElement
                            : node;

                    if (!inPurchaseWindow(anchorNode)) continue;
                    if (belongsToDifferentItem(anchorNode)) continue;

                    const y = pageY(anchorNode);
                    if (y < anchorY - 430 || y > anchorY + 80) continue;

                    if (
                        hasTokenAncestor(
                            anchorNode,
                            ["variant", "swatch", "option", "choice"],
                            8
                        )
                    ) {
                        continue;
                    }

                    const desc = descriptors(node);
                    if (
                        /was-price|strike|comparison|unit-price|protection|installment|affirm|klarna|afterpay|seller-offer|other-seller/
                            .test(desc)
                    ) {
                        continue;
                    }

                    const raw = [
                        node.getAttribute("content") || "",
                        node.getAttribute("aria-label") || "",
                        node.getAttribute("title") || "",
                        ownText(node)
                    ].join(" ");

                    const values = parsePrices(raw);
                    if (!values.length) continue;

                    const lower = raw.toLowerCase();
                    let priority = selectorPriority + 5;
                    if (lower.includes("current price")) priority -= 5;
                    if (
                        desc.includes("product-price") ||
                        desc.includes("product_price")
                    ) {
                        priority -= 3;
                    }
                    if (
                        lower.includes("was $") &&
                        !lower.includes("current price")
                    ) {
                        priority += 15;
                    }

                    priceCandidates.push({
                        value: values[0],
                        priority,
                        y,
                        distance: Math.abs(anchorY - y),
                        descriptor: desc || selector,
                        text: raw.slice(0, 240)
                    });
                }
            }

            priceCandidates.sort((a, b) =>
                (a.priority - b.priority) ||
                (a.distance - b.distance) ||
                (a.y - b.y)
            );

            const primaryPrice = priceCandidates[0] || null;
            const primaryPriceValues = primaryPrice
                ? Array.from(new Set(
                    priceCandidates
                        .filter(candidate =>
                            candidate.priority <= primaryPrice.priority + 1 &&
                            Math.abs(candidate.y - primaryPrice.y) <= 120
                        )
                        .map(candidate => candidate.value)
                ))
                : [];

            const allControls = Array.from(document.querySelectorAll(
                "button,[role='button'],input[type='button'],input[type='submit']," +
                "[data-automation-id*='add-to-cart'],[data-testid*='add-to-cart']"
            )).filter(inPurchaseWindow);

            const addControls = allControls.filter(el => {
                const label = normalizedLabel(el);
                const y = pageY(el);
                return (
                    label.includes("add to cart") ||
                    label.includes("add to basket") ||
                    label.includes("add to bag")
                ) &&
                y >= anchorY - 80 &&
                y <= anchorY + 520 &&
                !belongsToDifferentItem(el);
            }).sort((a, b) =>
                (Number(disabled(a)) - Number(disabled(b))) ||
                (Math.abs(pageY(a) - anchorY) - Math.abs(pageY(b) - anchorY))
            );

            const enabledAdd = addControls.find(el => !disabled(el)) || null;
            const disabledAdd = addControls.find(el => disabled(el)) || null;

            const fulfillmentLabels = ["shipping", "pickup", "delivery"];
            const fulfillment = {};
            const fulfillmentRanges = [];

            function fulfillmentState(value) {
                const lower = value.toLowerCase();
                if (
                    /out of stock|not available|unavailable|sold out/
                        .test(lower)
                ) {
                    return "UNAVAILABLE";
                }
                if (
                    /arrives|delivery date|order within|free shipping|today|tomorrow|get it nearby|ready in/
                        .test(lower)
                ) {
                    return "AVAILABLE";
                }
                return "UNKNOWN";
            }

            for (const node of document.querySelectorAll("div,span,p,h3,h4")) {
                if (!inPurchaseWindow(node)) continue;
                const label = (directText(node) || ownText(node)).toLowerCase();
                if (!fulfillmentLabels.includes(label)) continue;

                let box = node.parentElement;
                let selected = null;
                for (
                    let depth = 0;
                    box && depth < 5;
                    depth++, box = box.parentElement
                ) {
                    if (!inPurchaseWindow(box)) continue;
                    const value = ownText(box);
                    if (value.length >= label.length && value.length <= 550) {
                        selected = box;
                        if (
                            /out of stock|not available|unavailable|arrives|order within|today|tomorrow|check nearby|get it nearby/i
                                .test(value)
                        ) {
                            break;
                        }
                    }
                }

                if (!selected) continue;
                const value = ownText(selected);
                const top = pageY(selected);
                const bottom = top + selected.getBoundingClientRect().height;
                fulfillment[label] = {
                    state: fulfillmentState(value),
                    text: value.slice(0, 400),
                    top,
                    bottom
                };
                fulfillmentRanges.push({label, top, bottom});
            }

            function fulfillmentFor(node) {
                const y = pageY(node);
                const match = fulfillmentRanges.find(
                    range => y >= range.top - 5 && y <= range.bottom + 5
                );
                return match ? match.label : "";
            }

            const inventoryTexts = [];
            const selectedOosTexts = [];
            const productOosTexts = [];
            const genericOosTexts = [];

            for (const node of document.querySelectorAll("div,span,p,button")) {
                if (!inPurchaseWindow(node)) continue;

                const fullText = ownText(node);
                if (!fullText || fullText.length > 220) continue;

                const atomicText =
                    directText(node) ||
                    (node.children.length === 0 ? fullText : "");
                const value = atomicText || fullText;
                const fulfillmentMode = fulfillmentFor(node);

                if (
                    /low stock|limited stock|only\s+\d+\s+(?:left|remaining|in stock)/i
                        .test(value)
                ) {
                    const inVariantArea = hasTokenAncestor(
                        node,
                        ["variant", "swatch", "option", "choice"],
                        8
                    );
                    if (!inVariantArea) inventoryTexts.push(value);
                }

                if (
                    /selected option[^.]{0,100}(?:out of stock|unavailable|not available|sold out)/i
                        .test(value)
                ) {
                    selectedOosTexts.push(value);
                    continue;
                }

                if (
                    !/(?:out of stock|sold out|currently unavailable|this item is unavailable|not available|no longer available)/i
                        .test(value)
                ) {
                    continue;
                }

                if (fulfillmentMode) {
                    genericOosTexts.push(`${fulfillmentMode}: ${value}`);
                    continue;
                }

                const inVariantArea = hasTokenAncestor(
                    node,
                    ["variant", "swatch", "option", "choice"],
                    7
                );
                const standaloneProductStatus =
                    /^(?:this item is )?(?:out of stock|sold out|currently unavailable|unavailable|not available|no longer available)[.!]?$/i
                        .test(atomicText);
                const y = pageY(node);
                const closeToAnchor =
                    y >= anchorY - 80 &&
                    y <= anchorY + 520;

                if (
                    standaloneProductStatus &&
                    !inVariantArea &&
                    closeToAnchor
                ) {
                    productOosTexts.push(atomicText);
                } else {
                    genericOosTexts.push(value);
                }
            }

            const regionTextNodes = Array.from(
                document.querySelectorAll("div,span,p")
            ).filter(inPurchaseWindow)
             .map(ownText)
             .filter(value => value && value.length <= 220);

            const sellerText = regionTextNodes.find(value =>
                /sold (?:and shipped )?by|sold by|fulfilled by/i.test(value)
            ) || "";

            return {
                regionFound: true,
                exactItemAnchor: true,
                currentItemMatch,
                targetItemId: targetId,
                currentItemId,
                enabledCta: Boolean(enabledAdd),
                disabledCta: Boolean(disabledAdd),
                ctaText: enabledAdd
                    ? normalizedLabel(enabledAdd)
                    : disabledAdd
                    ? normalizedLabel(disabledAdd)
                    : "",
                priceValues: primaryPriceValues,
                priceCandidate: primaryPrice,
                priceCandidates: priceCandidates.slice(0, 8),
                inventoryText: Array.from(new Set(inventoryTexts)).join(" | "),
                selectedOptionOos: selectedOosTexts.length > 0,
                selectedOosTexts: Array.from(new Set(selectedOosTexts)).slice(0, 5),
                productOos: productOosTexts.length > 0,
                productOosTexts: Array.from(new Set(productOosTexts)).slice(0, 5),
                genericOosTexts: Array.from(new Set(genericOosTexts)).slice(0, 8),
                fulfillment,
                sellerText,
                reason: "Primary Walmart purchase evidence was anchored to the requested item."
            };
        """

        data = driver.execute_script(script, title, item_id) or {}

        strong_product_oos = [
            str(value).strip()
            for value in (data.get("productOosTexts") or [])
            if self._is_strong_product_oos_text(value)
        ]
        data["productOosTexts"] = strong_product_oos
        data["productOos"] = bool(strong_product_oos)

        (
            stock,
            quantity,
            reason,
            internal_conflict,
            oos_scope,
        ) = self.policy.classify_rendered_signals(data)

        price = self._candidate_price(data.get("priceValues") or [])
        seller = self._seller_from_text(str(data.get("sellerText") or ""))
        price_candidate = data.get("priceCandidate") or {}

        details = {
            "internal_conflict": internal_conflict,
            "oos_scope": oos_scope,
            "region_found": bool(data.get("regionFound")),
            "exact_item_anchor": bool(data.get("exactItemAnchor")),
            "current_item_match": bool(data.get("currentItemMatch")),
            "target_item_id": str(data.get("targetItemId") or ""),
            "current_item_id": str(data.get("currentItemId") or ""),
            "enabled_cta": bool(data.get("enabledCta")),
            "disabled_cta": bool(data.get("disabledCta")),
            "cta_text": str(data.get("ctaText") or ""),
            "price_candidate": price_candidate,
            "price_candidates": data.get("priceCandidates") or [],
            "inventory_text": str(data.get("inventoryText") or ""),
            "selected_option_oos": bool(data.get("selectedOptionOos")),
            "selected_oos_texts": data.get("selectedOosTexts") or [],
            "product_oos": bool(data.get("productOos")),
            "product_oos_texts": data.get("productOosTexts") or [],
            "generic_oos_texts": data.get("genericOosTexts") or [],
            "fulfillment": data.get("fulfillment") or {},
        }

        confidence = 0.0
        if internal_conflict:
            confidence = 0.20
        elif stock == StockState.IN_STOCK:
            confidence = (
                0.98
                if (
                    data.get("enabledCta")
                    and data.get("exactItemAnchor")
                    and price is not None
                )
                else 0.65
            )
        elif stock in {
            StockState.LOW_STOCK,
            StockState.LIMITED_STOCK,
            StockState.QUANTITY_REMAINING,
        }:
            confidence = (
                0.98
                if data.get("enabledCta") and data.get("exactItemAnchor")
                else 0.75
            )
        elif stock == StockState.OOS:
            confidence = (
                0.98
                if data.get("exactItemAnchor")
                and (
                    data.get("productOos")
                    or oos_scope in {"selected_option", "all_fulfillment"}
                )
                else 0.80
            )

        text_parts = [
            str(data.get("inventoryText") or ""),
            " | ".join(data.get("selectedOosTexts") or []),
            " | ".join(data.get("productOosTexts") or []),
            " | ".join(data.get("genericOosTexts") or []),
        ]
        observed_text = " | ".join(part for part in text_parts if part)

        return WalmartObservation(
            source="visual",
            stock=stock,
            price=price,
            quantity=quantity,
            confidence=confidence,
            title_match=bool(
                data.get("regionFound")
                and data.get("exactItemAnchor")
                and data.get("currentItemMatch")
            ),
            seller=seller,
            text=observed_text,
            reason=reason,
            selector=str(price_candidate.get("descriptor") or ""),
            details=details,
        )

    @staticmethod
    def _is_strong_product_oos_text(value: Any) -> bool:
        """Return True only for a standalone product-level OOS phrase.

        Fulfillment, variant, seller, and recommendation text must remain
        generic evidence and may never by itself update the sheet to OOS.
        """

        normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
        return bool(
            re.fullmatch(
                r"(?:this item is )?"
                r"(?:out of stock|sold out|currently unavailable|"
                r"unavailable|not available|no longer available)[.!]?",
                normalized,
            )
        )

    @staticmethod
    def _candidate_price(values: list[Any]) -> Optional[Decimal]:
        """Return one independent rendered product price.

        The visual extractor already orders values by selector reliability.
        JSON is intentionally not consulted here, preventing circular
        verification where the rendered pass is steered toward the JSON
        price it is supposed to verify.
        """

        parsed = [ScrapeResult.parse_price(value) for value in values]
        parsed = [value for value in parsed if value is not None]
        if not parsed:
            return None
        unique: list[Decimal] = []
        for value in parsed:
            if value not in unique:
                unique.append(value)
        return unique[0] if len(unique) == 1 else None

    def _find_exact_items(
        self,
        value: Any,
        item_id: str,
        *,
        path: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(value, dict):
            if self._matches_item_id(value, item_id) and self._product_like(value):
                found.append(value)

            skipped = {
                "carousel",
                "related",
                "recommended",
                "sponsored",
                "reviews",
                "searchresult",
                "similar",
            }
            for key, child in value.items():
                normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
                if any(token in normalized_key for token in skipped):
                    continue
                found.extend(
                    self._find_exact_items(
                        child,
                        item_id,
                        path=path + (str(key),),
                    )
                )
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found.extend(
                    self._find_exact_items(
                        child,
                        item_id,
                        path=path + (str(index),),
                    )
                )
        return found

    @staticmethod
    def _matches_item_id(item: dict[str, Any], item_id: str) -> bool:
        values = (
            item.get("usItemId"),
            item.get("itemId"),
            item.get("id"),
            item.get("productId"),
        )
        return any(
            value is not None and str(value).strip() == item_id
            for value in values
        )

    @staticmethod
    def _product_like(item: dict[str, Any]) -> bool:
        return bool(
            {
                "availabilityStatus",
                "availability",
                "priceInfo",
                "buyBox",
                "fulfillmentOptions",
                "name",
                "product",
            }.intersection(item)
        )

    @staticmethod
    def _item_score(item: dict[str, Any]) -> int:
        score = 0
        for key, points in (
            ("availabilityStatus", 5),
            ("priceInfo", 5),
            ("buyBox", 4),
            ("fulfillmentOptions", 3),
            ("name", 2),
            ("sellerDisplayName", 1),
        ):
            if item.get(key) not in (None, "", [], {}):
                score += points
        return score

    def _item_stock(
        self,
        item: dict[str, Any],
    ) -> tuple[StockState, str, bool]:
        availability = str(
            item.get("availabilityStatus")
            or item.get("availability")
            or ""
        ).strip().upper()
        buy_box = item.get("buyBox") or {}
        if not isinstance(buy_box, dict):
            buy_box = {}

        cta = buy_box.get("cta") or item.get("cta") or {}
        if isinstance(cta, dict):
            cta_text = str(
                cta.get("text")
                or cta.get("buttonText")
                or cta.get("label")
                or ""
            ).strip().lower()
        else:
            cta_text = str(cta).strip().lower()

        fulfillment_statuses: list[str] = []
        for option in item.get("fulfillmentOptions") or []:
            if not isinstance(option, dict):
                continue
            fulfillment_statuses.append(
                str(
                    option.get("availabilityStatus")
                    or option.get("availability")
                    or ""
                ).strip().upper()
            )

        fulfillment_statuses = [
            value
            for value in fulfillment_statuses
            if value
        ]

        explicit_in = (
            cta_text in {"add to cart", "add to basket"}
            or availability in {
                "IN_STOCK",
                "AVAILABLE",
                "AVAILABLE_TO_ORDER",
            }
            or any(
                value in {"IN_STOCK", "AVAILABLE"}
                for value in fulfillment_statuses
            )
        )
        all_fulfillment_unavailable = (
            len(fulfillment_statuses) >= 2
            and all(
                value in {
                    "OUT_OF_STOCK",
                    "UNAVAILABLE",
                    "NOT_AVAILABLE",
                }
                for value in fulfillment_statuses
            )
        )
        explicit_oos = (
            item.get("isOutOfStock") is True
            or buy_box.get("isOutOfStock") is True
            or availability in {
                "OUT_OF_STOCK",
                "UNAVAILABLE",
                "NOT_AVAILABLE",
                "SOLD_OUT",
                "PREORDER",
            }
            or all_fulfillment_unavailable
        )

        if explicit_in and explicit_oos:
            return (
                StockState.UNKNOWN,
                (
                    "the exact JSON item contains contradictory in-stock "
                    "and out-of-stock signals"
                ),
                True,
            )
        if explicit_in:
            return StockState.IN_STOCK, "JSON availability is in stock", False
        if explicit_oos:
            return StockState.OOS, "JSON availability is out of stock", False
        return StockState.UNKNOWN, "JSON availability is inconclusive", False

    def _item_price(self, item: dict[str, Any]) -> Optional[Decimal]:
        candidates: list[Any] = []
        for container in (
            item.get("priceInfo"),
            item.get("price"),
            item.get("offerPrice"),
        ):
            if isinstance(container, dict):
                current = container.get("currentPrice") or {}
                if isinstance(current, dict):
                    candidates.extend(
                        (
                            current.get("price"),
                            current.get("priceString"),
                            current.get("displayValue"),
                            current.get("value"),
                        )
                    )
                candidates.extend(
                    (
                        container.get("price"),
                        container.get("value"),
                        container.get("displayValue"),
                    )
                )
            else:
                candidates.append(container)

        for candidate in candidates:
            parsed = ScrapeResult.parse_price(candidate)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _item_inventory_text(item: dict[str, Any]) -> str:
        """Return stock wording scoped to the selected exact item.

        The exact Walmart product object can contain nested variants, seller
        offers, and recommendations.  Scanning its complete JSON text can
        incorrectly promote another option's "Limited stock" message to the
        selected product.  This helper intentionally reads only direct item,
        buy-box, and fulfillment fields.
        """

        values: list[str] = []

        def add(value: Any) -> None:
            if value in (None, "", [], {}):
                return
            if isinstance(value, (str, int, float, bool)):
                normalized = re.sub(r"\s+", " ", str(value)).strip()
                if normalized and normalized not in values:
                    values.append(normalized)

        direct_keys = (
            "availabilityStatus",
            "availability",
            "inventoryStatus",
            "stockStatus",
            "availabilityDisplay",
            "availabilityText",
            "inventoryText",
            "inventoryMessage",
            "stockMessage",
            "statusText",
            "message",
            "availableQuantity",
            "availableCount",
            "inventoryCount",
            "quantity",
        )

        for key in direct_keys:
            add(item.get(key))

        buy_box = item.get("buyBox")
        if isinstance(buy_box, dict):
            for key in direct_keys:
                add(buy_box.get(key))
            cta = buy_box.get("cta")
            if isinstance(cta, dict):
                for key in ("text", "buttonText", "label"):
                    add(cta.get(key))
            else:
                add(cta)

        for option in item.get("fulfillmentOptions") or []:
            if not isinstance(option, dict):
                continue
            for key in direct_keys:
                add(option.get(key))
            for nested_key in (
                "availability",
                "inventory",
                "shippingOption",
                "pickupOption",
                "deliveryOption",
            ):
                nested = option.get(nested_key)
                if not isinstance(nested, dict):
                    continue
                for key in direct_keys:
                    add(nested.get(key))

        return " | ".join(values)

    @staticmethod
    def _item_quantity(item: dict[str, Any]) -> Optional[int]:
        for key in (
            "availableQuantity",
            "quantity",
            "inventoryCount",
            "availableCount",
        ):
            value = item.get(key)
            if value is None:
                continue
            try:
                quantity = int(float(value))
            except (TypeError, ValueError):
                continue
            if quantity >= 0:
                return quantity
        return None

    @staticmethod
    def _item_seller(item: dict[str, Any]) -> str:
        candidates = (
            item.get("sellerDisplayName"),
            item.get("sellerName"),
            (item.get("seller") or {}).get("displayName")
            if isinstance(item.get("seller"), dict)
            else item.get("seller"),
            (item.get("buyBox") or {}).get("sellerDisplayName")
            if isinstance(item.get("buyBox"), dict)
            else None,
        )
        for candidate in candidates:
            if candidate:
                return str(candidate).strip()
        return ""

    @staticmethod
    def _seller_from_text(text: str) -> str:
        match = re.search(
            r"(?:sold|shipped)\s+(?:and\s+shipped\s+)?by\s+([^\n|]{2,80})",
            text or "",
            re.I,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _decode_json(raw: str) -> Optional[dict[str, Any]]:
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(raw[start : end + 1])
            return value if isinstance(value, dict) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    @staticmethod
    def _item_id(url: str) -> Optional[str]:
        clean = url.split("?", 1)[0].split("#", 1)[0]
        matches = re.findall(r"/(\d+)(?=/|$)", clean)
        return matches[-1] if matches else None

    @staticmethod
    def _title(soup: BeautifulSoup) -> str:
        h1 = soup.find("h1")
        if h1 and h1.get_text(" ", strip=True):
            return h1.get_text(" ", strip=True)
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            return str(meta["content"]).strip()
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        return "Unknown"

    @staticmethod
    def _blocked_reason(
        *,
        visible_text: str,
        title: str,
        final_url: str,
    ) -> str:
        text = f"{title} {final_url} {visible_text[:12000]}".lower()
        for phrase in (
            "verify you are human",
            "are you a human",
            "robot or human",
            "access denied",
            "pardon our interruption",
            "additional security check",
            "blocked due to unusual activity",
        ):
            if phrase in text:
                return f"Walmart blocked the page with {phrase!r}."
        return ""

    def _error_result(
        self,
        *,
        url: str,
        item_id: str,
        title: str,
        target_variation: str,
        message: str,
        logs: list[tuple[str, str, str]],
    ) -> ScrapeResult:
        self._append_log(logs, url, message, "error")
        result = ScrapeResult(
            supplier="WAL",
            url=url,
            verification=VerificationStatus.ERROR,
            observed_stock=StockState.UNKNOWN,
            title=title,
            item_id=item_id,
            variant=target_variation,
            final_url=url,
            error_message=message,
            policy_reason="Scrape error cannot update the sheet.",
        )
        self._emit_structured(result)
        return result

    def _emit_structured(self, result: ScrapeResult) -> None:
        if self.structured_logger is None:
            return
        try:
            self.structured_logger.emit(
                "walmart_result_resolved",
                phase="supplier_verification",
                url=result.url,
                verification=result.verification.value,
                observed_stock=result.observed_stock.value,
                sheet_stock=result.sheet_stock or "",
                price=ScrapeResult.format_price(result.price),
                quantity=result.quantity,
                reason=result.policy_reason or result.error_message,
                level=(
                    "error"
                    if result.verification in {
                        VerificationStatus.ERROR,
                        VerificationStatus.BLOCKED,
                    }
                    else "warning"
                    if result.verification != VerificationStatus.VERIFIED
                    else "info"
                ),
                extra={
                    "item_id": result.item_id,
                    "title": result.title,
                    "seller": result.seller,
                    "final_url": result.final_url,
                },
            )
        except OSError:
            pass

    def _append_log(
        self,
        logs: list[tuple[str, str, str]],
        url: str,
        message: str,
        tag: str,
    ) -> None:
        logs.append((f"{self.log_prefix}: {message}", tag, url))

    @staticmethod
    def _observation_line(
        label: str,
        observation: WalmartObservation,
    ) -> str:
        price = (
            f"${observation.price:.2f}"
            if observation.price is not None
            else "[blank]"
        )
        return (
            f"{label} -> Stock={observation.stock.value} | "
            f"Price={price} | Qty={observation.quantity} | "
            f"Confidence={observation.confidence:.2f} | "
            f"Reason={observation.reason}"
        )
