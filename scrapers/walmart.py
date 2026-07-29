"""Exact-item Walmart scraper using an isolated Selenium tab.

This implementation deliberately avoids scanning Walmart's entire embedded
application state.  Broad recursive JSON scans can mix sibling variants,
alternate item conditions, recommendations, and location-specific fulfillment
records.  Instead, each row is checked in a fresh tab and resolved from the
selected product's primary purchase area.  Exact JSON-LD is used only as a
fallback/corroborating source when it can be tied to the linked Walmart item ID.

The public ``scrape`` method preserves the six-value interface expected by the
Supplier Stock Checker engine.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from selenium.common.exceptions import (
    JavascriptException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support.ui import WebDriverWait

from core.result_policy import apply_stock_policy
from core.scrape_result import ScrapeResult, StockState, VerificationStatus
from core.scraper_diagnostics import result_to_legacy_tuple


WALMART_SCRAPER_VERSION = "2026.07.28.scrapling-hybrid-v10.2.0"

_STOCK_IN = "In Stock"
_STOCK_OOS = "OOS"
_STOCK_LOW = "Low Stock"
_STOCK_LIMITED = "Limited Stock"
_STOCK_UNKNOWN = "Unable to Verify"

_LOW_STOCK_RE = re.compile(r"\blow\s+stock\b|\bfew\s+left\b", re.I)
_LIMITED_STOCK_RE = re.compile(r"\blimited\s+stock\b", re.I)
_QUANTITY_RE = re.compile(
    r"\bonly\s+(\d+)\s+(?:left|remaining|in\s+stock)\b|"
    r"\b(\d+)\s+left\s+in\s+stock\b",
    re.I,
)
_PRICE_RE = re.compile(r"\$\s*([0-9]{1,7}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)")
_ITEM_ID_RE = re.compile(r"/(?:ip/[^/?#]+/)?(\d{5,})(?:[/?#]|$)", re.I)


@dataclass(slots=True)
class _ResolvedSnapshot:
    status: str
    stock: str
    price: str
    title: str
    item_id: str
    reason: str
    evidence: dict[str, Any]

    @property
    def consensus_key(self) -> tuple[str, str, str, str]:
        price_key = "" if self.stock == _STOCK_OOS else self.price
        return self.status, self.stock, price_key, self.item_id


class WalmartScraper:
    """Check the exact Walmart link without leaking sibling-offer evidence."""

    _driver_lock = threading.RLock()

    def __init__(
        self,
        browser_manager: Any = None,
        logger_func: Any = None,
        **_: Any,
    ) -> None:
        self.browser_manager = browser_manager
        self.log = logger_func
        self.project_root = Path(__file__).resolve().parents[1]
        self.config_path = self.project_root / "config" / "walmart_playwright.json"
        self.config = self._read_config()

    def scrape(
        self,
        url: str,
        target_variation: str = "",
    ) -> tuple[
        str,
        str,
        str,
        str,
        list[dict[str, Any]],
        list[tuple[str, str, str]],
    ]:
        """Return ``status, price, stock, title, variants, logs``."""

        clean_url = str(url or "").strip()
        requested_item_id = self._extract_item_id(clean_url)

        if not clean_url or "walmart.com" not in clean_url.lower():
            return self._error(clean_url, "The link is not a valid Walmart product URL.")

        if not requested_item_id:
            return self._error(
                clean_url,
                "The Walmart item ID could not be read from this link.",
            )

        if self.browser_manager is None:
            return self._error(
                clean_url,
                "The Walmart Brave browser manager is not available.",
            )

        with self._driver_lock:
            try:
                return self._scrape_locked(
                    clean_url,
                    requested_item_id,
                    str(target_variation or "").strip(),
                )
            except TimeoutException:
                return self._error(
                    clean_url,
                    "Walmart did not finish loading the exact product purchase area.",
                )
            except WebDriverException as exc:
                return self._error(
                    clean_url,
                    f"The Walmart browser check failed: {self._safe_exception(exc)}",
                )
            except Exception as exc:  # Safety boundary for engine compatibility.
                return self._error(
                    clean_url,
                    f"The Walmart exact-item check failed: {self._safe_exception(exc)}",
                )

    def _scrape_locked(
        self,
        url: str,
        requested_item_id: str,
        target_variation: str,
    ) -> tuple[
        str,
        str,
        str,
        str,
        list[dict[str, Any]],
        list[tuple[str, str, str]],
    ]:
        driver = self.browser_manager.get_driver()
        original_handle = self._safe_current_handle(driver)
        created_handle: Optional[str] = None

        try:
            created_handle = self._open_background_tab(driver)
            self._switch_to_tab(driver, created_handle)
            self._keep_browser_minimized(driver)

            try:
                driver.execute_cdp_cmd("Network.clearBrowserCache", {})
            except Exception:
                pass

            driver.get("about:blank")
            self._keep_browser_minimized(driver)
            driver.get(url)
            self._keep_browser_minimized(driver)

            wait_seconds = self._navigation_timeout_seconds()
            WebDriverWait(driver, wait_seconds, poll_frequency=0.35).until(
                lambda current: self._page_has_exact_product_shell(
                    current,
                    requested_item_id,
                )
            )
            self._keep_browser_minimized(driver)

            resolved: Optional[_ResolvedSnapshot] = None
            raw_snapshots: list[dict[str, Any]] = []
            attempt_reasons: list[str] = []

            for navigation_attempt in range(self._max_navigation_attempts()):
                if navigation_attempt:
                    try:
                        driver.get("about:blank")
                        self._keep_browser_minimized(driver)
                        time.sleep(0.4)
                        driver.get(url)
                        self._keep_browser_minimized(driver)
                        WebDriverWait(
                            driver,
                            wait_seconds,
                            poll_frequency=0.35,
                        ).until(
                            lambda current: self._page_has_exact_product_shell(
                                current,
                                requested_item_id,
                            )
                        )
                        self._keep_browser_minimized(driver)
                    except Exception as exc:
                        attempt_reasons.append(
                            "Fresh-page retry failed: "
                            + self._safe_exception(exc)
                        )
                        continue

                candidate, candidate_snapshots = self._collect_consensus(
                    driver,
                    requested_item_id,
                )
                raw_snapshots.extend(candidate_snapshots)
                resolved = candidate

                if candidate.status == "Success":
                    break

                attempt_reasons.append(candidate.reason)
                if candidate.stock == "Captcha":
                    break

            if resolved is None:
                resolved = _ResolvedSnapshot(
                    status="Error",
                    stock=_STOCK_UNKNOWN,
                    price="",
                    title="Walmart item",
                    item_id=requested_item_id,
                    reason=(
                        attempt_reasons[-1]
                        if attempt_reasons
                        else "Walmart did not return an exact-item result."
                    ),
                    evidence={},
                )

            if resolved.status != "Success":
                if attempt_reasons:
                    resolved.evidence = {
                        **dict(resolved.evidence or {}),
                        "navigation_attempt_reasons": attempt_reasons,
                    }
                self._save_debug_evidence(
                    requested_item_id,
                    url,
                    raw_snapshots,
                    resolved.reason,
                    driver=driver,
                )
                review_result = self._resolved_to_result(
                    url=url,
                    target_variation=target_variation,
                    resolved=resolved,
                )
                return self._result_to_public_tuple(
                    review_result,
                    target_variation=target_variation,
                    logs=[
                        (
                            resolved.reason or "Walmart result requires review.",
                            "warning",
                            url,
                        )
                    ],
                )

            fixed_zip = str(self.config.get("fixed_zip_code") or "").strip()
            observed_location = self._observed_location(raw_snapshots)
            if fixed_zip and observed_location and fixed_zip not in observed_location:
                reason = (
                    "Walmart loaded a different shopping location than the configured "
                    f"ZIP {fixed_zip}. Current page location: {observed_location}."
                )
                self._save_debug_evidence(
                    requested_item_id,
                    url,
                    raw_snapshots,
                    reason,
                    driver=driver,
                )
                location_result = self._resolved_to_result(
                    url=url,
                    target_variation=target_variation,
                    resolved=_ResolvedSnapshot(
                        status="Error",
                        stock=_STOCK_UNKNOWN,
                        price="",
                        title=resolved.title,
                        item_id=resolved.item_id or requested_item_id,
                        reason=reason,
                        evidence=resolved.evidence,
                    ),
                )
                return self._result_to_public_tuple(
                    location_result,
                    target_variation=target_variation,
                    logs=[(reason, "warning", url)],
                )

            result = self._resolved_to_result(
                url=url,
                target_variation=target_variation,
                resolved=resolved,
            )
            logs: list[tuple[str, str, str]] = [
                (
                    (
                        "Walmart exact selected-offer check: "
                        f"item {resolved.item_id} | "
                        f"price {resolved.price or 'not shown'} | "
                        f"stock {resolved.stock}."
                    ),
                    "info",
                    url,
                )
            ]
            if resolved.reason:
                logs.append((resolved.reason, "info", url))
            return self._result_to_public_tuple(
                result,
                target_variation=target_variation,
                logs=logs,
            )
        finally:
            if created_handle:
                try:
                    if created_handle in driver.window_handles:
                        close_tab = getattr(
                            self.browser_manager,
                            "close_tab",
                            None,
                        )
                        if callable(close_tab):
                            close_tab(created_handle)
                        else:
                            driver.switch_to.window(created_handle)
                            driver.close()
                except Exception:
                    pass

            try:
                handles = driver.window_handles
                if original_handle and original_handle in handles:
                    self._switch_to_tab(
                        driver,
                        original_handle,
                    )
                elif handles:
                    self._switch_to_tab(
                        driver,
                        handles[0],
                    )
                self._keep_browser_minimized(driver)
            except Exception:
                pass

    def _open_background_tab(
        self,
        driver: Any,
    ) -> str:
        get_new_tab = getattr(
            self.browser_manager,
            "get_new_tab",
            None,
        )
        if callable(get_new_tab):
            return str(get_new_tab())

        driver.switch_to.new_window("tab")
        return str(driver.current_window_handle)

    def _switch_to_tab(
        self,
        driver: Any,
        handle: str,
    ) -> None:
        switch_to_tab = getattr(
            self.browser_manager,
            "switch_to_tab",
            None,
        )
        if callable(switch_to_tab):
            switch_to_tab(handle)
            return

        driver.switch_to.window(handle)

    def _keep_browser_minimized(
        self,
        driver: Any,
    ) -> None:
        keep_minimized = getattr(
            self.browser_manager,
            "keep_browser_minimized",
            None,
        )
        if not callable(keep_minimized):
            return

        try:
            keep_minimized(driver)
        except Exception:
            # Window-state protection must never turn a stock check into an
            # application error.
            pass

    def _collect_consensus(
        self,
        driver: Any,
        requested_item_id: str,
    ) -> tuple[_ResolvedSnapshot, list[dict[str, Any]]]:
        settle_deadline = time.monotonic() + self._settle_timeout_seconds()
        raw_snapshots: list[dict[str, Any]] = []
        resolved_snapshots: list[_ResolvedSnapshot] = []

        while time.monotonic() < settle_deadline and len(raw_snapshots) < 6:
            raw = self._extract_snapshot(driver, requested_item_id)
            raw_snapshots.append(raw)
            resolved = self._resolve_snapshot(raw, requested_item_id)
            resolved_snapshots.append(resolved)

            if self._has_two_matching_successes(resolved_snapshots):
                winner = self._majority_success(resolved_snapshots)
                if winner is not None:
                    winner.evidence = {
                        **winner.evidence,
                        "scraper_version": WALMART_SCRAPER_VERSION,
                        "snapshot_count": len(raw_snapshots),
                        "consensus": "two matching exact-item snapshots",
                        "raw_snapshots": raw_snapshots[-3:],
                    }
                    return winner, raw_snapshots

            if self._has_captcha(raw):
                result = _ResolvedSnapshot(
                    status="Error",
                    stock="Captcha",
                    price="",
                    title=str(raw.get("title") or "Walmart item"),
                    item_id=requested_item_id,
                    reason="Captcha or robot verification blocked the Walmart page.",
                    evidence={"raw_snapshots": raw_snapshots},
                )
                return result, raw_snapshots

            time.sleep(self._snapshot_gap_seconds())

        winner = self._majority_success(resolved_snapshots)
        if winner is not None:
            winner.evidence = {
                **winner.evidence,
                "scraper_version": WALMART_SCRAPER_VERSION,
                "snapshot_count": len(raw_snapshots),
                "consensus": "majority exact-item snapshots",
                "raw_snapshots": raw_snapshots[-3:],
            }
            return winner, raw_snapshots

        last = resolved_snapshots[-1] if resolved_snapshots else None
        reason = self._consensus_failure_reason(resolved_snapshots)
        return (
            _ResolvedSnapshot(
                status="Error",
                stock=_STOCK_UNKNOWN,
                price="",
                title=(last.title if last else "Walmart item"),
                item_id=requested_item_id,
                reason=reason,
                evidence={
                    "scraper_version": WALMART_SCRAPER_VERSION,
                    "raw_snapshots": raw_snapshots[-4:],
                },
            ),
            raw_snapshots,
        )

    @classmethod
    def _resolve_snapshot(
        cls,
        raw: dict[str, Any],
        requested_item_id: str,
    ) -> _ResolvedSnapshot:
        title = str(raw.get("title") or "Walmart item").strip()
        final_url = str(raw.get("pageUrl") or "")
        page_item_id = str(raw.get("pageItemId") or cls._extract_item_id(final_url))
        price = cls._normalize_price(raw.get("priceText"))
        price_exact = bool(raw.get("priceExact", True))
        price_conflict = bool(raw.get("priceConflict"))
        purchase_control_exact = bool(raw.get("purchaseControlExact", True))
        enabled_purchase = (
            bool(raw.get("enabledPurchase"))
            and purchase_control_exact
        )
        selected_option_exact = bool(
            raw.get("selectedOptionExact", True)
        )
        selected_option_oos = (
            bool(raw.get("selectedOptionOos"))
            and selected_option_exact
        )
        product_oos_exact = bool(
            raw.get("productOosExact", True)
        )
        product_oos = (
            bool(raw.get("productOos"))
            and product_oos_exact
        )
        all_fulfillment_unavailable = bool(raw.get("allFulfillmentUnavailable"))
        identity_conflict = bool(raw.get("identityConflict"))
        low_text = cls._first_text(raw.get("lowStockTexts"))
        low_stock_exact = bool(raw.get("lowStockExact", True))
        json_ld = raw.get("jsonLdExact") or {}
        json_ld_stock = cls._json_ld_stock(json_ld.get("availability"))
        json_ld_price = cls._normalize_price(json_ld.get("price"))
        json_ld_conflict = bool(raw.get("jsonLdConflict"))

        base_evidence = {
            "requested_item_id": requested_item_id,
            "page_item_id": page_item_id,
            "page_url": final_url,
            "title": title,
            "purchase_control": raw.get("purchaseControl") or {},
            "purchase_control_exact": purchase_control_exact,
            "identity_ids": raw.get("identityIds") or [],
            "identity_conflict": identity_conflict,
            "price_text": raw.get("priceText") or "",
            "price_exact": price_exact,
            "price_conflict": price_conflict,
            "price_candidates": raw.get("priceCandidates") or [],
            "low_stock_texts": raw.get("lowStockTexts") or [],
            "low_stock_exact": low_stock_exact,
            "product_oos_texts": raw.get("productOosTexts") or [],
            "product_oos_exact": product_oos_exact,
            "selected_option_oos": selected_option_oos,
            "selected_option_exact": selected_option_exact,
            "fulfillment": raw.get("fulfillment") or {},
            "json_ld_exact": json_ld,
            "json_ld_conflict": json_ld_conflict,
            "shopping_location": raw.get("locationText") or "",
        }

        if cls._has_captcha(raw):
            return _ResolvedSnapshot(
                status="Error",
                stock="Captcha",
                price="",
                title=title,
                item_id=requested_item_id,
                reason="Captcha or robot verification blocked the Walmart page.",
                evidence=base_evidence,
            )

        if identity_conflict:
            return _ResolvedSnapshot(
                status="Error",
                stock=_STOCK_UNKNOWN,
                price="",
                title=title,
                item_id=requested_item_id,
                reason=(
                    "Walmart exposed conflicting product identity signals on "
                    "the page. The row was held instead of using a sibling item."
                ),
                evidence=base_evidence,
            )

        if not page_item_id or page_item_id != requested_item_id:
            return _ResolvedSnapshot(
                status="Error",
                stock=_STOCK_UNKNOWN,
                price="",
                title=title,
                item_id=requested_item_id,
                reason=(
                    "Walmart did not remain on the exact linked item ID. "
                    f"Expected {requested_item_id}; page showed {page_item_id or 'unknown'}."
                ),
                evidence=base_evidence,
            )

        quantity_match = _QUANTITY_RE.search(low_text)
        if enabled_purchase and low_stock_exact and quantity_match:
            quantity = quantity_match.group(1) or quantity_match.group(2)
            stock = f"Only {int(quantity)} Remaining"
            return _ResolvedSnapshot(
                status="Success",
                stock=stock,
                price=price,
                title=title,
                item_id=requested_item_id,
                reason=(
                    "The exact selected offer has an enabled purchase control "
                    f"and reports only {int(quantity)} remaining."
                ),
                evidence=base_evidence,
            )

        if (
            enabled_purchase
            and low_stock_exact
            and _LIMITED_STOCK_RE.search(low_text)
        ):
            return _ResolvedSnapshot(
                status="Success",
                stock=_STOCK_LIMITED,
                price=price,
                title=title,
                item_id=requested_item_id,
                reason=(
                    "The exact selected offer has an enabled purchase control "
                    "and reports Limited Stock."
                ),
                evidence=base_evidence,
            )

        if (
            enabled_purchase
            and low_stock_exact
            and _LOW_STOCK_RE.search(low_text)
        ):
            return _ResolvedSnapshot(
                status="Success",
                stock=_STOCK_LOW,
                price=price,
                title=title,
                item_id=requested_item_id,
                reason=(
                    "The exact selected offer has an enabled purchase control "
                    "and reports Low Stock."
                ),
                evidence=base_evidence,
            )

        if selected_option_oos and not enabled_purchase:
            return _ResolvedSnapshot(
                status="Success",
                stock=_STOCK_OOS,
                price="",
                title=title,
                item_id=requested_item_id,
                reason="The selected option for the exact Walmart item is out of stock.",
                evidence=base_evidence,
            )

        # A strict, exact Add to cart/Buy now control is decisive only when
        # the rendered primary offer also provides its own current price.
        # JSON-LD is recorded as corroboration, but it is never allowed to
        # create an In Stock result by itself because Walmart can leave stale
        # offers or sibling-variant state in structured data.
        if enabled_purchase:
            if price_conflict:
                return _ResolvedSnapshot(
                    status="Error",
                    stock=_STOCK_UNKNOWN,
                    price="",
                    title=title,
                    item_id=requested_item_id,
                    reason=(
                        "Multiple current prices were found inside the exact "
                        "selected offer. The row was held instead of choosing "
                        "one price."
                    ),
                    evidence=base_evidence,
                )

            if price and not price_exact:
                return _ResolvedSnapshot(
                    status="Error",
                    stock=_STOCK_UNKNOWN,
                    price="",
                    title=title,
                    item_id=requested_item_id,
                    reason=(
                        "A rendered price was visible, but it could not be "
                        "bound to the same exact offer as the purchase control."
                    ),
                    evidence=base_evidence,
                )

            if not price:
                return _ResolvedSnapshot(
                    status="Error",
                    stock=_STOCK_UNKNOWN,
                    price="",
                    title=title,
                    item_id=requested_item_id,
                    reason=(
                        "The exact selected offer has an enabled purchase control, "
                        "but its rendered current price could not be verified. "
                        "Structured data alone was not trusted."
                    ),
                    evidence=base_evidence,
                )

            json_note = ""
            if json_ld_conflict:
                json_note = " Conflicting JSON-LD was ignored."
            elif json_ld_stock == _STOCK_OOS:
                json_note = " Stale JSON-LD OOS was ignored."
            elif json_ld_price and json_ld_price != price:
                json_note = " A different JSON-LD price was ignored."

            return _ResolvedSnapshot(
                status="Success",
                stock=_STOCK_IN,
                price=price,
                title=title,
                item_id=requested_item_id,
                reason=(
                    "The exact selected offer has an enabled primary purchase "
                    "control and a rendered current price." + json_note
                ),
                evidence=base_evidence,
            )

        if product_oos:
            return _ResolvedSnapshot(
                status="Success",
                stock=_STOCK_OOS,
                price="",
                title=title,
                item_id=requested_item_id,
                reason=(
                    "The exact selected offer explicitly reports Out of stock "
                    "and has no enabled exact purchase control."
                ),
                evidence=base_evidence,
            )

        if all_fulfillment_unavailable:
            return _ResolvedSnapshot(
                status="Error",
                stock=_STOCK_UNKNOWN,
                price="",
                title=title,
                item_id=requested_item_id,
                reason=(
                    "All visible fulfillment methods are unavailable for the "
                    "current shopping location, but Walmart did not show "
                    "product-level OOS. The row was held because fulfillment "
                    "availability can be location-specific."
                ),
                evidence=base_evidence,
            )

        if json_ld_stock != _STOCK_UNKNOWN or json_ld_price:
            return _ResolvedSnapshot(
                status="Error",
                stock=_STOCK_UNKNOWN,
                price="",
                title=title,
                item_id=requested_item_id,
                reason=(
                    "Only exact-item JSON-LD produced an availability signal. "
                    "The rendered selected offer was inconclusive, so the row "
                    "was held instead of trusting possibly stale structured data."
                ),
                evidence=base_evidence,
            )

        return _ResolvedSnapshot(
            status="Error",
            stock=_STOCK_UNKNOWN,
            price="",
            title=title,
            item_id=requested_item_id,
            reason=(
                "The exact selected Walmart offer did not provide enough stable "
                "purchase evidence. The row was held instead of guessing."
            ),
            evidence=base_evidence,
        )

    def _extract_snapshot(
        self,
        driver: Any,
        requested_item_id: str,
    ) -> dict[str, Any]:
        try:
            value = driver.execute_script(_EXACT_SELECTED_OFFER_SCRIPT, requested_item_id)
        except JavascriptException as exc:
            return {
                "pageUrl": getattr(driver, "current_url", ""),
                "title": "Walmart item",
                "scriptError": self._safe_exception(exc),
            }

        return value if isinstance(value, dict) else {}

    @classmethod
    def _page_has_exact_product_shell(
        cls,
        driver: Any,
        requested_item_id: str,
    ) -> bool:
        try:
            state = driver.execute_script(
                """
                const requested = String(arguments[0] || '');
                const url = String(location.href || '');
                const body = String(document.body?.innerText || '').toLowerCase();
                const title = String(document.querySelector('main h1, [role="main"] h1, h1')?.textContent || '').trim();
                const hasId = url.includes('/' + requested) || url.includes(requested);
                const blocked = /verify you are human|robot or human|press and hold|access denied|captcha/.test(body);
                return Boolean(blocked || (hasId && title.length >= 3));
                """,
                requested_item_id,
            )
            return bool(state)
        except Exception:
            return False

    @staticmethod
    def _has_two_matching_successes(
        snapshots: list[_ResolvedSnapshot],
    ) -> bool:
        keys = [item.consensus_key for item in snapshots if item.status == "Success"]
        counts = Counter(keys)
        return any(count >= 2 for count in counts.values())

    @staticmethod
    def _majority_success(
        snapshots: list[_ResolvedSnapshot],
    ) -> Optional[_ResolvedSnapshot]:
        successes = [item for item in snapshots if item.status == "Success"]
        if not successes:
            return None

        counts = Counter(item.consensus_key for item in successes)
        key, count = counts.most_common(1)[0]
        required = 2 if len(snapshots) >= 2 else 1

        if count < required:
            return None

        for item in reversed(successes):
            if item.consensus_key == key:
                return item

        return None

    @staticmethod
    def _consensus_failure_reason(
        snapshots: list[_ResolvedSnapshot],
    ) -> str:
        success_states = [
            f"{item.stock} @ {item.price or 'no price'}"
            for item in snapshots
            if item.status == "Success"
        ]
        if len(set(success_states)) > 1:
            return (
                "Walmart changed the exact selected-offer evidence while the page "
                "was settling. Conflicting snapshots: " + "; ".join(success_states[-4:])
            )

        errors = [item.reason for item in snapshots if item.reason]
        if errors:
            return errors[-1]

        return "Walmart did not produce two matching exact-item snapshots."

    @staticmethod
    def _first_text(value: Any) -> str:
        if isinstance(value, list):
            for item in value:
                text = str(item or "").strip()
                if text:
                    return text
        return str(value or "").strip()

    @staticmethod
    def _normalize_price(value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, (int, float, Decimal)):
            try:
                amount = Decimal(str(value))
            except InvalidOperation:
                return ""
            return f"{amount:.2f}"

        text = str(value).replace("\u00a0", " ").strip()
        match = _PRICE_RE.search(text)
        if not match:
            plain = re.fullmatch(r"\s*([0-9]{1,7}(?:\.[0-9]{1,2})?)\s*", text)
            if not plain:
                return ""
            numeric = plain.group(1)
        else:
            numeric = match.group(1).replace(",", "")

        try:
            amount = Decimal(numeric)
        except InvalidOperation:
            return ""

        if amount <= 0 or amount > Decimal("1000000"):
            return ""

        return f"{amount:.2f}"

    @staticmethod
    def _json_ld_stock(value: Any) -> str:
        text = str(value or "").lower()
        if "outofstock" in text or "soldout" in text or "discontinued" in text:
            return _STOCK_OOS
        if "instock" in text or "preorder" in text or "backorder" in text:
            return _STOCK_IN
        return _STOCK_UNKNOWN

    @staticmethod
    def _extract_item_id(url: str) -> str:
        try:
            parsed = urlparse(str(url or ""))
            segments = [part for part in parsed.path.split("/") if part]
            for index, part in enumerate(segments):
                if part.lower() == "ip":
                    for candidate in reversed(segments[index + 1 :]):
                        if candidate.isdigit() and len(candidate) >= 5:
                            return candidate
            for candidate in reversed(segments):
                if candidate.isdigit() and len(candidate) >= 5:
                    return candidate
        except Exception:
            pass

        match = _ITEM_ID_RE.search(str(url or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _has_captcha(raw: dict[str, Any]) -> bool:
        return bool(raw.get("captcha"))

    @staticmethod
    def _safe_current_handle(driver: Any) -> Optional[str]:
        try:
            return str(driver.current_window_handle)
        except Exception:
            return None

    @staticmethod
    def _safe_exception(exc: BaseException) -> str:
        message = re.sub(r"\s+", " ", str(exc or "")).strip()
        return message[:800] or type(exc).__name__

    @staticmethod
    def _observed_location(raw_snapshots: list[dict[str, Any]]) -> str:
        values = [
            str(item.get("locationText") or "").strip()
            for item in raw_snapshots
            if isinstance(item, dict) and str(item.get("locationText") or "").strip()
        ]
        if not values:
            return ""
        return Counter(values).most_common(1)[0][0]

    @staticmethod
    def _stock_state_from_text(stock: Any, reason: Any = "") -> tuple[StockState, Optional[int]]:
        stock_text = str(stock or "").strip()
        combined = f"{stock_text} {str(reason or '')}".strip()
        quantity_match = _QUANTITY_RE.search(combined)
        if quantity_match:
            quantity = int(quantity_match.group(1) or quantity_match.group(2))
            return StockState.QUANTITY_REMAINING, quantity
        if _LIMITED_STOCK_RE.search(combined):
            return StockState.LIMITED_STOCK, None
        if _LOW_STOCK_RE.search(combined):
            return StockState.LOW_STOCK, None
        normalized = stock_text.lower()
        if normalized in {"oos", "out of stock", "out-of-stock"}:
            return StockState.OOS, None
        if normalized in {"in stock", "instock", "available"}:
            return StockState.IN_STOCK, None
        return StockState.UNKNOWN, None

    @staticmethod
    def _normalize_match_tokens(value: Any) -> set[str]:
        text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
        ignored = {
            "add", "to", "cart", "buy", "now", "the", "a", "an", "for",
            "with", "and", "of", "in", "on", "item", "product",
        }
        return {
            token
            for token in text.split()
            if len(token) >= 2 and token not in ignored
        }

    @classmethod
    def _cta_matches_title(cls, title: Any, cart_label: Any) -> bool:
        title_tokens = cls._normalize_match_tokens(title)
        cta_tokens = cls._normalize_match_tokens(cart_label)
        if not title_tokens or not cta_tokens:
            return False
        ordered_title_tokens = [
            token
            for token in re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).split()
            if token in title_tokens
        ]
        identity_anchor = ordered_title_tokens[0] if ordered_title_tokens else ""
        if identity_anchor and identity_anchor not in cta_tokens:
            return False
        shared = title_tokens & cta_tokens
        # Require enough exact product-language overlap to reject recommendation
        # cards that happen to share a size or one generic product word.
        required = max(2, min(5, (len(title_tokens) + 2) // 3))
        return len(shared) >= required and len(shared) / max(1, len(title_tokens)) >= 0.35

    @staticmethod
    def _fulfillment_texts(dom: dict[str, Any]) -> list[str]:
        fulfillment = dom.get("fulfillment") or {}
        if not isinstance(fulfillment, dict):
            return []
        return [
            str(value or "").strip()
            for value in fulfillment.values()
            if str(value or "").strip()
        ]

    @staticmethod
    def _is_unavailable_fulfillment(text: str) -> bool:
        lowered = str(text or "").lower()
        return any(
            token in lowered
            for token in (
                "not available", "unavailable", "out of stock", "sold out",
                "not eligible", "cannot be shipped",
            )
        )

    @classmethod
    def _all_fulfillment_unavailable(cls, dom: dict[str, Any]) -> bool:
        values = cls._fulfillment_texts(dom)
        return bool(values) and all(cls._is_unavailable_fulfillment(value) for value in values)

    @classmethod
    def _has_available_shipping(cls, dom: dict[str, Any]) -> bool:
        fulfillment = dom.get("fulfillment") or {}
        if not isinstance(fulfillment, dict):
            return False
        shipping = str(fulfillment.get("shipping") or "").strip()
        if not shipping or cls._is_unavailable_fulfillment(shipping):
            return False
        lowered = shipping.lower()
        return any(
            token in lowered
            for token in ("arrives", "shipping", "delivery date", "free")
        )

    @staticmethod
    def _exact_oos_text_present(dom: dict[str, Any]) -> bool:
        texts = dom.get("exactStockTexts") or []
        if not isinstance(texts, (list, tuple)):
            texts = [texts]
        return any(
            re.search(r"\bout\s+of\s+stock\b|\bsold\s+out\b", str(item or ""), re.I)
            for item in texts
        )

    @classmethod
    def _payload_to_result(
        cls,
        url: str,
        target_variation: str,
        payload: dict[str, Any],
        logs: list[tuple[str, str, str]],
    ) -> ScrapeResult:
        """Convert old/runtime payloads into the one structured Walmart contract.

        This compatibility bridge is intentionally conservative. It keeps a
        trustworthy price inside ``ScrapeResult`` for verification, but marks
        Walmart as stock-only so the engine never proposes a Walmart price
        change to the sheet.
        """
        data = payload if isinstance(payload, dict) else {}
        evidence = data.get("evidence") or {}
        evidence = evidence if isinstance(evidence, dict) else {}
        dom = evidence.get("dom") or {}
        dom = dom if isinstance(dom, dict) else {}
        title = str(data.get("title") or dom.get("title") or "Walmart item").strip()
        item_id = str(data.get("itemId") or cls._extract_item_id(url) or "").strip()
        final_url = str(evidence.get("pageUrl") or url or "").strip()
        reason = str(data.get("reason") or "").strip()
        status_text = str(data.get("status") or "Error").strip().lower()
        raw_stock = str(data.get("stock") or "Unable to Verify").strip()
        raw_price = data.get("price")
        if raw_price in (None, ""):
            raw_price = dom.get("priceText")
        parsed_price = ScrapeResult.parse_price(raw_price)

        metadata = {
            "walmart_stock_only_mode": True,
            "scraper_version": WALMART_SCRAPER_VERSION,
            "runtime_version": str(evidence.get("runtimeVersion") or ""),
            "browser_mode": str(evidence.get("browserMode") or ""),
            "legacy_payload": data,
            "logs_received": len(logs or []),
        }

        if status_text != "success":
            lowered_reason = reason.lower()
            verification = (
                VerificationStatus.BLOCKED
                if any(token in lowered_reason for token in ("captcha", "robot", "human verification"))
                else VerificationStatus.ERROR
            )
            result = ScrapeResult(
                supplier="WAL",
                url=url,
                verification=verification,
                observed_stock=StockState.UNKNOWN,
                title=title,
                item_id=item_id,
                final_url=final_url,
                variant=target_variation,
                error_message=reason or "The Walmart page could not be verified.",
                policy_reason=reason or "The Walmart page could not be verified.",
                metadata=metadata,
            )
            result.add_evidence(
                source="walmart_runtime",
                field_name="status",
                value=data.get("status") or "Error",
                item_id=item_id or None,
                confidence=1.0,
                details=evidence,
            )
            return result

        state, quantity = cls._stock_state_from_text(raw_stock, reason)
        fulfillment_text = " ".join(cls._fulfillment_texts(dom))
        fulfillment_state, fulfillment_quantity = cls._stock_state_from_text(
            fulfillment_text,
            reason,
        )
        if fulfillment_state in {
            StockState.LOW_STOCK,
            StockState.LIMITED_STOCK,
            StockState.QUANTITY_REMAINING,
        }:
            state = fulfillment_state
            quantity = fulfillment_quantity

        cart_label = str(dom.get("cartControlLabel") or "").strip()
        enabled_add = bool(dom.get("enabledAdd") or dom.get("buyNowEnabled"))
        cta_matches = cls._cta_matches_title(title, cart_label) if cart_label else False
        exact_oos = cls._exact_oos_text_present(dom)
        all_unavailable = cls._all_fulfillment_unavailable(dom)
        shipping_available = cls._has_available_shipping(dom)

        # Explicit low/limited/quantity rules always become verified sheet OOS.
        if state in {
            StockState.LOW_STOCK,
            StockState.LIMITED_STOCK,
            StockState.QUANTITY_REMAINING,
        }:
            verification = VerificationStatus.VERIFIED
        elif state == StockState.OOS:
            verification = VerificationStatus.VERIFIED
        elif cart_label and not cta_matches:
            if exact_oos and all_unavailable:
                state = StockState.OOS
                verification = VerificationStatus.VERIFIED
                parsed_price = None
                reason = reason or "Exact-item OOS evidence overrides a mismatched recommendation CTA."
            else:
                state = StockState.UNKNOWN
                verification = VerificationStatus.CONFLICT
                parsed_price = None
                reason = (
                    "The visible purchase control belongs to a different product and "
                    "the exact linked item did not provide complete OOS evidence."
                )
        elif state == StockState.IN_STOCK or (enabled_add and cta_matches) or shipping_available:
            state = StockState.IN_STOCK
            if parsed_price is None:
                verification = VerificationStatus.PARTIAL
                reason = (
                    "The exact linked item appears In Stock, but its current rendered "
                    "price could not be verified."
                )
            else:
                verification = VerificationStatus.VERIFIED
        elif exact_oos and all_unavailable:
            state = StockState.OOS
            parsed_price = None
            verification = VerificationStatus.VERIFIED
        else:
            state = StockState.UNKNOWN
            parsed_price = None
            verification = VerificationStatus.UNKNOWN
            reason = reason or "The exact linked item did not provide a stable inventory decision."

        result = ScrapeResult(
            supplier="WAL",
            url=url,
            verification=verification,
            observed_stock=state,
            price=(
                parsed_price
                if state == StockState.IN_STOCK and verification == VerificationStatus.VERIFIED
                else None
            ),
            quantity=quantity,
            title=title,
            item_id=item_id,
            final_url=final_url,
            variant=target_variation,
            policy_reason=reason,
            metadata=metadata,
        )
        result.add_evidence(
            source="walmart_runtime",
            field_name="payload",
            value=raw_stock,
            item_id=item_id or None,
            confidence=1.0 if verification == VerificationStatus.VERIFIED else 0.5,
            details={
                "cart_label": cart_label,
                "cta_matches_title": cta_matches,
                "exact_oos": exact_oos,
                "all_fulfillment_unavailable": all_unavailable,
                "shipping_available": shipping_available,
                "dom": dom,
            },
        )
        return apply_stock_policy(result)

    @classmethod
    def _resolved_to_result(
        cls,
        *,
        url: str,
        target_variation: str,
        resolved: _ResolvedSnapshot,
    ) -> ScrapeResult:
        state, quantity = cls._stock_state_from_text(resolved.stock, resolved.reason)
        if resolved.status == "Success":
            verification = VerificationStatus.VERIFIED
        else:
            reason_lower = str(resolved.reason or "").lower()
            if resolved.stock == "Captcha" or any(
                token in reason_lower for token in ("captcha", "robot verification", "human verification")
            ):
                verification = VerificationStatus.BLOCKED
            elif any(
                token in reason_lower
                for token in (
                    "conflicting", "different shopping location", "did not remain on the exact",
                    "could not be bound", "multiple current prices", "inconclusive",
                    "not provide enough stable", "only exact-item json-ld",
                    "location-specific",
                )
            ):
                verification = VerificationStatus.CONFLICT
            else:
                verification = VerificationStatus.ERROR

        result = ScrapeResult(
            supplier="WAL",
            url=url,
            verification=verification,
            observed_stock=state,
            price=(
                ScrapeResult.parse_price(resolved.price)
                if verification == VerificationStatus.VERIFIED and state == StockState.IN_STOCK
                else None
            ),
            quantity=quantity,
            title=resolved.title,
            item_id=resolved.item_id,
            final_url=str((resolved.evidence or {}).get("page_url") or url),
            variant=target_variation,
            policy_reason=resolved.reason,
            error_message=(
                resolved.reason
                if verification in {VerificationStatus.ERROR, VerificationStatus.BLOCKED}
                else ""
            ),
            metadata={
                "walmart_stock_only_mode": True,
                "scraper_version": WALMART_SCRAPER_VERSION,
                "exact_offer_binding": True,
                "raw_evidence": resolved.evidence,
            },
        )
        result.add_evidence(
            source="walmart_exact_offer",
            field_name="resolved_snapshot",
            value=resolved.stock,
            item_id=resolved.item_id or None,
            confidence=1.0 if verification == VerificationStatus.VERIFIED else 0.5,
            details=resolved.evidence,
        )
        return apply_stock_policy(result)

    @staticmethod
    def _result_to_public_tuple(
        result: ScrapeResult,
        *,
        target_variation: str = "",
        logs: Optional[list[tuple[str, str, str]]] = None,
    ) -> tuple[
        str,
        str,
        str,
        str,
        list[dict[str, Any]],
        list[tuple[str, str, str]],
    ]:
        legacy = result_to_legacy_tuple(
            result,
            target_variation=target_variation,
            logs=logs,
        )
        status, _price, stock, title, variants, output_logs = legacy
        # Walmart prices remain internal evidence only. The structured result
        # keeps the verified price so the safety gate can validate In Stock,
        # while the public tuple cannot trigger a sheet price update.
        public_variants = []
        for variant in variants:
            item = dict(variant)
            item["price"] = ""
            item["source"] = "walmart_exact_offer_structured_v10_2"
            public_variants.append(item)
        return status, "", stock, title, public_variants, output_logs

    def _read_config(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "navigation_timeout_ms": 45000,
            "settle_timeout_ms": 12000,
            "snapshot_gap_ms": 1200,
            "max_navigation_attempts": 2,
            "fixed_zip_code": "",
            "save_debug_evidence": True,
        }
        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return defaults

    def _navigation_timeout_seconds(self) -> float:
        try:
            milliseconds = int(self.config.get("navigation_timeout_ms", 45000))
        except (TypeError, ValueError):
            milliseconds = 45000
        return max(15.0, min(90.0, milliseconds / 1000.0))

    def _settle_timeout_seconds(self) -> float:
        try:
            milliseconds = int(self.config.get("settle_timeout_ms", 12000))
        except (TypeError, ValueError):
            milliseconds = 12000
        return max(4.0, min(30.0, milliseconds / 1000.0))

    def _max_navigation_attempts(self) -> int:
        try:
            value = int(self.config.get("max_navigation_attempts", 2))
        except (TypeError, ValueError):
            value = 2
        return max(1, min(3, value))

    def _snapshot_gap_seconds(self) -> float:
        try:
            milliseconds = int(self.config.get("snapshot_gap_ms", 1200))
        except (TypeError, ValueError):
            milliseconds = 1200
        return max(0.6, min(3.0, milliseconds / 1000.0))

    def _save_debug_evidence(
        self,
        item_id: str,
        url: str,
        snapshots: list[dict[str, Any]],
        reason: str,
        *,
        driver: Any = None,
    ) -> None:
        if not bool(self.config.get("save_debug_evidence", True)):
            return

        try:
            debug_dir = self.project_root / "logs" / "walmart_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = debug_dir / f"{item_id}_{stamp}.json"
            screenshot_path = ""
            if (
                driver is not None
                and bool(self.config.get("screenshots_on_error", True))
            ):
                try:
                    screenshot = debug_dir / f"{item_id}_{stamp}.png"
                    if driver.save_screenshot(str(screenshot)):
                        screenshot_path = str(screenshot)
                except Exception:
                    screenshot_path = ""

            path.write_text(
                json.dumps(
                    {
                        "scraper_version": WALMART_SCRAPER_VERSION,
                        "url": url,
                        "item_id": item_id,
                        "reason": reason,
                        "screenshot": screenshot_path,
                        "snapshots": snapshots,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    @classmethod
    def _error(
        cls,
        url: str,
        message: str,
        *,
        title: str = "Walmart item",
        evidence: Any = None,
        stock: str = _STOCK_UNKNOWN,
    ) -> tuple[
        str,
        str,
        str,
        str,
        list[dict[str, Any]],
        list[tuple[str, str, str]],
    ]:
        safe_message = str(message or "Unable to verify Walmart item.").strip()
        try:
            project_root = Path(__file__).resolve().parents[1]
            log_path = project_root / "logs" / "walmart_runtime_errors.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(
                    f"URL: {url}\n"
                    f"ERROR: {safe_message}\n"
                    + ("-" * 80)
                    + "\n"
                )
        except OSError:
            pass

        resolved = _ResolvedSnapshot(
            status="Error",
            stock=stock,
            price="",
            title=title,
            item_id=cls._extract_item_id(url),
            reason=safe_message,
            evidence=dict(evidence or {}) if isinstance(evidence, dict) else {"raw": evidence},
        )
        result = cls._resolved_to_result(
            url=url,
            target_variation="",
            resolved=resolved,
        )
        return cls._result_to_public_tuple(
            result,
            target_variation="Exact linked item",
            logs=[(safe_message, "error", url)],
        )


_EXACT_SELECTED_OFFER_SCRIPT = r"""
const requestedItemId = String(arguments[0] || '');

