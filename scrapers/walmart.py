"""Safe Walmart scraper with exact-item and primary-buy-box validation."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from core.base_scraper import BaseScraper


UNKNOWN = "UNKNOWN"
IN_STOCK = "In Stock"
OOS = "OOS"


class WalmartScraper(BaseScraper):
    def __init__(
        self,
        browser_manager=None,
        logger_func=None,
        render_timeout=12,
        post_ready_delay=1.0,
        log_prefix="WAL",
        **kwargs,
    ):
        super().__init__(
            browser_manager,
            logger_func,
        )

        self.render_timeout = max(
            3,
            int(render_timeout),
        )

        self.post_ready_delay = max(
            0.0,
            float(post_ready_delay),
        )

        self.log_prefix = log_prefix

    def scrape(
        self,
        url: str,
        target_variation: Optional[str] = None,
    ):
        logs = []
        title = "Unknown"
        item_id = self._item_id(url)

        if not self.browser_manager:
            return self._error(
                url,
                target_variation,
                title,
                "No browser manager.",
                logs,
            )

        if not item_id:
            return self._error(
                url,
                target_variation,
                title,
                "No Walmart item ID in URL.",
                logs,
            )

        try:
            driver = self.browser_manager.get_driver()

            driver.set_page_load_timeout(
                40
            )

            driver.get(url)

        except TimeoutException:
            self._log(
                logs,
                url,
                (
                    "Page-load timeout; "
                    "parsing available DOM."
                ),
                "warning",
            )

        except Exception as exc:
            return self._error(
                url,
                target_variation,
                title,
                (
                    "Navigation failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                logs,
            )

        try:
            WebDriverWait(
                driver,
                self.render_timeout,
            ).until(
                lambda current_driver: (
                    current_driver.execute_script(
                        "return document.readyState"
                    )
                    in {
                        "interactive",
                        "complete",
                    }
                )
            )

        except (
            TimeoutException,
            WebDriverException,
        ):
            pass

        if self.post_ready_delay:
            time.sleep(
                self.post_ready_delay
            )

        try:
            html = driver.page_source or ""
            final_url = driver.current_url or url

        except WebDriverException as exc:
            return self._error(
                url,
                target_variation,
                title,
                (
                    "Could not read page: "
                    f"{type(exc).__name__}: {exc}"
                ),
                logs,
            )

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        title = self._title(soup)

        visible_text = soup.get_text(
            " ",
            strip=True,
        )

        blocked = self._blocked_reason(
            visible_text,
            title,
            final_url,
        )

        if blocked:
            return self._error(
                url,
                target_variation,
                title,
                blocked,
                logs,
                stock="Captcha/Blocked",
            )

        if self._missing_page(
            visible_text,
            title,
        ):
            return self._success(
                url,
                target_variation,
                title,
                "",
                OOS,
                (
                    "explicit missing/"
                    "no-longer-available page"
                ),
                logs,
            )

        json_result = self._json_result(
            soup,
            item_id,
        )

        visual_result = (
            self._visual_result(
                soup
            )
        )

        self._log(
            logs,
            url,
            (
                f"JSON={json_result['stock']}/"
                f"{json_result['price'] or '[blank]'} "
                f"({json_result['reason']})"
            ),
            "info",
        )

        self._log(
            logs,
            url,
            (
                f"VISUAL={visual_result['stock']}/"
                f"{visual_result['price'] or '[blank]'} "
                f"({visual_result['reason']})"
            ),
            "info",
        )

        if (
            json_result["conflict"]
            or visual_result["conflict"]
        ):
            return self._error(
                url,
                target_variation,
                title,
                (
                    "A source contained contradictory "
                    "stock evidence."
                ),
                logs,
            )

        json_stock = json_result["stock"]
        visual_stock = visual_result["stock"]

        # Exact-item JSON and primary buy box
        # must never contradict each other.
        if (
            json_stock != UNKNOWN
            and visual_stock != UNKNOWN
            and json_stock != visual_stock
        ):
            return self._error(
                url,
                target_variation,
                title,
                (
                    "JSON/primary-buy-box conflict: "
                    f"{json_stock} vs {visual_stock}."
                ),
                logs,
            )

        # Both sources say In Stock, but prices
        # must also agree when both provide one.
        if (
            json_stock == IN_STOCK
            and visual_stock == IN_STOCK
            and json_result["price"]
            and visual_result["price"]
            and (
                json_result["price"]
                != visual_result["price"]
            )
        ):
            return self._error(
                url,
                target_variation,
                title,
                (
                    "Price conflict: "
                    f"{json_result['price']} vs "
                    f"{visual_result['price']}."
                ),
                logs,
            )

        if json_stock != UNKNOWN:
            result = dict(
                json_result
            )

            if (
                result["stock"] == IN_STOCK
                and not result["price"]
            ):
                result["price"] = (
                    visual_result["price"]
                )

            result["text"] = (
                f"{result.get('text', '')} "
                f"{visual_result.get('text', '')}"
            ).strip()

        elif visual_stock != UNKNOWN:
            result = dict(
                visual_result
            )

        else:
            return self._error(
                url,
                target_variation,
                title,
                (
                    "No decisive exact-item or "
                    "primary-buy-box evidence."
                ),
                logs,
            )

        if result["stock"] == IN_STOCK:
            inventory_detail = (
                self._inventory_detail(
                    result.get(
                        "text",
                        "",
                    ),
                    result.get(
                        "quantity"
                    ),
                )
            )

            # User business rule:
            # Low Stock, Limited Stock and
            # Only N remaining are all OOS.
            if inventory_detail != IN_STOCK:
                return self._success(
                    url,
                    target_variation,
                    title,
                    "",
                    OOS,
                    (
                        f"{inventory_detail}; "
                        "business rule treats "
                        "this as OOS"
                    ),
                    logs,
                )

            if not result["price"]:
                return self._error(
                    url,
                    target_variation,
                    title,
                    (
                        "In Stock found, but no "
                        "trustworthy primary price."
                    ),
                    logs,
                )

        return self._success(
            url,
            target_variation,
            title,
            result["price"],
            result["stock"],
            result["reason"],
            logs,
        )

    def _json_result(
        self,
        soup,
        item_id,
    ):
        payloads = []

        next_script = soup.find(
            "script",
            id="__NEXT_DATA__",
        )

        if next_script:
            data = self._decode(
                next_script.string
                or next_script.get_text()
            )

            if data:
                payloads.append(
                    (
                        "__NEXT_DATA__",
                        data,
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

            raw = (
                script.string
                or script.get_text()
                or ""
            )

            if (
                script_id
                == "Items-REDUX_STATE"
                or "REDUX_STATE"
                in raw[:1000]
            ):
                data = self._decode(
                    raw
                )

                if data:
                    payloads.append(
                        (
                            "REDUX_STATE",
                            data,
                        )
                    )

        for source, data in payloads:
            item = self._find_item(
                data,
                item_id,
            )

            if item:
                stock, conflict = (
                    self._item_stock(
                        item
                    )
                )

                return {
                    "stock": stock,
                    "conflict": conflict,
                    "price": self._item_price(
                        item
                    ),
                    "quantity": (
                        self._item_quantity(
                            item
                        )
                    ),
                    "text": json.dumps(
                        item,
                        ensure_ascii=False,
                    )[:20000],
                    "reason": (
                        "exact target item in "
                        f"{source}"
                    ),
                }

        return self._empty(
            (
                "JSON found, exact "
                "target item missing"
            )
            if payloads
            else "no usable Walmart JSON"
        )

    def _visual_result(
        self,
        soup,
    ):
        buy_box = self._primary_buy_box(
            soup
        )

        if buy_box is None:
            return self._empty(
                (
                    "primary product "
                    "buy box not found"
                )
            )

        text = buy_box.get_text(
            " ",
            strip=True,
        )

        lower_text = text.lower()

        explicit_oos = any(
            phrase in lower_text
            for phrase in (
                "out of stock",
                "sold out",
                "currently unavailable",
                "not available",
                "this item is unavailable",
                "no longer available",
            )
        )

        enabled_add_to_cart = False
        disabled_add_to_cart = False

        for button in buy_box.find_all(
            "button"
        ):
            button_text = (
                button.get_text(
                    " ",
                    strip=True,
                )
                + " "
                + str(
                    button.get(
                        "aria-label",
                        "",
                    )
                )
            ).lower()

            if (
                "add to cart"
                not in button_text
            ):
                continue

            is_disabled = (
                button.has_attr(
                    "disabled"
                )
                or str(
                    button.get(
                        "aria-disabled",
                        "",
                    )
                ).lower()
                == "true"
            )

            disabled_add_to_cart = (
                disabled_add_to_cart
                or is_disabled
            )

            enabled_add_to_cart = (
                enabled_add_to_cart
                or not is_disabled
            )

        price = self._visual_price(
            buy_box
        )

        if (
            enabled_add_to_cart
            and explicit_oos
        ):
            result = self._empty(
                (
                    "primary buy box "
                    "internally conflicts"
                )
            )

            result.update(
                conflict=True,
                price=price,
                text=text,
            )

            return result

        if enabled_add_to_cart:
            return self._make_result(
                IN_STOCK,
                price,
                None,
                text,
                (
                    "enabled Add to cart "
                    "inside primary buy box"
                ),
            )

        if explicit_oos:
            return self._make_result(
                OOS,
                "",
                None,
                text,
                (
                    "primary buy box "
                    "explicitly unavailable"
                ),
            )

        if disabled_add_to_cart:
            return self._make_result(
                OOS,
                "",
                None,
                text,
                (
                    "primary Add to cart "
                    "button disabled"
                ),
            )

        result = self._empty(
            (
                "primary product "
                "buy box inconclusive"
            )
        )

        result.update(
            price=price,
            text=text,
        )

        return result

    def _primary_buy_box(
        self,
        soup,
    ):
        selectors = (
            '[data-testid="buy-box"]',
            '[data-automation-id="buy-box"]',
            'section[data-testid*="buy-box"]',
            'div[data-testid*="buy-box"]',
            'section[class*="buybox"]',
            'div[class*="buybox"]',
        )

        rejected_tokens = (
            "carousel",
            "recommend",
            "related",
            "sponsor",
            "search-result",
            "similar-item",
        )

        for selector in selectors:
            for node in soup.select(
                selector
            ):
                if not isinstance(
                    node,
                    Tag,
                ):
                    continue

                ancestry_parts = []
                current = node

                for _ in range(7):
                    if current is None:
                        break

                    ancestry_parts.extend(
                        (
                            str(
                                current.get(
                                    "id",
                                    "",
                                )
                            ),
                            " ".join(
                                current.get(
                                    "class",
                                    [],
                                )
                            ),
                            str(
                                current.get(
                                    "data-testid",
                                    "",
                                )
                            ),
                            str(
                                current.get(
                                    "data-automation-id",
                                    "",
                                )
                            ),
                        )
                    )

                    current = (
                        current.parent
                        if isinstance(
                            current.parent,
                            Tag,
                        )
                        else None
                    )

                ancestry_text = " ".join(
                    ancestry_parts
                ).lower()

                if any(
                    token in ancestry_text
                    for token
                    in rejected_tokens
                ):
                    continue

                node_text = node.get_text(
                    " ",
                    strip=True,
                ).lower()

                if any(
                    token in node_text
                    for token in (
                        "$",
                        "add to cart",
                        "out of stock",
                        "sold out",
                        "unavailable",
                    )
                ):
                    return node

        return None

    def _find_item(
        self,
        data,
        item_id,
    ):
        current = data

        for key in (
            "props",
            "pageProps",
            "initialData",
            "data",
            "product",
        ):
            current = (
                current.get(key)
                if isinstance(
                    current,
                    dict,
                )
                else None
            )

        if (
            isinstance(
                current,
                dict,
            )
            and self._matches(
                current,
                item_id,
            )
        ):
            return current

        return self._search(
            data,
            item_id,
        )

    def _search(
        self,
        value,
        item_id,
    ):
        if isinstance(
            value,
            dict,
        ):
            if (
                self._matches(
                    value,
                    item_id,
                )
                and self._product_like(
                    value
                )
            ):
                return value

            skipped_tokens = (
                "carousel",
                "related",
                "recommended",
                "sponsored",
                "review",
                "searchresult",
                "similar",
            )

            for key, child in value.items():
                if any(
                    token
                    in str(key).lower()
                    for token
                    in skipped_tokens
                ):
                    continue

                found = self._search(
                    child,
                    item_id,
                )

                if found:
                    return found

        elif isinstance(
            value,
            list,
        ):
            for child in value:
                found = self._search(
                    child,
                    item_id,
                )

                if found:
                    return found

        return None

    @staticmethod
    def _matches(
        item,
        item_id,
    ):
        return any(
            value is not None
            and str(value).strip()
            == item_id
            for value in (
                item.get(
                    "usItemId"
                ),
                item.get(
                    "itemId"
                ),
                item.get(
                    "id"
                ),
            )
        )

    @staticmethod
    def _product_like(
        item,
    ):
        return bool(
            {
                "availabilityStatus",
                "availability",
                "priceInfo",
                "buyBox",
                "fulfillmentOptions",
                "name",
            }.intersection(
                item
            )
        )

    @staticmethod
    def _item_stock(
        item,
    ):
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
            item.get(
                "buyBox"
            )
            or {}
        )

        if not isinstance(
            buy_box,
            dict,
        ):
            buy_box = {}

        cta = (
            buy_box.get(
                "cta"
            )
            or item.get(
                "cta"
            )
            or {}
        )

        if isinstance(
            cta,
            dict,
        ):
            cta_text = str(
                cta.get(
                    "text"
                )
                or cta.get(
                    "buttonText"
                )
                or ""
            ).strip().lower()

        else:
            cta_text = str(
                cta
            ).strip().lower()

        explicit_in_stock = (
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
        )

        explicit_oos = (
            item.get(
                "isOutOfStock"
            )
            is True
            or buy_box.get(
                "isOutOfStock"
            )
            is True
            or availability
            in {
                "OUT_OF_STOCK",
                "UNAVAILABLE",
                "SOLD_OUT",
                "PREORDER",
            }
        )

        if (
            explicit_in_stock
            and explicit_oos
        ):
            return UNKNOWN, True

        if explicit_in_stock:
            return IN_STOCK, False

        if explicit_oos:
            return OOS, False

        return UNKNOWN, False

    def _item_price(
        self,
        item,
    ):
        candidates = []

        for container in (
            item.get(
                "priceInfo"
            ),
            item.get(
                "price"
            ),
        ):
            if isinstance(
                container,
                dict,
            ):
                current = (
                    container.get(
                        "currentPrice"
                    )
                    or {}
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
                    )
                )

            else:
                candidates.append(
                    container
                )

        for candidate in candidates:
            price = self._money(
                candidate
            )

            if price:
                return price

        return ""

    @staticmethod
    def _item_quantity(
        item,
    ):
        for key in (
            "availableQuantity",
            "quantity",
            "inventoryCount",
        ):
            value = item.get(
                key
            )

            if value is None:
                continue

            try:
                quantity = int(
                    float(value)
                )

                if quantity >= 0:
                    return quantity

            except (
                TypeError,
                ValueError,
            ):
                pass

        return None

    def _visual_price(
        self,
        buy_box,
    ):
        selectors = (
            '[data-automation-id="product-price"]',
            '[data-testid*="product-price"]',
            '[data-testid="price-wrap"]',
            '[itemprop="price"]',
            'meta[itemprop="price"]',
        )

        for selector in selectors:
            for node in buy_box.select(
                selector
            ):
                for raw in (
                    node.get(
                        "content"
                    ),
                    node.get(
                        "aria-label"
                    ),
                    node.get_text(
                        " ",
                        strip=True,
                    ),
                ):
                    price = self._money(
                        raw
                    )

                    if price:
                        return price

        return ""

    @staticmethod
    def _decode(
        raw,
    ):
        if not raw:
            return None

        try:
            data = json.loads(
                raw
            )

            return (
                data
                if isinstance(
                    data,
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

        start = raw.find(
            "{"
        )

        end = raw.rfind(
            "}"
        )

        if (
            start < 0
            or end <= start
        ):
            return None

        try:
            data = json.loads(
                raw[
                    start:
                    end + 1
                ]
            )

            return (
                data
                if isinstance(
                    data,
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
    def _money(
        value,
    ):
        if (
            value is None
            or isinstance(
                value,
                bool,
            )
        ):
            return ""

        if isinstance(
            value,
            (
                int,
                float,
            ),
        ):
            if value < 0:
                return ""

            return (
                f"${float(value):.2f}"
            )

        match = re.search(
            (
                r"(?:USD\s*)?"
                r"\$\s*"
                r"(\d{1,6}"
                r"(?:,\d{3})*"
                r"(?:\.\d{1,2})?)"
            ),
            str(value),
            re.I,
        )

        if not match:
            return ""

        try:
            amount = float(
                match.group(1).replace(
                    ",",
                    "",
                )
            )

            return f"${amount:.2f}"

        except ValueError:
            return ""

    @staticmethod
    def _item_id(
        url,
    ):
        clean_url = (
            url.split(
                "?",
                1,
            )[0]
            .split(
                "#",
                1,
            )[0]
        )

        ids = re.findall(
            r"/(\d+)(?=/|$)",
            clean_url,
        )

        return (
            ids[-1]
            if ids
            else None
        )

    @staticmethod
    def _title(
        soup,
    ):
        h1 = soup.find(
            "h1"
        )

        if (
            h1
            and h1.get_text(
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
                "og:title"
            },
        )

        if (
            meta
            and meta.get(
                "content"
            )
        ):
            return str(
                meta["content"]
            ).strip()

        if (
            soup.title
            and soup.title.string
        ):
            return soup.title.string.strip()

        return "Unknown"

    @staticmethod
    def _blocked_reason(
        visible_text,
        title,
        final_url,
    ):
        text = (
            f"{title} "
            f"{final_url} "
            f"{visible_text[:10000]}"
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
                    "Walmart blocked the page "
                    f"with {phrase!r}."
                )

        return ""

    @staticmethod
    def _missing_page(
        visible_text,
        title,
    ):
        text = (
            f"{title} "
            f"{visible_text[:20000]}"
        ).lower()

        return any(
            phrase in text
            for phrase in (
                "we couldn't find this page",
                "page not found",
                (
                    "this item is "
                    "no longer available"
                ),
                (
                    "this product is "
                    "no longer available"
                ),
            )
        )

    @staticmethod
    def _inventory_detail(
        text,
        quantity,
    ):
        if (
            quantity is not None
            and 0 < quantity <= 10
        ):
            return (
                f"Only {quantity} remaining"
            )

        lower_text = (
            text
            or ""
        ).lower()

        match = re.search(
            (
                r"\bonly\s+"
                r"(\d+)\s+"
                r"(?:left|remaining|in stock)"
                r"\b"
            ),
            lower_text,
        )

        if match:
            return (
                f"Only {match.group(1)} "
                "remaining"
            )

        if "low stock" in lower_text:
            return "Low Stock"

        if "limited stock" in lower_text:
            return "Limited Stock"

        return IN_STOCK

    @staticmethod
    def _empty(
        reason,
    ):
        return {
            "stock": UNKNOWN,
            "conflict": False,
            "price": "",
            "quantity": None,
            "text": "",
            "reason": reason,
        }

    @staticmethod
    def _make_result(
        stock,
        price,
        quantity,
        text,
        reason,
    ):
        return {
            "stock": stock,
            "conflict": False,
            "price": price,
            "quantity": quantity,
            "text": text,
            "reason": reason,
        }

    def _success(
        self,
        url,
        target_variation,
        title,
        price,
        stock,
        evidence,
        logs,
    ):
        if stock == OOS:
            price = ""

        self._log(
            logs,
            url,
            (
                "FINAL -> "
                f"Price: {price or '[blank]'} | "
                f"Stock: {stock} | "
                f"Evidence: {evidence}"
            ),
            (
                "oos"
                if stock == OOS
                else "price"
            ),
        )

        variants = [
            {
                "label": (
                    target_variation
                    or "Default"
                ),
                "price": price,
                "stock": stock,
            }
        ]

        return (
            "Success",
            price,
            stock,
            title,
            variants,
            logs,
        )

    def _error(
        self,
        url,
        target_variation,
        title,
        reason,
        logs,
        stock=UNKNOWN,
    ):
        self._log(
            logs,
            url,
            (
                "INCONCLUSIVE -> "
                f"{reason}"
            ),
            "error",
        )

        variants = [
            {
                "label": (
                    target_variation
                    or "Default"
                ),
                "price": "",
                "stock": stock,
            }
        ]

        return (
            "Error",
            "",
            stock,
            title,
            variants,
            logs,
        )

    def _log(
        self,
        logs,
        url,
        message,
        tag,
    ):
        full_message = (
            f"{self.log_prefix}: "
            f"{message}"
        )

        writer = getattr(
            self,
            "_write_log",
            None,
        )

        if callable(
            writer
        ):
            try:
                writer(
                    logs,
                    full_message,
                    tag,
                )
                return

            except Exception:
                pass

        logs.append(
            (
                full_message,
                tag,
                url,
            )
        )