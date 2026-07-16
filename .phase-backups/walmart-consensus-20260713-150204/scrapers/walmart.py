"""Walmart scraper with exact-item JSON and primary-rendered-area consensus.

The scraper keeps the application's six-value tuple interface, but every
Walmart result also carries structured verification metadata in the first
variant dictionary. The engine guard permits updates only when JSON and
rendered primary-product evidence agree.
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


WALMART_VISUAL_FIX_VERSION = "2026.07.13.4"


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

        self._wait_for_product_page(
            driver,
            item_id=item_id,
        )

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
                details={
                    "exception": repr(exc),
                },
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
                details={
                    "exception": repr(exc),
                },
            )

        self._append_log(
            output_logs,
            url,
            self._observation_line(
                "JSON",
                json_observation,
            ),
            "info",
        )

        self._append_log(
            output_logs,
            url,
            self._observation_line(
                "VISUAL",
                visual_observation,
            ),
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
                if result.verification
                in {
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

    def get_last_result(
        self,
        url: str,
    ) -> Optional[ScrapeResult]:
        with self._result_lock:
            return self._last_results.get(url)

    def _wait_for_product_page(
        self,
        driver,
        *,
        item_id: str,
    ) -> None:
        """Wait until Walmart's primary purchase block is usable.

        The page heading often renders before the selected item's price,
        availability, and Add to cart control. Waiting for any price or any
        Add button is unsafe because recommendation cards load on the same
        page.

        This wait is anchored to Walmart's primary
        "Price when purchased online" block and the requested item ID.
        """

        try:
            WebDriverWait(
                driver,
                self.render_timeout,
            ).until(
                lambda current: current.execute_script(
                    "return document.readyState"
                )
                in {
                    "interactive",
                    "complete",
                }
            )
        except (
            TimeoutException,
            WebDriverException,
        ):
            pass

        try:
            WebDriverWait(
                driver,
                self.render_timeout,
            ).until(
                lambda current: bool(
                    current.find_elements(
                        By.CSS_SELECTOR,
                        "h1, script#__NEXT_DATA__, main",
                    )
                )
            )
        except (
            TimeoutException,
            WebDriverException,
        ):
            pass

        purchase_ready_script = r"""
            const targetItemId = String(arguments[0] || "");

            function visible(el) {
                if (!el) return false;

                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();

                return (
                    style.display !== "none" &&
                    style.visibility !== "hidden" &&
                    Number(style.opacity || "1") > 0 &&
                    rect.width > 1 &&
                    rect.height > 1
                );
            }

            function pageY(el) {
                const rect = el.getBoundingClientRect();
                return rect.top + window.scrollY;
            }

            function ownText(el) {
                return (
                    el &&
                    (
                        el.innerText ||
                        el.textContent
                    ) ||
                    ""
                )
                    .replace(/\s+/g, " ")
                    .trim();
            }

            function directText(el) {
                if (!el) return "";

                return Array.from(
                    el.childNodes || []
                )
                    .filter(
                        node =>
                            node.nodeType ===
                            Node.TEXT_NODE
                    )
                    .map(
                        node =>
                            node.textContent || ""
                    )
                    .join(" ")
                    .replace(/\s+/g, " ")
                    .trim();
            }

            function label(el) {
                return [
                    ownText(el),
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("title") || "",
                    el.getAttribute("value") || "",
                    el.getAttribute("data-automation-id") || "",
                    el.getAttribute("data-testid") || ""
                ]
                    .join(" ")
                    .replace(/\s+/g, " ")
                    .trim()
                    .toLowerCase();
            }

            function itemIdFromHref(href) {
                const match = String(
                    href || ""
                ).match(
                    /\/ip\/(?:[^/?#]+\/)?(\d+)(?:[/?#]|$)/i
                );

                return match
                    ? match[1]
                    : "";
            }

            function belongsToDifferentItem(el) {
                if (
                    !el ||
                    !targetItemId
                ) {
                    return false;
                }

                const closestLink = el.closest(
                    "a[href*='/ip/']"
                );

                if (closestLink) {
                    const closestId =
                        itemIdFromHref(
                            closestLink.getAttribute(
                                "href"
                            ) ||
                            closestLink.href
                        );

                    if (
                        closestId &&
                        closestId !== targetItemId
                    ) {
                        return true;
                    }
                }

                let current = el;

                for (
                    let depth = 0;
                    current &&
                    current !== document.body &&
                    current !==
                        document.documentElement &&
                    depth < 8;
                    depth++,
                    current = current.parentElement
                ) {
                    const descriptor = [
                        current.id || "",
                        typeof current.className ===
                            "string"
                            ? current.className
                            : "",
                        current.getAttribute(
                            "data-testid"
                        ) || "",
                        current.getAttribute(
                            "data-automation-id"
                        ) || "",
                        current.getAttribute(
                            "role"
                        ) || "",
                        current.tagName || ""
                    ]
                        .join(" ")
                        .toLowerCase();

                    const cardLike =
                        [
                            "li",
                            "article"
                        ].includes(
                            String(
                                current.tagName || ""
                            ).toLowerCase()
                        ) ||
                        /product[-_ ]?(card|tile|item)|carousel|shelf|recommend|related|sponsored/
                            .test(descriptor);

                    if (!cardLike) {
                        continue;
                    }

                    const ids = Array.from(
                        current.querySelectorAll(
                            "a[href*='/ip/']"
                        )
                    )
                        .map(
                            link =>
                                itemIdFromHref(
                                    link.getAttribute(
                                        "href"
                                    ) ||
                                    link.href
                                )
                        )
                        .filter(Boolean);

                    if (
                        ids.length &&
                        !ids.includes(targetItemId)
                    ) {
                        return true;
                    }
                }

                return false;
            }

            const h1 = Array.from(
                document.querySelectorAll("h1")
            ).find(
                el =>
                    visible(el) &&
                    ownText(el).length > 2
            ) || null;

            if (!h1) {
                return false;
            }

            const headingY = pageY(h1);

            const purchaseAnchors =
                Array.from(
                    document.querySelectorAll(
                        "div,span,p"
                    )
                )
                    .filter(el => {
                        if (
                            !visible(el) ||
                            belongsToDifferentItem(el)
                        ) {
                            return false;
                        }

                        const text = (
                            directText(el) ||
                            ownText(el)
                        ).toLowerCase();

                        const y = pageY(el);

                        return (
                            text ===
                                "price when purchased online" &&
                            y >= headingY - 40 &&
                            y <= headingY + 1500
                        );
                    })
                    .sort(
                        (first, second) =>
                            pageY(first) -
                            pageY(second)
                    );

            const anchor =
                purchaseAnchors[0] ||
                null;

            if (!anchor) {
                return false;
            }

            const anchorY = pageY(anchor);
            const windowTop = anchorY - 420;
            const windowBottom = anchorY + 900;

            function inPurchaseWindow(el) {
                if (
                    !el ||
                    !visible(el) ||
                    belongsToDifferentItem(el)
                ) {
                    return false;
                }

                const y = pageY(el);

                return (
                    y >= windowTop &&
                    y <= windowBottom
                );
            }

            const controls = Array.from(
                document.querySelectorAll(
                    "button,[role='button']," +
                    "input[type='button']," +
                    "input[type='submit']," +
                    "[data-automation-id*='add-to-cart']," +
                    "[data-testid*='add-to-cart']"
                )
            ).filter(inPurchaseWindow);

            const hasCart = controls.some(el => {
                const text = label(el);
                const y = pageY(el);

                return (
                    (
                        text.includes(
                            "add to cart"
                        ) ||
                        text.includes(
                            "add to basket"
                        ) ||
                        text.includes(
                            "add to bag"
                        )
                    ) &&
                    y >= anchorY - 80 &&
                    y <= anchorY + 500
                );
            });

            const priceNodes = Array.from(
                document.querySelectorAll(
                    '[data-automation-id="product-price"],' +
                    '[data-testid="product-price"],' +
                    '[data-testid*="product-price"],' +
                    '[data-testid="price-wrap"],' +
                    '[itemprop="price"]'
                )
            ).filter(inPurchaseWindow);

            const hasPrimaryPrice =
                priceNodes.some(el => {
                    const y = pageY(el);

                    return (
                        y >= anchorY - 420 &&
                        y <= anchorY + 60
                    );
                });

            const statusNodes = Array.from(
                document.querySelectorAll(
                    "main div,main span," +
                    "main p,main button"
                )
            ).filter(inPurchaseWindow);

            const hasStandaloneStatus =
                statusNodes.some(el => {
                    const text = (
                        directText(el) ||
                        ownText(el)
                    ).toLowerCase();

                    const y = pageY(el);

                    return (
                        (
                            /^(?:this item is )?(?:out of stock|sold out|currently unavailable|unavailable|not available|no longer available)[.!]?$/
                                .test(text) ||
                            /^(?:low stock|limited stock|only\s+\d+\s+(?:left|remaining|in stock))[.!]?$/
                                .test(text)
                        ) &&
                        y >= anchorY - 60 &&
                        y <= anchorY + 500
                    );
                });

            const hasFulfillmentHeading =
                statusNodes.some(el => {
                    const text = (
                        directText(el) ||
                        ownText(el)
                    ).toLowerCase();

                    const y = pageY(el);

                    return (
                        /^(shipping|pickup|delivery)$/
                            .test(text) &&
                        y >= anchorY &&
                        y <= anchorY + 800
                    );
                });

            return Boolean(
                hasCart ||
                hasPrimaryPrice ||
                hasStandaloneStatus ||
                hasFulfillmentHeading
            );
        """

        try:
            WebDriverWait(
                driver,
                self.render_timeout,
            ).until(
                lambda current: bool(
                    current.execute_script(
                        purchase_ready_script,
                        item_id,
                    )
                )
            )
        except (
            TimeoutException,
            WebDriverException,
        ):
            # Inconclusive pages remain blocked from automatic updates by
            # WalmartPolicy and the engine guard.
            pass

        if self.post_ready_delay:
            time.sleep(
                self.post_ready_delay
            )

    def _json_observation(
        self,
        *,
        soup: BeautifulSoup,
        item_id: str,
    ) -> WalmartObservation:
        payloads: list[
            tuple[str, dict[str, Any]]
        ] = []

        next_script = soup.find(
            "script",
            id="__NEXT_DATA__",
        )

        if next_script is not None:
            decoded = self._decode_json(
                next_script.string
                or next_script.get_text()
                or ""
            )

            if decoded:
                payloads.append(
                    (
                        "__NEXT_DATA__",
                        decoded,
                    )
                )

        for script in soup.find_all(
            "script"
        ):
            script_id = str(
                script.get(
                    "id",
                    "",
                )
            )

            if (
                script_id !=
                "Items-REDUX_STATE"
            ):
                continue

            decoded = self._decode_json(
                script.string
                or script.get_text()
                or ""
            )

            if decoded:
                payloads.append(
                    (
                        "Items-REDUX_STATE",
                        decoded,
                    )
                )

        best_item: Optional[
            dict[str, Any]
        ] = None

        best_source = ""
        best_score = -1

        for source, payload in payloads:
            for item in self._find_exact_items(
                payload,
                item_id,
            ):
                score = self._item_score(
                    item
                )

                if score > best_score:
                    best_item = item
                    best_source = source
                    best_score = score

        if best_item is None:
            return WalmartObservation(
                source="json",
                reason=(
                    "Walmart JSON was present, "
                    "but no exact target-item "
                    "product object was found."
                    if payloads
                    else
                    "No usable Walmart product "
                    "JSON was found."
                ),
                details={
                    "payload_sources": [
                        source
                        for source, _ in payloads
                    ],
                },
            )

        price = self._item_price(
            best_item
        )

        quantity = self._item_quantity(
            best_item
        )

        seller = self._item_seller(
            best_item
        )

        (
            stock,
            stock_reason,
            conflict,
        ) = self._item_stock(
            best_item
        )

        raw_text = json.dumps(
            best_item,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        inventory_text = (
            self._item_inventory_text(
                best_item
            )
        )

        if stock in {
            StockState.IN_STOCK,
            StockState.LOW_STOCK,
            StockState.LIMITED_STOCK,
            StockState.QUANTITY_REMAINING,
        }:
            stock, quantity = (
                parse_inventory_detail(
                    inventory_text,
                    quantity=quantity,
                    available=True,
                )
            )

        confidence = (
            0.98
            if stock != StockState.UNKNOWN
            else 0.45
        )

        if (
            stock != StockState.OOS and
            price is None
        ):
            confidence = min(
                confidence,
                0.70,
            )

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
            text=(
                inventory_text or
                raw_text
            ),
            reason=(
                f"Exact item {item_id} found "
                f"in {best_source}; "
                f"{stock_reason}"
            ),
            details={
                "item_score": best_score,
                "internal_conflict": conflict,
                "inventory_text": inventory_text,
                "raw_item_excerpt": (
                    raw_text[:1200]
                ),
            },
        )

    def _visual_observation(
        self,
        *,
        driver,
        title: str,
        item_id: str,
    ) -> WalmartObservation:
        """Read the selected item's primary Walmart purchase block.

        The extraction is anchored to the visible
        "Price when purchased online" text. Recommendation cards may contain
        their own prices and Add buttons, so every candidate is checked
        against the requested Walmart item ID.
        """

        script = r"""
            const title =
                String(
                    arguments[0] || ""
                ).trim();

            const targetItemId =
                String(
                    arguments[1] || ""
                ).trim();

            const badTokens = [
                "carousel",
                "recommend",
                "related",
                "sponsor",
                "search-result",
                "searchresult",
                "similar",
                "recently-viewed",
                "product-card",
                "product-tile",
                "product-grid",
                "module-product",
                "shelf",
                "review",
                "feedback",
                "footer",
                "header",
                "protection-plan",
                "warranty",
                "ad-container",
                "more-seller",
                "other-seller",
                "seller-offer"
            ];

            const priceRegex =
                /\$\s*([0-9]{1,7}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)/g;

            function visible(el) {
                if (!el) return false;

                const style =
                    window.getComputedStyle(el);

                const rect =
                    el.getBoundingClientRect();

                return (
                    style.display !== "none" &&
                    style.visibility !== "hidden" &&
                    Number(
                        style.opacity || "1"
                    ) > 0 &&
                    rect.width > 1 &&
                    rect.height > 1
                );
            }

            function pageY(el) {
                const rect =
                    el.getBoundingClientRect();

                return (
                    rect.top +
                    window.scrollY
                );
            }

            function descriptors(el) {
                if (!el) return "";

                return [
                    el.id || "",
                    typeof el.className ===
                        "string"
                        ? el.className
                        : "",
                    el.getAttribute(
                        "data-testid"
                    ) || "",
                    el.getAttribute(
                        "data-automation-id"
                    ) || "",
                    el.getAttribute(
                        "aria-label"
                    ) || "",
                    el.getAttribute(
                        "role"
                    ) || "",
                    el.tagName || ""
                ]
                    .join(" ")
                    .toLowerCase();
            }

            function ownText(el) {
                if (!el) return "";

                return (
                    el.innerText ||
                    el.textContent ||
                    ""
                )
                    .replace(/\s+/g, " ")
                    .trim();
            }

            function directText(el) {
                if (!el) return "";

                return Array.from(
                    el.childNodes || []
                )
                    .filter(
                        node =>
                            node.nodeType ===
                            Node.TEXT_NODE
                    )
                    .map(
                        node =>
                            node.textContent || ""
                    )
                    .join(" ")
                    .replace(/\s+/g, " ")
                    .trim();
            }

            function normalizedLabel(el) {
                return [
                    ownText(el),
                    el.getAttribute(
                        "aria-label"
                    ) || "",
                    el.getAttribute(
                        "title"
                    ) || "",
                    el.getAttribute(
                        "value"
                    ) || "",
                    el.getAttribute(
                        "data-automation-id"
                    ) || "",
                    el.getAttribute(
                        "data-testid"
                    ) || ""
                ]
                    .join(" ")
                    .replace(/\s+/g, " ")
                    .trim()
                    .toLowerCase();
            }

            function disabled(el) {
                return (
                    Boolean(el.disabled) ||
                    (
                        el.getAttribute(
                            "aria-disabled"
                        ) || ""
                    ).toLowerCase() ===
                        "true" ||
                    descriptors(el).includes(
                        "disabled"
                    )
                );
            }

            function parsePrices(raw) {
                const values = [];
                const text =
                    String(raw || "");

                let match;
                priceRegex.lastIndex = 0;

                while (
                    (
                        match =
                            priceRegex.exec(text)
                    ) !== null
                ) {
                    const value =
                        parseFloat(
                            match[1].replace(
                                /,/g,
                                ""
                            )
                        );

                    if (
                        Number.isFinite(value) &&
                        value >= 0.01 &&
                        value <= 1000000
                    ) {
                        values.push(value);
                    }
                }

                return values;
            }

            function itemIdFromHref(href) {
                const match =
                    String(
                        href || ""
                    ).match(
                        /\/ip\/(?:[^/?#]+\/)?(\d+)(?:[/?#]|$)/i
                    );

                return match
                    ? match[1]
                    : "";
            }

            function hasBadAncestor(el) {
                let current = el;

                for (
                    let depth = 0;
                    current &&
                    current !== document.body &&
                    current !==
                        document.documentElement &&
                    depth < 10;
                    depth++,
                    current =
                        current.parentElement
                ) {
                    const desc =
                        descriptors(current);

                    if (
                        badTokens.some(
                            token =>
                                desc.includes(token)
                        )
                    ) {
                        return true;
                    }
                }

                return false;
            }

            function hasTokenAncestor(
                el,
                tokens,
                maxDepth = 8
            ) {
                let current = el;

                for (
                    let depth = 0;
                    current &&
                    depth < maxDepth;
                    depth++,
                    current =
                        current.parentElement
                ) {
                    const desc =
                        descriptors(current);

                    if (
                        tokens.some(
                            token =>
                                desc.includes(token)
                        )
                    ) {
                        return true;
                    }
                }

                return false;
            }

            function belongsToDifferentItem(el) {
                if (
                    !el ||
                    !targetItemId
                ) {
                    return false;
                }

                const closestLink =
                    el.closest(
                        "a[href*='/ip/']"
                    );

                if (closestLink) {
                    const closestId =
                        itemIdFromHref(
                            closestLink.getAttribute(
                                "href"
                            ) ||
                            closestLink.href
                        );

                    if (
                        closestId &&
                        closestId !==
                            targetItemId
                    ) {
                        return true;
                    }
                }

                let current = el;

                for (
                    let depth = 0;
                    current &&
                    current !== document.body &&
                    current !==
                        document.documentElement &&
                    depth < 9;
                    depth++,
                    current =
                        current.parentElement
                ) {
                    const desc =
                        descriptors(current);

                    const tag =
                        String(
                            current.tagName || ""
                        ).toLowerCase();

                    const cardLike =
                        tag === "li" ||
                        tag === "article" ||
                        /product[-_ ]?(card|tile|item)|carousel|shelf|recommend|related|sponsored/
                            .test(desc);

                    if (!cardLike) {
                        continue;
                    }

                    const ids =
                        Array.from(
                            current.querySelectorAll(
                                "a[href*='/ip/']"
                            )
                        )
                            .map(
                                link =>
                                    itemIdFromHref(
                                        link.getAttribute(
                                            "href"
                                        ) ||
                                        link.href
                                    )
                            )
                            .filter(Boolean);

                    if (
                        ids.length &&
                        !ids.includes(
                            targetItemId
                        )
                    ) {
                        return true;
                    }
                }

                return false;
            }

            const h1 =
                Array.from(
                    document.querySelectorAll(
                        "h1"
                    )
                ).find(
                    el =>
                        visible(el) &&
                        ownText(el).length > 2
                ) || null;

            if (!h1) {
                return {
                    regionFound: false,
                    reason:
                        "No visible primary product heading was found."
                };
            }

            const headingY =
                pageY(h1);

            const stopLabels = [
                "about this item",
                "product details",
                "similar items",
                "similar items you might like",
                "based on what customers bought",
                "customers also considered",
                "customers also bought",
                "you may also like",
                "recommended for you",
                "related products",
                "sponsored products",
                "continue your search",
                "frequently bought together",
                "more seller options",
                "compare all sellers",
                "other sellers"
            ];

            const stopHeadings =
                Array.from(
                    document.querySelectorAll(
                        "h2,h3,[role='heading']"
                    )
                )
                    .filter(el => {
                        if (!visible(el)) {
                            return false;
                        }

                        const y = pageY(el);

                        if (
                            y <= headingY + 220
                        ) {
                            return false;
                        }

                        const label =
                            ownText(el)
                                .toLowerCase();

                        return stopLabels.some(
                            stop =>
                                label.startsWith(
                                    stop
                                )
                        );
                    })
                    .sort(
                        (first, second) =>
                            pageY(first) -
                            pageY(second)
                    );

            const regionTop =
                Math.max(
                    0,
                    headingY - 120
                );

            const naturalBottom =
                stopHeadings.length
                    ? pageY(
                        stopHeadings[0]
                    ) - 8
                    : headingY + 1800;

            const regionBottom =
                Math.min(
                    naturalBottom,
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

                return (
                    y >= regionTop &&
                    y <= regionBottom
                );
            }

            const exactPurchaseAnchors =
                Array.from(
                    document.querySelectorAll(
                        "div,span,p"
                    )
                )
                    .filter(el => {
                        if (!inMainRegion(el)) {
                            return false;
                        }

                        const text = (
                            directText(el) ||
                            ownText(el)
                        ).toLowerCase();

                        const y = pageY(el);

                        return (
                            text ===
                                "price when purchased online" &&
                            y >= headingY - 40 &&
                            y <= headingY + 1500
                        );
                    })
                    .sort(
                        (first, second) =>
                            pageY(first) -
                            pageY(second)
                    );

            const fallbackPurchaseAnchors =
                Array.from(
                    document.querySelectorAll(
                        "div,span,p"
                    )
                )
                    .filter(el => {
                        if (!inMainRegion(el)) {
                            return false;
                        }

                        const text =
                            ownText(el)
                                .toLowerCase();

                        const y = pageY(el);

                        return (
                            text.includes(
                                "price when purchased online"
                            ) &&
                            text.length <= 100 &&
                            y >= headingY - 40 &&
                            y <= headingY + 1500
                        );
                    })
                    .sort(
                        (first, second) =>
                            pageY(first) -
                            pageY(second)
                    );

            const purchaseAnchor =
                exactPurchaseAnchors[0] ||
                fallbackPurchaseAnchors[0] ||
                null;

            const anchorY =
                purchaseAnchor
                    ? pageY(purchaseAnchor)
                    : headingY + 650;

            const purchaseTop =
                purchaseAnchor
                    ? Math.max(
                        regionTop,
                        anchorY - 430
                    )
                    : regionTop;

            const purchaseBottom =
                purchaseAnchor
                    ? Math.min(
                        regionBottom,
                        anchorY + 900
                    )
                    : Math.min(
                        regionBottom,
                        headingY + 1100
                    );

            function inPurchaseWindow(el) {
                if (!inMainRegion(el)) {
                    return false;
                }

                const y = pageY(el);

                return (
                    y >= purchaseTop &&
                    y <= purchaseBottom
                );
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
            const seenPriceNodes =
                new Set();

            for (
                let selectorPriority = 0;
                selectorPriority <
                    priceSelectors.length;
                selectorPriority++
            ) {
                const selector =
                    priceSelectors[
                        selectorPriority
                    ];

                for (
                    const node of
                    document.querySelectorAll(
                        selector
                    )
                ) {
                    if (
                        seenPriceNodes.has(node)
                    ) {
                        continue;
                    }

                    seenPriceNodes.add(node);

                    const anchorNode =
                        node.tagName === "META" &&
                        node.parentElement
                            ? node.parentElement
                            : node;

                    if (
                        !inPurchaseWindow(
                            anchorNode
                        ) ||
                        belongsToDifferentItem(
                            anchorNode
                        )
                    ) {
                        continue;
                    }

                    const candidateY =
                        pageY(anchorNode);

                    const expectedPriceWindow =
                        purchaseAnchor
                            ? (
                                candidateY >=
                                    anchorY - 430 &&
                                candidateY <=
                                    anchorY + 65
                            )
                            : (
                                candidateY >=
                                    headingY - 60 &&
                                candidateY <=
                                    headingY + 850
                            );

                    if (!expectedPriceWindow) {
                        continue;
                    }

                    if (
                        hasTokenAncestor(
                            anchorNode,
                            [
                                "variant",
                                "swatch",
                                "option",
                                "choice"
                            ],
                            8
                        )
                    ) {
                        continue;
                    }

                    const desc =
                        descriptors(node);

                    if (
                        /was-price|strike|comparison|unit-price|protection|installment|affirm|klarna|afterpay|seller-offer|other-seller/
                            .test(desc)
                    ) {
                        continue;
                    }

                    const raw = [
                        node.getAttribute(
                            "content"
                        ) || "",
                        node.getAttribute(
                            "aria-label"
                        ) || "",
                        node.getAttribute(
                            "title"
                        ) || "",
                        ownText(node),
                        anchorNode.parentElement
                            ? directText(
                                anchorNode.parentElement
                            )
                            : ""
                    ].join(" ");

                    const values =
                        parsePrices(raw);

                    if (!values.length) {
                        continue;
                    }

                    const lowerRaw =
                        raw.toLowerCase();

                    let semanticPriority =
                        selectorPriority + 5;

                    if (
                        lowerRaw.includes(
                            "current price"
                        )
                    ) {
                        semanticPriority -= 5;
                    }

                    if (
                        desc.includes(
                            "product-price"
                        ) ||
                        desc.includes(
                            "product_price"
                        )
                    ) {
                        semanticPriority -= 3;
                    }

                    if (
                        lowerRaw.includes(
                            "was $"
                        ) &&
                        !lowerRaw.includes(
                            "current price"
                        )
                    ) {
                        semanticPriority += 15;
                    }

                    priceCandidates.push({
                        value: values[0],
                        priority:
                            semanticPriority,
                        selectorPriority,
                        y: candidateY,
                        anchorDistance:
                            Math.abs(
                                anchorY -
                                candidateY
                            ),
                        descriptor:
                            desc || selector,
                        text:
                            raw.slice(
                                0,
                                240
                            )
                    });
                }
            }

            if (
                !priceCandidates.length
            ) {
                for (
                    const node of
                    document.querySelectorAll(
                        "div,span,p"
                    )
                ) {
                    if (
                        !inPurchaseWindow(node)
                    ) {
                        continue;
                    }

                    const text =
                        ownText(node);

                    const lowerText =
                        text.toLowerCase();

                    const y =
                        pageY(node);

                    if (
                        text.length > 180 ||
                        !(
                            lowerText.includes(
                                "current price is"
                            ) ||
                            lowerText.includes(
                                "current price now"
                            )
                        )
                    ) {
                        continue;
                    }

                    if (
                        purchaseAnchor &&
                        !(
                            y >=
                                anchorY - 430 &&
                            y <=
                                anchorY + 65
                        )
                    ) {
                        continue;
                    }

                    const values =
                        parsePrices(text);

                    if (!values.length) {
                        continue;
                    }

                    priceCandidates.push({
                        value: values[0],
                        priority: 20,
                        selectorPriority: 50,
                        y,
                        anchorDistance:
                            Math.abs(
                                anchorY - y
                            ),
                        descriptor:
                            descriptors(node),
                        text:
                            text.slice(
                                0,
                                240
                            )
                    });
                }
            }

            priceCandidates.sort(
                (first, second) =>
                    (
                        first.priority -
                        second.priority
                    ) ||
                    (
                        first.anchorDistance -
                        second.anchorDistance
                    ) ||
                    (
                        first.y -
                        second.y
                    )
            );

            const primaryPrice =
                priceCandidates.length
                    ? priceCandidates[0]
                    : null;

            const primaryPriceValues =
                primaryPrice
                    ? Array.from(
                        new Set(
                            priceCandidates
                                .filter(
                                    candidate =>
                                        candidate.priority <=
                                            primaryPrice.priority +
                                                1 &&
                                        Math.abs(
                                            candidate.y -
                                            primaryPrice.y
                                        ) <= 120
                                )
                                .map(
                                    candidate =>
                                        candidate.value
                                )
                        )
                    )
                    : [];

            const allControls =
                Array.from(
                    document.querySelectorAll(
                        "button,[role='button']," +
                        "input[type='button']," +
                        "input[type='submit']," +
                        "[data-automation-id*='add-to-cart']," +
                        "[data-testid*='add-to-cart']"
                    )
                ).filter(
                    inPurchaseWindow
                );

            const addControls =
                allControls
                    .filter(el => {
                        const text =
                            normalizedLabel(el);

                        const y =
                            pageY(el);

                        const isAddToCart =
                            text.includes(
                                "add to cart"
                            ) ||
                            text.includes(
                                "add to basket"
                            ) ||
                            text.includes(
                                "add to bag"
                            );

                        const expectedCtaWindow =
                            purchaseAnchor
                                ? (
                                    y >=
                                        anchorY -
                                            80 &&
                                    y <=
                                        anchorY +
                                            520
                                )
                                : (
                                    y >=
                                        headingY &&
                                    y <=
                                        headingY +
                                            1050
                                );

                        return (
                            isAddToCart &&
                            expectedCtaWindow &&
                            !belongsToDifferentItem(
                                el
                            )
                        );
                    })
                    .sort(
                        (first, second) => {
                            const firstDisabled =
                                disabled(first)
                                    ? 1
                                    : 0;

                            const secondDisabled =
                                disabled(second)
                                    ? 1
                                    : 0;

                            return (
                                firstDisabled -
                                    secondDisabled ||
                                Math.abs(
                                    pageY(first) -
                                    anchorY
                                ) -
                                    Math.abs(
                                        pageY(second) -
                                        anchorY
                                    )
                            );
                        }
                    );

            const enabledAdd =
                addControls.find(
                    el => !disabled(el)
                ) || null;

            const disabledAdd =
                addControls.find(
                    el => disabled(el)
                ) || null;

            const fulfillmentLabels = [
                "shipping",
                "pickup",
                "delivery"
            ];

            const fulfillment = {};
            const fulfillmentRanges = [];

            function fulfillmentState(
                text,
                label
            ) {
                const lower =
                    text.toLowerCase();

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

                if (
                    label === "pickup" &&
                    /check nearby/
                        .test(lower)
                ) {
                    return "UNKNOWN";
                }

                return "UNKNOWN";
            }

            for (
                const node of
                document.querySelectorAll(
                    "div,span,p,h3,h4"
                )
            ) {
                if (
                    !inPurchaseWindow(node)
                ) {
                    continue;
                }

                const label = (
                    directText(node) ||
                    ownText(node)
                ).toLowerCase();

                if (
                    !fulfillmentLabels
                        .includes(label)
                ) {
                    continue;
                }

                const labelY =
                    pageY(node);

                if (
                    purchaseAnchor &&
                    (
                        labelY <
                            anchorY - 10 ||
                        labelY >
                            anchorY + 850
                    )
                ) {
                    continue;
                }

                let box =
                    node.parentElement;

                let chosen = null;

                for (
                    let depth = 0;
                    box &&
                    depth < 5;
                    depth++,
                    box =
                        box.parentElement
                ) {
                    if (
                        !inPurchaseWindow(box) ||
                        belongsToDifferentItem(
                            box
                        )
                    ) {
                        continue;
                    }

                    const text =
                        ownText(box);

                    if (
                        text.length >=
                            label.length &&
                        text.length <= 550
                    ) {
                        chosen = box;

                        if (
                            /out of stock|not available|arrives|order within|check nearby|get it nearby|today|tomorrow|low stock|limited stock/i
                                .test(text)
                        ) {
                            break;
                        }
                    }
                }

                if (!chosen) {
                    continue;
                }

                const text =
                    ownText(chosen);

                const top =
                    pageY(chosen);

                const height =
                    chosen
                        .getBoundingClientRect()
                        .height;

                fulfillment[label] = {
                    state:
                        fulfillmentState(
                            text,
                            label
                        ),
                    text:
                        text.slice(
                            0,
                            400
                        ),
                    top,
                    bottom:
                        top + height
                };

                fulfillmentRanges.push({
                    label,
                    top,
                    bottom:
                        top + height
                });
            }

            function fulfillmentFor(node) {
                const y = pageY(node);

                const match =
                    fulfillmentRanges.find(
                        range =>
                            y >=
                                range.top - 5 &&
                            y <=
                                range.bottom + 5
                    );

                return match
                    ? match.label
                    : "";
            }

            const inventoryTexts = [];
            const selectedOosTexts = [];
            const productOosTexts = [];
            const genericOosTexts = [];

            const firstFulfillmentY =
                fulfillmentRanges.length
                    ? Math.min(
                        ...fulfillmentRanges.map(
                            item =>
                                item.top
                        )
                    )
                    : purchaseBottom;

            for (
                const node of
                document.querySelectorAll(
                    "div,span,p,button"
                )
            ) {
                if (
                    !inPurchaseWindow(node)
                ) {
                    continue;
                }

                const fullText =
                    ownText(node);

                if (
                    !fullText ||
                    fullText.length > 220
                ) {
                    continue;
                }

                const atomicText =
                    directText(node) ||
                    (
                        node.children.length === 0
                            ? fullText
                            : ""
                    );

                const text =
                    atomicText ||
                    fullText;

                const mode =
                    fulfillmentFor(node);

                const y =
                    pageY(node);

                if (
                    /low stock|limited stock|only\s+\d+\s+(?:left|remaining|in stock)/i
                        .test(text)
                ) {
                    const inVariantArea =
                        hasTokenAncestor(
                            node,
                            [
                                "variant",
                                "swatch",
                                "option",
                                "choice"
                            ],
                            8
                        );

                    const nearBuyBox =
                        y >= anchorY - 80 &&
                        y <= Math.min(
                            purchaseBottom,
                            firstFulfillmentY +
                                520
                        );

                    const containsCart =
                        Boolean(
                            enabledAdd &&
                            (
                                node ===
                                    enabledAdd ||
                                node.contains(
                                    enabledAdd
                                )
                            )
                        );

                    if (
                        !inVariantArea &&
                        (
                            Boolean(mode) ||
                            nearBuyBox ||
                            containsCart
                        )
                    ) {
                        inventoryTexts.push(
                            text
                        );
                    }
                }

                if (
                    /selected option[^.]{0,80}(?:out of stock|unavailable|not available|sold out)/i
                        .test(text)
                ) {
                    selectedOosTexts.push(
                        text
                    );
                    continue;
                }

                if (
                    !/(?:out of stock|sold out|currently unavailable|this item is unavailable|not available|no longer available)/i
                        .test(text)
                ) {
                    continue;
                }

                if (mode) {
                    genericOosTexts.push(
                        `${mode}: ${text}`
                    );
                    continue;
                }

                const inVariantArea =
                    hasTokenAncestor(
                        node,
                        [
                            "variant",
                            "swatch",
                            "option",
                            "choice"
                        ],
                        7
                    );

                const containsEnabledCart =
                    Boolean(
                        enabledAdd &&
                        (
                            node ===
                                enabledAdd ||
                            node.contains(
                                enabledAdd
                            )
                        )
                    );

                const standaloneStatus =
                    /^(?:this item is )?(?:out of stock|sold out|currently unavailable|unavailable|not available|no longer available)[.!]?$/i
                        .test(atomicText);

                const nearPrimaryBlock =
                    y >= anchorY - 80 &&
                    y <= Math.min(
                        firstFulfillmentY +
                            60,
                        anchorY + 520
                    );

                if (
                    standaloneStatus &&
                    !inVariantArea &&
                    !containsEnabledCart &&
                    nearPrimaryBlock
                ) {
                    productOosTexts.push(
                        atomicText
                    );
                } else {
                    genericOosTexts.push(
                        text
                    );
                }
            }

            const regionTextNodes =
                Array.from(
                    document.querySelectorAll(
                        "div,span,p"
                    )
                )
                    .filter(
                        inPurchaseWindow
                    )
                    .map(ownText)
                    .filter(
                        text =>
                            text &&
                            text.length <= 220
                    );

            const sellerText =
                regionTextNodes.find(
                    text =>
                        /sold (?:and shipped )?by|sold by|fulfilled by/i
                            .test(text)
                ) || "";

            const anyDifferentItemCart =
                Array.from(
                    document.querySelectorAll(
                        "button,[role='button']," +
                        "input[type='button']," +
                        "input[type='submit']"
                    )
                ).some(el => {
                    if (!visible(el)) {
                        return false;
                    }

                    const text =
                        normalizedLabel(el);

                    return (
                        (
                            text.includes(
                                "add to cart"
                            ) ||
                            text.includes(
                                "add to basket"
                            ) ||
                            text.includes(
                                "add to bag"
                            )
                        ) &&
                        belongsToDifferentItem(
                            el
                        )
                    );
                });

            return {
                regionFound: true,
                title,
                targetItemId,
                regionTop,
                regionBottom,

                purchaseAnchorFound:
                    Boolean(
                        purchaseAnchor
                    ),

                purchaseAnchorY:
                    purchaseAnchor
                        ? anchorY
                        : null,

                purchaseTop,
                purchaseBottom,

                enabledCta:
                    Boolean(enabledAdd),

                disabledCta:
                    Boolean(disabledAdd),

                provisionalEnabledCta:
                    Boolean(
                        allControls.find(
                            el =>
                                !disabled(el) &&
                                (
                                    normalizedLabel(el)
                                        .includes(
                                            "add to cart"
                                        ) ||
                                    normalizedLabel(el)
                                        .includes(
                                            "add to basket"
                                        ) ||
                                    normalizedLabel(el)
                                        .includes(
                                            "add to bag"
                                        )
                                )
                        )
                    ),

                provisionalDisabledCta:
                    Boolean(
                        allControls.find(
                            el =>
                                disabled(el) &&
                                (
                                    normalizedLabel(el)
                                        .includes(
                                            "add to cart"
                                        ) ||
                                    normalizedLabel(el)
                                        .includes(
                                            "add to basket"
                                        ) ||
                                    normalizedLabel(el)
                                        .includes(
                                            "add to bag"
                                        )
                                )
                        )
                    ),

                rejectedRecommendationCta:
                    Boolean(
                        !enabledAdd &&
                        anyDifferentItemCart
                    ),

                ctaText:
                    enabledAdd
                        ? normalizedLabel(
                            enabledAdd
                        )
                        : disabledAdd
                        ? normalizedLabel(
                            disabledAdd
                        )
                        : "",

                ctaY:
                    enabledAdd
                        ? pageY(enabledAdd)
                        : disabledAdd
                        ? pageY(disabledAdd)
                        : null,

                priceValues:
                    primaryPriceValues,

                priceCandidate:
                    primaryPrice,

                priceCandidates:
                    priceCandidates.slice(
                        0,
                        8
                    ),

                inventoryText:
                    Array.from(
                        new Set(
                            inventoryTexts
                        )
                    ).join(" | "),

                selectedOptionOos:
                    selectedOosTexts.length > 0,

                selectedOosTexts:
                    Array.from(
                        new Set(
                            selectedOosTexts
                        )
                    ).slice(
                        0,
                        5
                    ),

                productOos:
                    productOosTexts.length > 0,

                productOosTexts:
                    Array.from(
                        new Set(
                            productOosTexts
                        )
                    ).slice(
                        0,
                        5
                    ),

                genericOosTexts:
                    Array.from(
                        new Set(
                            genericOosTexts
                        )
                    ).slice(
                        0,
                        8
                    ),

                fulfillment,
                sellerText,

                reason:
                    "Primary rendered product signals were anchored to the selected item's purchase block."
            };
        """

        data = driver.execute_script(
            script,
            title,
            item_id,
        ) or {}

        strong_product_oos = [
            str(text).strip()
            for text in (
                data.get(
                    "productOosTexts"
                ) or []
            )
            if self._is_strong_product_oos_text(
                text
            )
        ]

        data["productOosTexts"] = (
            strong_product_oos
        )

        data["productOos"] = bool(
            strong_product_oos
        )

        (
            stock,
            quantity,
            reason,
            internal_conflict,
            oos_scope,
        ) = self.policy.classify_rendered_signals(
            data
        )

        price = self._candidate_price(
            data.get(
                "priceValues"
            ) or []
        )

        seller = self._seller_from_text(
            str(
                data.get(
                    "sellerText",
                    "",
                )
            )
        )

        price_candidate = (
            data.get(
                "priceCandidate"
            ) or {}
        )

        selector = str(
            price_candidate.get(
                "descriptor",
                "",
            )
        )

        details = {
            "internal_conflict":
                internal_conflict,

            "oos_scope":
                oos_scope,

            "region_found":
                bool(
                    data.get(
                        "regionFound"
                    )
                ),

            "region_top":
                data.get(
                    "regionTop"
                ),

            "region_bottom":
                data.get(
                    "regionBottom"
                ),

            "purchase_anchor_found":
                bool(
                    data.get(
                        "purchaseAnchorFound"
                    )
                ),

            "purchase_anchor_y":
                data.get(
                    "purchaseAnchorY"
                ),

            "purchase_top":
                data.get(
                    "purchaseTop"
                ),

            "purchase_bottom":
                data.get(
                    "purchaseBottom"
                ),

            "enabled_cta":
                bool(
                    data.get(
                        "enabledCta"
                    )
                ),

            "disabled_cta":
                bool(
                    data.get(
                        "disabledCta"
                    )
                ),

            "provisional_enabled_cta":
                bool(
                    data.get(
                        "provisionalEnabledCta"
                    )
                ),

            "provisional_disabled_cta":
                bool(
                    data.get(
                        "provisionalDisabledCta"
                    )
                ),

            "rejected_recommendation_cta":
                bool(
                    data.get(
                        "rejectedRecommendationCta"
                    )
                ),

            "cta_text":
                data.get(
                    "ctaText"
                ) or "",

            "cta_y":
                data.get(
                    "ctaY"
                ),

            "price_candidate":
                price_candidate,

            "price_candidates":
                data.get(
                    "priceCandidates"
                ) or [],

            "inventory_text":
                data.get(
                    "inventoryText"
                ) or "",

            "selected_option_oos":
                bool(
                    data.get(
                        "selectedOptionOos"
                    )
                ),

            "selected_oos_texts":
                data.get(
                    "selectedOosTexts"
                ) or [],

            "product_oos":
                bool(
                    data.get(
                        "productOos"
                    )
                ),

            "product_oos_texts":
                data.get(
                    "productOosTexts"
                ) or [],

            "generic_oos_texts":
                data.get(
                    "genericOosTexts"
                ) or [],

            "fulfillment":
                data.get(
                    "fulfillment"
                ) or {},
        }

        confidence = 0.0

        if internal_conflict:
            confidence = 0.20

        elif stock == StockState.IN_STOCK:
            confidence = (
                0.98
                if (
                    data.get(
                        "enabledCta"
                    ) and
                    data.get(
                        "purchaseAnchorFound"
                    ) and
                    price is not None
                )
                else 0.72
            )

        elif stock in {
            StockState.LOW_STOCK,
            StockState.LIMITED_STOCK,
            StockState.QUANTITY_REMAINING,
        }:
            confidence = (
                0.98
                if (
                    data.get(
                        "enabledCta"
                    ) and
                    data.get(
                        "purchaseAnchorFound"
                    )
                )
                else 0.80
            )

        elif stock == StockState.OOS:
            confidence = (
                0.98
                if (
                    data.get(
                        "purchaseAnchorFound"
                    ) and
                    (
                        data.get(
                            "productOos"
                        ) or
                        oos_scope ==
                            "all_fulfillment"
                    )
                )
                else 0.90
            )

        text_parts = [
            str(
                data.get(
                    "inventoryText",
                    "",
                )
            ),
            " | ".join(
                data.get(
                    "selectedOosTexts"
                ) or []
            ),
            " | ".join(
                data.get(
                    "productOosTexts"
                ) or []
            ),
            " | ".join(
                data.get(
                    "genericOosTexts"
                ) or []
            ),
        ]

        text = " | ".join(
            part
            for part in text_parts
            if part
        )

        return WalmartObservation(
            source="visual",
            stock=stock,
            price=price,
            quantity=quantity,
            confidence=confidence,
            title_match=bool(
                data.get(
                    "regionFound"
                ) and
                data.get(
                    "purchaseAnchorFound"
                )
            ),
            seller=seller,
            text=text,
            reason=reason,
            selector=selector,
            details=details,
        )

    @staticmethod
    def _is_strong_product_oos_text(
        value: Any,
    ) -> bool:
        """Accept only a standalone product-level unavailable phrase."""

        normalized = re.sub(
            r"\s+",
            " ",
            str(value or ""),
        ).strip().lower()

        return bool(
            re.fullmatch(
                r"(?:this item is )?"
                r"(?:out of stock|sold out|"
                r"currently unavailable|"
                r"unavailable|not available|"
                r"no longer available)"
                r"[.!]?",
                normalized,
            )
        )

    @staticmethod
    def _candidate_price(
        values: list[Any],
    ) -> Optional[Decimal]:
        """Return a price only when the primary rendered values agree."""

        parsed = [
            ScrapeResult.parse_price(
                value
            )
            for value in values
        ]

        parsed = [
            value
            for value in parsed
            if value is not None
        ]

        if not parsed:
            return None

        unique: list[Decimal] = []

        for value in parsed:
            if value not in unique:
                unique.append(value)

        return (
            unique[0]
            if len(unique) == 1
            else None
        )

    def _find_exact_items(
        self,
        value: Any,
        item_id: str,
        *,
        path: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        if isinstance(value, dict):
            if (
                self._matches_item_id(
                    value,
                    item_id,
                ) and
                self._product_like(value)
            ):
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
                normalized_key = re.sub(
                    r"[^a-z]",
                    "",
                    str(key).lower(),
                )

                if any(
                    token in normalized_key
                    for token in skipped
                ):
                    continue

                found.extend(
                    self._find_exact_items(
                        child,
                        item_id,
                        path=(
                            path +
                            (
                                str(key),
                            )
                        ),
                    )
                )

        elif isinstance(value, list):
            for index, child in enumerate(
                value
            ):
                found.extend(
                    self._find_exact_items(
                        child,
                        item_id,
                        path=(
                            path +
                            (
                                str(index),
                            )
                        ),
                    )
                )

        return found

    @staticmethod
    def _matches_item_id(
        item: dict[str, Any],
        item_id: str,
    ) -> bool:
        values = (
            item.get("usItemId"),
            item.get("itemId"),
            item.get("id"),
            item.get("productId"),
        )

        return any(
            value is not None and
            str(value).strip() == item_id
            for value in values
        )

    @staticmethod
    def _product_like(
        item: dict[str, Any],
    ) -> bool:
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
    def _item_score(
        item: dict[str, Any],
    ) -> int:
        score = 0

        for key, points in (
            (
                "availabilityStatus",
                5,
            ),
            (
                "priceInfo",
                5,
            ),
            (
                "buyBox",
                4,
            ),
            (
                "fulfillmentOptions",
                3,
            ),
            (
                "name",
                2,
            ),
            (
                "sellerDisplayName",
                1,
            ),
        ):
            if item.get(key) not in (
                None,
                "",
                [],
                {},
            ):
                score += points

        return score

    def _item_stock(
        self,
        item: dict[str, Any],
    ) -> tuple[
        StockState,
        str,
        bool,
    ]:
        availability = str(
            item.get(
                "availabilityStatus"
            )
            or item.get(
                "availability"
            )
            or ""
        ).strip().upper()

        buy_box = (
            item.get("buyBox") or {}
        )

        if not isinstance(
            buy_box,
            dict,
        ):
            buy_box = {}

        cta = (
            buy_box.get("cta")
            or item.get("cta")
            or {}
        )

        if isinstance(cta, dict):
            cta_text = str(
                cta.get("text")
                or cta.get(
                    "buttonText"
                )
                or cta.get("label")
                or ""
            ).strip().lower()
        else:
            cta_text = str(
                cta
            ).strip().lower()

        fulfillment_statuses: list[
            str
        ] = []

        for option in (
            item.get(
                "fulfillmentOptions"
            ) or []
        ):
            if not isinstance(
                option,
                dict,
            ):
                continue

            fulfillment_statuses.append(
                str(
                    option.get(
                        "availabilityStatus"
                    )
                    or option.get(
                        "availability"
                    )
                    or ""
                ).strip().upper()
            )

        fulfillment_statuses = [
            value
            for value
            in fulfillment_statuses
            if value
        ]

        explicit_in = (
            cta_text
            in {
                "add to cart",
                "add to basket",
            }
            or availability
            in {
                "IN_STOCK",
                "AVAILABLE",
                "AVAILABLE_TO_ORDER",
            }
            or any(
                value
                in {
                    "IN_STOCK",
                    "AVAILABLE",
                }
                for value
                in fulfillment_statuses
            )
        )

        all_fulfillment_unavailable = (
            len(
                fulfillment_statuses
            ) >= 2
            and all(
                value
                in {
                    "OUT_OF_STOCK",
                    "UNAVAILABLE",
                    "NOT_AVAILABLE",
                }
                for value
                in fulfillment_statuses
            )
        )

        explicit_oos = (
            item.get(
                "isOutOfStock"
            ) is True
            or buy_box.get(
                "isOutOfStock"
            ) is True
            or availability
            in {
                "OUT_OF_STOCK",
                "UNAVAILABLE",
                "SOLD_OUT",
                "PREORDER",
            }
            or all_fulfillment_unavailable
        )

        if (
            explicit_in and
            explicit_oos
        ):
            return (
                StockState.UNKNOWN,
                (
                    "the exact JSON item "
                    "contains contradictory "
                    "in-stock and out-of-stock "
                    "signals"
                ),
                True,
            )

        if explicit_in:
            return (
                StockState.IN_STOCK,
                (
                    "JSON availability "
                    "is in stock"
                ),
                False,
            )

        if explicit_oos:
            return (
                StockState.OOS,
                (
                    "JSON availability "
                    "is out of stock"
                ),
                False,
            )

        return (
            StockState.UNKNOWN,
            (
                "JSON availability "
                "is inconclusive"
            ),
            False,
        )

    def _item_price(
        self,
        item: dict[str, Any],
    ) -> Optional[Decimal]:
        candidates: list[Any] = []

        for container in (
            item.get("priceInfo"),
            item.get("price"),
            item.get("offerPrice"),
        ):
            if isinstance(
                container,
                dict,
            ):
                current = (
                    container.get(
                        "currentPrice"
                    ) or {}
                )

                if isinstance(
                    current,
                    dict,
                ):
                    candidates.extend(
                        (
                            current.get(
                                "price"
                            ),
                            current.get(
                                "priceString"
                            ),
                            current.get(
                                "displayValue"
                            ),
                            current.get(
                                "value"
                            ),
                        )
                    )

                candidates.extend(
                    (
                        container.get(
                            "price"
                        ),
                        container.get(
                            "value"
                        ),
                        container.get(
                            "displayValue"
                        ),
                    )
                )
            else:
                candidates.append(
                    container
                )

        for candidate in candidates:
            parsed = (
                ScrapeResult.parse_price(
                    candidate
                )
            )

            if parsed is not None:
                return parsed

        return None

    @staticmethod
    def _item_inventory_text(
        item: dict[str, Any],
    ) -> str:
        """Read inventory wording only from the exact selected item."""

        values: list[str] = []

        def add(value: Any) -> None:
            if value in (
                None,
                "",
                [],
                {},
            ):
                return

            if isinstance(
                value,
                (
                    str,
                    int,
                    float,
                    bool,
                ),
            ):
                normalized = re.sub(
                    r"\s+",
                    " ",
                    str(value),
                ).strip()

                if (
                    normalized and
                    normalized not in values
                ):
                    values.append(
                        normalized
                    )

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
            add(
                item.get(key)
            )

        buy_box = item.get(
            "buyBox"
        )

        if isinstance(
            buy_box,
            dict,
        ):
            for key in direct_keys:
                add(
                    buy_box.get(key)
                )

            cta = buy_box.get(
                "cta"
            )

            if isinstance(
                cta,
                dict,
            ):
                for key in (
                    "text",
                    "buttonText",
                    "label",
                ):
                    add(
                        cta.get(key)
                    )
            else:
                add(cta)

        for option in (
            item.get(
                "fulfillmentOptions"
            ) or []
        ):
            if not isinstance(
                option,
                dict,
            ):
                continue

            for key in direct_keys:
                add(
                    option.get(key)
                )

            for nested_key in (
                "availability",
                "inventory",
                "shippingOption",
                "pickupOption",
                "deliveryOption",
            ):
                nested = option.get(
                    nested_key
                )

                if not isinstance(
                    nested,
                    dict,
                ):
                    continue

                for key in direct_keys:
                    add(
                        nested.get(key)
                    )

        return " | ".join(values)

    @staticmethod
    def _item_quantity(
        item: dict[str, Any],
    ) -> Optional[int]:
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
                quantity = int(
                    float(value)
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            if quantity >= 0:
                return quantity

        return None

    @staticmethod
    def _item_seller(
        item: dict[str, Any],
    ) -> str:
        candidates = (
            item.get(
                "sellerDisplayName"
            ),
            item.get(
                "sellerName"
            ),
            (
                item.get("seller") or {}
            ).get(
                "displayName"
            )
            if isinstance(
                item.get("seller"),
                dict,
            )
            else item.get(
                "seller"
            ),
            (
                item.get("buyBox") or {}
            ).get(
                "sellerDisplayName"
            )
            if isinstance(
                item.get("buyBox"),
                dict,
            )
            else None,
        )

        for candidate in candidates:
            if candidate:
                return str(
                    candidate
                ).strip()

        return ""

    @staticmethod
    def _seller_from_text(
        text: str,
    ) -> str:
        match = re.search(
            r"(?:sold|shipped)\s+"
            r"(?:and\s+shipped\s+)?"
            r"by\s+([^\n|]{2,80})",
            text or "",
            re.I,
        )

        return (
            match.group(1).strip()
            if match
            else ""
        )

    @staticmethod
    def _decode_json(
        raw: str,
    ) -> Optional[dict[str, Any]]:
        if not raw:
            return None

        try:
            value = json.loads(raw)

            return (
                value
                if isinstance(
                    value,
                    dict,
                )
                else None
            )
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            pass

        start = raw.find("{")
        end = raw.rfind("}")

        if (
            start < 0 or
            end <= start
        ):
            return None

        try:
            value = json.loads(
                raw[
                    start:
                    end + 1
                ]
            )

            return (
                value
                if isinstance(
                    value,
                    dict,
                )
                else None
            )
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _item_id(
        url: str,
    ) -> Optional[str]:
        clean = (
            url
            .split(
                "?",
                1,
            )[0]
            .split(
                "#",
                1,
            )[0]
        )

        matches = re.findall(
            r"/(\d+)(?=/|$)",
            clean,
        )

        return (
            matches[-1]
            if matches
            else None
        )

    @staticmethod
    def _title(
        soup: BeautifulSoup,
    ) -> str:
        h1 = soup.find("h1")

        if (
            h1 and
            h1.get_text(
                " ",
                strip=True,
            )
        ):
            return h1.get_text(
                " ",
                strip=True,
            )

        meta = soup.find(
            "meta",
            attrs={
                "property":
                    "og:title",
            },
        )

        if (
            meta and
            meta.get("content")
        ):
            return str(
                meta["content"]
            ).strip()

        if (
            soup.title and
            soup.title.string
        ):
            return (
                soup.title.string
                .strip()
            )

        return "Unknown"

    @staticmethod
    def _blocked_reason(
        *,
        visible_text: str,
        title: str,
        final_url: str,
    ) -> str:
        text = (
            f"{title} "
            f"{final_url} "
            f"{visible_text[:12000]}"
        ).lower()

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
                return (
                    "Walmart blocked the "
                    f"page with {phrase!r}."
                )

        return ""

    def _error_result(
        self,
        *,
        url: str,
        item_id: str,
        title: str,
        target_variation: str,
        message: str,
        logs: list[
            tuple[str, str, str]
        ],
    ) -> ScrapeResult:
        self._append_log(
            logs,
            url,
            message,
            "error",
        )

        result = ScrapeResult(
            supplier="WAL",
            url=url,
            verification=(
                VerificationStatus.ERROR
            ),
            observed_stock=(
                StockState.UNKNOWN
            ),
            title=title,
            item_id=item_id,
            variant=target_variation,
            final_url=url,
            error_message=message,
            policy_reason=(
                "Scrape error cannot "
                "update the sheet."
            ),
        )

        self._emit_structured(
            result
        )

        return result

    def _emit_structured(
        self,
        result: ScrapeResult,
    ) -> None:
        if (
            self.structured_logger
            is None
        ):
            return

        try:
            self.structured_logger.emit(
                "walmart_result_resolved",
                phase=(
                    "supplier_verification"
                ),
                url=result.url,
                verification=(
                    result.verification.value
                ),
                observed_stock=(
                    result.observed_stock.value
                ),
                sheet_stock=(
                    result.sheet_stock or ""
                ),
                price=(
                    ScrapeResult.format_price(
                        result.price
                    )
                ),
                quantity=result.quantity,
                reason=(
                    result.policy_reason
                    or result.error_message
                ),
                level=(
                    "error"
                    if result.verification
                    in {
                        VerificationStatus.ERROR,
                        VerificationStatus.BLOCKED,
                    }
                    else "warning"
                    if result.verification !=
                        VerificationStatus.VERIFIED
                    else "info"
                ),
                extra={
                    "item_id":
                        result.item_id,
                    "title":
                        result.title,
                    "seller":
                        result.seller,
                    "final_url":
                        result.final_url,
                },
            )
        except OSError:
            pass

    def _append_log(
        self,
        logs: list[
            tuple[str, str, str]
        ],
        url: str,
        message: str,
        tag: str,
    ) -> None:
        logs.append(
            (
                f"{self.log_prefix}: "
                f"{message}",
                tag,
                url,
            )
        )

    @staticmethod
    def _observation_line(
        label: str,
        observation: WalmartObservation,
    ) -> str:
        price = (
            f"${observation.price:.2f}"
            if observation.price
            is not None
            else "[blank]"
        )

        return (
            f"{label} -> "
            f"Stock="
            f"{observation.stock.value} | "
            f"Price={price} | "
            f"Qty={observation.quantity} | "
            f"Confidence="
            f"{observation.confidence:.2f} | "
            f"Reason="
            f"{observation.reason}"
        )