const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
const lower = value => normalize(value).toLowerCase();
const visible = element => {
  if (!element) return false;
  const style = getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.display !== 'none'
    && style.visibility !== 'hidden'
    && Number(style.opacity || 1) > 0
    && rect.width > 0
    && rect.height > 0;
};
const pageY = element => {
  const rect = element.getBoundingClientRect();
  return rect.top + window.scrollY;
};
const directText = element => normalize(
  [...element.childNodes]
    .filter(node => node.nodeType === Node.TEXT_NODE)
    .map(node => node.textContent)
    .join(' ')
);
const elementToken = element => normalize([
  element?.id || '',
  element?.className || '',
  element?.getAttribute?.('data-testid') || '',
  element?.getAttribute?.('data-automation-id') || '',
  element?.getAttribute?.('aria-label') || '',
].join(' ')).toLowerCase();
const recommendationPattern = /recommend|sponsor|carousel|similar|related|also.viewed|popular.picks|you.may.also.like|frequently.bought|customers.also/;
const alternateConditionPattern = /\b(open box|used|refurbished|pre-owned|renewed)\b/i;
const unselectedVariantPattern = /variant|option|swatch|choice|selector|tile/;

const itemIdsNear = element => {
  const ids = new Set();
  let current = element;
  for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
    for (const attr of ['data-item-id', 'data-us-item-id', 'data-product-id', 'data-tl-id']) {
      const value = normalize(current.getAttribute?.(attr));
      if (/^\d{5,}$/.test(value)) ids.add(value);
    }
    const href = normalize(current.getAttribute?.('href'));
    const match = href.match(/\/(\d{5,})(?:[/?#]|$)/);
    if (match) ids.add(match[1]);
    // Never scan every descendant link of a broad ancestor. Walmart places
    // sibling variants and recommendations under shared containers.
    for (const link of current.querySelectorAll?.(':scope > a[href*="/ip/"]') || []) {
      const linkedMatch = normalize(link.getAttribute('href'))
        .match(/\/(\d{5,})(?:[/?#]|$)/);
      if (linkedMatch) ids.add(linkedMatch[1]);
    }
    if (ids.size) break;
  }
  return [...ids];
};

const hasExcludedAncestor = element => {
  let current = element;
  for (let depth = 0; current && depth < 10; depth += 1, current = current.parentElement) {
    if (recommendationPattern.test(elementToken(current))) return true;
    const role = lower(current.getAttribute?.('role'));
    if (role === 'dialog' && !/buy box|purchase|product/.test(elementToken(current))) return true;
  }
  return false;
};

const insideUnselectedVariant = element => {
  let current = element;
  for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
    if (!unselectedVariantPattern.test(elementToken(current))) continue;
    const selected = current.getAttribute?.('aria-selected');
    const checked = current.getAttribute?.('aria-checked');
    const dataSelected = current.getAttribute?.('data-selected');
    if (selected === 'false' || checked === 'false' || dataSelected === 'false') {
      return true;
    }
  }
  return false;
};

const productName = value => lower(value)
  .replace(/\badd to cart\b|\bbuy now\b|\bwith add-on services\b/g, ' ')
  .replace(/[^a-z0-9]+/g, ' ')
  .replace(/\s+/g, ' ')
  .trim();

const exactTitleAssociation = (element, providedLabel = '') => {
  const controlName = productName(
    providedLabel
    || element?.getAttribute?.('aria-label')
    || element?.getAttribute?.('title')
    || element?.textContent
  );
  const pageName = productName(title);
  if (!controlName || !pageName || Math.min(controlName.length, pageName.length) < 20) {
    return false;
  }
  return controlName.includes(pageName) || pageName.includes(controlName);
};

const itemAssociation = (element, providedLabel = '') => {
  const ids = itemIdsNear(element);
  if (ids.length) {
    if (ids.length === 1 && ids[0] === requestedItemId) {
      return { strength: 3, ids, source: 'exact_local_item_id' };
    }
    return { strength: 0, ids, source: 'conflicting_local_item_id' };
  }
  if (exactTitleAssociation(element, providedLabel)) {
    return { strength: 2, ids, source: 'exact_product_title' };
  }
  if (/^(add to cart|add to basket|buy now)$/i.test(normalize(providedLabel))) {
    return { strength: 1, ids, source: 'generic_primary_control' };
  }
  return { strength: 0, ids, source: 'unbound' };
};
const belongsToRequestedItem = element => itemAssociation(
  element,
  normalize(
    element?.getAttribute?.('aria-label')
    || element?.getAttribute?.('title')
    || element?.textContent
  )
).strength > 0;

const insideAlternateCondition = element => {
  let current = element;
  for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
    const text = normalize(current.textContent);
    if (text.length > 0 && text.length <= 900 && alternateConditionPattern.test(text)) {
      const selected = current.getAttribute?.('aria-selected') === 'true'
        || current.getAttribute?.('aria-checked') === 'true'
        || current.getAttribute?.('data-selected') === 'true';
      if (!selected) return true;
    }
  }
  return false;
};

const main = document.querySelector('main')
  || document.querySelector('[role="main"]')
  || document.body;
const h1 = [...main.querySelectorAll('h1')].find(visible) || null;
const title = normalize(h1?.textContent || document.title);
const startY = h1 ? pageY(h1) - 120 : Math.max(0, window.scrollY);

const boundaryHeadings = [...main.querySelectorAll('h2, h3')]
  .filter(visible)
  .map(element => ({ text: lower(element.textContent), y: pageY(element) }))
  .filter(item => item.y > startY + 350)
  .filter(item => /about this item|similar items|customers also|frequently bought|sponsored|more details|product details/.test(item.text))
  .sort((a, b) => a.y - b.y);
const endY = boundaryHeadings[0]?.y || startY + 3200;
const inPrimaryBand = element => {
  if (!visible(element) || hasExcludedAncestor(element)) return false;
  const y = pageY(element);
  return y >= startY && y <= endY;
};

const canonical = normalize(document.querySelector('link[rel="canonical"]')?.href);
const pageUrl = String(location.href || '');
const idSource = canonical || pageUrl;
const idMatches = [...idSource.matchAll(/\/(\d{5,})(?=[/?#]|$)/g)];
const pageItemId = idMatches.length ? idMatches[idMatches.length - 1][1] : '';

const bodyText = normalize(document.body?.innerText || '');
const captcha = /verify you are human|robot or human|press and hold|access denied|captcha|unusual traffic/i.test(bodyText.slice(0, 25000));

const controls = [...main.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')]
  .filter(inPrimaryBand)
  .filter(belongsToRequestedItem)
  .filter(element => !insideAlternateCondition(element))
  .map(element => {
    const label = normalize(
      element.getAttribute('aria-label')
      || element.getAttribute('title')
      || element.value
      || element.textContent
    );
    const token = lower(
      elementToken(element)
      + ' '
      + normalize(element.getAttribute('itemprop'))
    );
    const enabled = !element.disabled && element.getAttribute('aria-disabled') !== 'true';
    const association = itemAssociation(element, label);
    const y = pageY(element);
    let score = 0;
    if (/^(add to cart|add to basket|buy now)$/i.test(label)) score += 100;
    if (/add[-_ ]?to[-_ ]?cart|addtocart|buy[-_ ]?now/.test(token)) score += 80;
    if (association.strength >= 2) score += 140;
    if (association.strength === 1) score += 20;
    if (enabled) score += 20;
    score -= Math.min(120, Math.abs(y - (startY + 900)) / 20);
    return {
      element, label, token, enabled, y, score,
      associationStrength: association.strength,
      associationSource: association.source,
      associatedItemIds: association.ids,
    };
  });

const purchaseControls = controls
  .filter(item => /^(add to cart|add to basket|buy now)$/i.test(item.label)
    || /add[-_ ]?to[-_ ]?cart|addtocart|buy[-_ ]?now/.test(item.token))
  .filter(item => item.associationStrength > 0)
  .filter(item => item.y <= startY + 2100)
  .sort((a, b) => b.score - a.score);
const enabledPurchaseControl = purchaseControls.find(item => item.enabled) || null;
const disabledPurchaseControl = purchaseControls.find(item => !item.enabled) || null;
const chosenPurchaseControl = enabledPurchaseControl || disabledPurchaseControl || null;
const purchaseControlExact = Boolean(
  chosenPurchaseControl
  && chosenPurchaseControl.associationStrength >= 2
);
const purchaseY = chosenPurchaseControl?.y || startY + 1250;

const purchaseScope = (() => {
  if (!chosenPurchaseControl?.element) return null;
  let current = chosenPurchaseControl.element.parentElement;
  let fallback = current;
  for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
    if (!visible(current) || hasExcludedAncestor(current)) continue;
    const text = normalize(current.textContent);
    if (!text || text.length > 9000) continue;
    fallback = current;
    const token = elementToken(current);
    const hasPrice = /\$\s*\d/.test(text)
      || current.querySelector?.(
        '[data-testid*="price" i], [data-automation-id*="price" i], [itemprop="price"]'
      );
    const hasPurchaseContext = /buy.?box|purchase|fulfillment|add.?to.?cart|shipping|pickup|delivery/.test(
      lower(token + ' ' + text.slice(0, 1200))
    );
    if (hasPrice && hasPurchaseContext) return current;
  }
  return fallback;
})();
const inPurchaseScope = element => {
  if (!element || !visible(element) || hasExcludedAncestor(element)) return false;
  if (insideAlternateCondition(element) || insideUnselectedVariant(element)) return false;
  const y = pageY(element);
  if (!chosenPurchaseControl) {
    return y >= startY - 150 && y <= startY + 1800;
  }
  if (
    purchaseScope?.contains(element)
    && Math.abs(y - purchaseY) <= 700
  ) return true;
  return Math.abs(y - purchaseY) <= 420;
};
const purchaseScopeVerified = Boolean(
  purchaseControlExact
  && purchaseScope
  && !hasExcludedAncestor(purchaseScope)
);

const priceElements = [...main.querySelectorAll(
  '[itemprop="price"], [data-testid*="price" i], [data-automation-id*="price" i], [aria-label*="$"]'
)]
  .filter(inPrimaryBand)
  .filter(inPurchaseScope)
  .map(element => {
    const text = normalize(
      element.getAttribute('content')
      || element.getAttribute('aria-label')
      || element.textContent
    );
    const token = lower(
      elementToken(element)
      + ' '
      + normalize(element.getAttribute('itemprop'))
    );
    const context = normalize(element.parentElement?.textContent || '').slice(0, 700);
    const directCombined = lower(text + ' ' + token);
    const combined = lower(directCombined + ' ' + context);
    const y = pageY(element);
    const hasCurrentPriceCue = /current price|price-current|price-characteristic|product-price|price when purchased online|\bnow\s*\$/.test(combined);
    const rejected = (
      !hasCurrentPriceCue
      && /was price|list price|\bwas\s*\$|unit price|strike|comparison|savings|save \$|discount/.test(directCombined)
    ) || /per month|monthly|affirm|klarna|afterpay|protection plan/.test(directCombined);
    const highConfidence = !rejected && (
      hasCurrentPriceCue
      || (
        /^\$\s*\d[\d,]*(?:\.\d{1,2})?$/.test(text)
        && purchaseScopeVerified
      )
    );
    let score = 0;
    if (highConfidence) score += 120;
    if (/current price|product price|price-current|price-characteristic/.test(combined)) score += 80;
    if (rejected) score -= 300;
    if (/\$\s*\d/.test(text)) score += 40;
    if (y <= purchaseY + 250) score += 20;
    score -= Math.min(60, Math.abs(purchaseY - y) / 40);
    return { text, token, context, y, score, highConfidence, rejected };
  })
  .filter(item => /\$\s*\d/.test(item.text) || /^\d+(?:\.\d{1,2})?$/.test(item.text))
  .filter(item => !item.rejected)
  .sort((a, b) => b.score - a.score);
const highConfidencePrices = priceElements.filter(item => item.highConfidence);
const normalizedPriceValues = highConfidencePrices
  .map(item => {
    const match = item.text.replace(/,/g, '').match(/\$\s*(\d+(?:\.\d{1,2})?)/);
    return match ? Number(match[1]).toFixed(2) : '';
  })
  .filter(Boolean);
const priceConflict = new Set(normalizedPriceValues).size > 1;
const priceText = priceConflict ? '' : (highConfidencePrices[0]?.text || '');
const priceExact = Boolean(
  priceText
  && purchaseControlExact
  && highConfidencePrices[0]?.highConfidence
);

const atomic = [...main.querySelectorAll('span, p, div, li, button, label')]
  .filter(inPrimaryBand)
  .map(element => ({
    element,
    text: directText(element) || (element.children.length === 0 ? normalize(element.textContent) : ''),
    y: pageY(element),
  }))
  .filter(item => item.text && item.text.length <= 220);

const lowPattern = /low stock|limited stock|few left|only\s+\d+\s+(?:left|remaining|in stock)|\d+\s+left in stock/i;
const lowStockTexts = atomic
  .filter(item => lowPattern.test(item.text))
  .filter(item => !insideAlternateCondition(item.element))
  .filter(item => inPurchaseScope(item.element))
  .filter(item => Math.abs(item.y - purchaseY) <= 850)
  .sort((a, b) => Math.abs(a.y - purchaseY) - Math.abs(b.y - purchaseY))
  .map(item => item.text)
  .filter((value, index, array) => array.indexOf(value) === index)
  .slice(0, 8);
const lowStockExact = Boolean(
  purchaseControlExact
  && lowStockTexts.length
);

const selectedElements = [...main.querySelectorAll(
  '[aria-checked="true"], [aria-selected="true"], [data-selected="true"], [data-state="selected"]'
)]
  .filter(inPrimaryBand)
  .filter(element => {
    const label = normalize(
      element.getAttribute('aria-label')
      || element.textContent
    );
    return itemAssociation(element, label).strength >= 2
      || inPurchaseScope(element);
  });
const selectedOptionTexts = selectedElements
  .map(element => normalize(element.getAttribute('aria-label') || element.textContent))
  .filter(Boolean)
  .slice(0, 20);
const selectedOptionOos = selectedOptionTexts.some(text => /out of stock|sold out|unavailable/i.test(text));
const selectedOptionExact = Boolean(
  selectedOptionOos
  && selectedElements.some(element => {
    const label = normalize(
      element.getAttribute('aria-label')
      || element.textContent
    );
    return itemAssociation(element, label).strength >= 2
      || inPurchaseScope(element);
  })
);

const oosPattern = /^(?:out of stock|sold out|currently unavailable|item unavailable|this item is unavailable|no longer available|not available)$/i;
const exactOosRegion = [...main.querySelectorAll(
  '[aria-label*="out of stock" i], [data-testid*="out-of-stock" i], [data-automation-id*="out-of-stock" i]'
)]
  .filter(inPrimaryBand)
  .filter(element => !hasExcludedAncestor(element))
  .some(element => /out of stock details|sorry,? this item is out of stock/i.test(
    normalize(
      element.getAttribute('aria-label')
      || element.textContent
    )
  ));
const productOosTexts = atomic
  .filter(item => oosPattern.test(item.text))
  .filter(item => !insideAlternateCondition(item.element))
  .filter(item => inPurchaseScope(item.element))
  .filter(item => {
    const ancestorText = lower(item.element.parentElement?.textContent || '');
    return !/shipping|pickup|delivery/.test(ancestorText.slice(0, 260));
  })
  .sort((a, b) => Math.abs(a.y - purchaseY) - Math.abs(b.y - purchaseY))
  .map(item => item.text)
  .filter((value, index, array) => array.indexOf(value) === index)
  .slice(0, 8);
const productOosExact = Boolean(
  exactOosRegion
  || productOosTexts.length
  && (
    purchaseScopeVerified
    || productOosTexts.some(text => /this item|item unavailable|no longer available/i.test(text))
  )
);

const fulfillment = {};
for (const method of ['shipping', 'pickup', 'delivery']) {
  const labels = [...main.querySelectorAll('span, p, div, h3, h4, button')]
    .filter(inPrimaryBand)
    .filter(inPurchaseScope)
    .filter(element => lower(directText(element) || element.textContent) === method);
  let best = null;
  for (const label of labels) {
    let current = label.parentElement;
    for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
      if (!visible(current) || hasExcludedAncestor(current)) continue;
      const text = normalize(current.textContent);
      if (!text || text.length > 650) continue;
      const otherCount = ['shipping', 'pickup', 'delivery']
        .filter(other => other !== method)
        .filter(other => new RegExp('\\b' + other + '\\b', 'i').test(text)).length;
      const candidate = { text, otherCount, depth };
      if (!best
        || candidate.otherCount < best.otherCount
        || (candidate.otherCount === best.otherCount && candidate.depth < best.depth)
        || (candidate.otherCount === best.otherCount && candidate.depth === best.depth && candidate.text.length < best.text.length)) {
        best = candidate;
      }
    }
  }
  if (best) fulfillment[method] = best.text;
}

const fulfillmentValues = Object.values(fulfillment).map(lower);
const availableMethodCount = fulfillmentValues.filter(text =>
  /arrives|available|shipping today|delivery today|pickup today|ready today|in stock/.test(text)
  && !/not available|unavailable|out of stock/.test(text)
).length;
const unavailableMethodCount = fulfillmentValues.filter(text =>
  /not available|unavailable|out of stock|not offered|no pickup/.test(text)
).length;
const allFulfillmentUnavailable = fulfillmentValues.length >= 2
  && unavailableMethodCount === fulfillmentValues.length
  && availableMethodCount === 0;

const productOos = Boolean(
  exactOosRegion
  || (productOosTexts.length > 0 && productOosExact)
);
const enabledPurchase = Boolean(enabledPurchaseControl && purchaseControlExact);

const normalizeAvailability = value => normalize(value).replace(/^https?:\/\/schema\.org\//i, '');
const jsonLdExactCandidates = [];
const visitJson = (value, depth = 0) => {
  if (!value || depth > 12) return;
  if (Array.isArray(value)) {
    for (const child of value.slice(0, 100)) visitJson(child, depth + 1);
    return;
  }
  if (typeof value !== 'object') return;

  const ids = [value.sku, value.productID, value.productId, value.itemID, value.itemId, value.usItemId]
    .filter(item => item !== null && item !== undefined)
    .map(String);
  const objectUrl = normalize(value.url || value['@id']);
  const exact = ids.includes(requestedItemId)
    || objectUrl.includes('/' + requestedItemId)
    || objectUrl.endsWith(requestedItemId);
  const typeText = normalize(value['@type']).toLowerCase();

  if (exact) {
    // Read only the exact object's immediate offers. Do not carry the exact
    // product identity into nested variant/recommendation objects.
    const offers = Array.isArray(value.offers) ? value.offers : (value.offers ? [value.offers] : []);
    if (typeText.includes('offer')) offers.push(value);
    if (!offers.length && value.availability) offers.push(value);
    for (const offer of offers) {
      if (!offer || typeof offer !== 'object') continue;
      jsonLdExactCandidates.push({
        availability: normalizeAvailability(offer.availability),
        price: normalize(offer.price || offer.lowPrice || offer.highPrice),
        seller: normalize(offer.seller?.name || offer.seller),
        sourceType: normalize(value['@type']),
      });
    }
  }

  for (const child of Object.values(value).slice(0, 200)) {
    visitJson(child, depth + 1);
  }
};
for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
  const text = script.textContent || '';
  if (!text || text.length > 1500000) continue;
  try { visitJson(JSON.parse(text)); } catch {}
}
const jsonLdSignatures = jsonLdExactCandidates
  .map(item => `${lower(item.availability)}|${normalize(item.price)}`)
  .filter(Boolean);
const jsonLdConflict = new Set(jsonLdSignatures).size > 1;
const jsonLdExact = jsonLdConflict
  ? {}
  : (jsonLdExactCandidates.find(item => item.availability || item.price) || {});

const identityIds = new Set();
if (pageItemId) identityIds.add(pageItemId);
for (const selector of [
  'meta[itemprop="sku"]',
  'meta[itemprop="productID"]',
]) {
  for (const element of [...document.querySelectorAll(selector)].slice(0, 10)) {
    const value = normalize(element.getAttribute('content') || element.textContent);
    if (/^\d{5,}$/.test(value)) identityIds.add(value);
  }
}
const identityConflict = pageItemId !== requestedItemId
  || ([...identityIds].length > 0 && !identityIds.has(requestedItemId));

const locationCandidates = [...document.querySelectorAll('header button, header [role="button"], header a')]
  .filter(visible)
  .map(element => normalize(element.getAttribute('aria-label') || element.textContent))
  .filter(text => /\b\d{5}\b/.test(text) || /location|delivery to|shipping to/i.test(text));
const locationText = locationCandidates[0] || '';

return {
  pageUrl,
  canonical,
  pageItemId,
  title,
  captcha,
  enabledPurchase,
  purchaseControlExact,
  identityIds: [...identityIds],
  identityConflict,
  purchaseControl: chosenPurchaseControl ? {
    label: chosenPurchaseControl.label,
    token: chosenPurchaseControl.token,
    enabled: chosenPurchaseControl.enabled,
    y: chosenPurchaseControl.y,
    associatedItemIds: chosenPurchaseControl.associatedItemIds,
    associationStrength: chosenPurchaseControl.associationStrength,
    associationSource: chosenPurchaseControl.associationSource,
    score: chosenPurchaseControl.score,
  } : {},
  priceText,
  priceExact,
  priceConflict,
  priceCandidates: priceElements.slice(0, 6).map(item => ({
    text: item.text,
    token: item.token,
    context: item.context,
    y: item.y,
    score: item.score,
    highConfidence: item.highConfidence,
  })),
  lowStockTexts,
  lowStockExact,
  selectedOptionTexts,
  selectedOptionOos,
  selectedOptionExact,
  productOos,
  productOosExact,
  productOosTexts,
  fulfillment,
  allFulfillmentUnavailable,
  jsonLdExact,
  jsonLdConflict,
  jsonLdExactCandidates: jsonLdExactCandidates.slice(0, 8),
  locationText,
};
"""
