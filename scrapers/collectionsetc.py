import os
import re
import time
import random
from bs4 import BeautifulSoup
from typing import Tuple, List, Dict, Optional, Any, Callable

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager

from core.base_scraper import BaseScraper
from config.settings import CURRENT_BRAVE_PATH
from scrapers.ce_price_parser import CEPriceParser


class CollectionsEtcScraper(BaseScraper):
    """
    Scraper for CollectionsEtc.com — a Shopify-powered retail store.
    Fully rewritten for stability and maintainability while keeping ALL original behavior.
    """

    SUPPORTED_DOMAINS = ("collectionsetc.com", "www.collectionsetc.com")

    def __init__(
        self,
        browser_manager: Any = None,
        logger_func: Optional[Callable] = None,
        **kwargs
    ):
        self.browser_manager = browser_manager
        self.log = logger_func
        self.parser = CEPriceParser(logger_func=self.log)

    def _init_standalone_driver(self) -> webdriver.Chrome:
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("window-size=1920,1080")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )

        if CURRENT_BRAVE_PATH and os.path.exists(CURRENT_BRAVE_PATH):
            options.binary_location = CURRENT_BRAVE_PATH

        try:
            os.environ["WDM_LOG"] = "0"
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
        except Exception:
            if self.log:
                self.log("WDM install failed, falling back to local ChromeDriver.", "warning")
            driver = webdriver.Chrome(options=options)

        driver.set_page_load_timeout(45)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"},
            )
        except Exception:
            pass

        return driver

    def _wait_for_page(self, driver: webdriver.Chrome, timeout: float = 15.0) -> None:
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "form[action*='/cart/add'], .product-form, h1, .product__title")
                )
            )
        except TimeoutException:
            pass
        time.sleep(random.uniform(1.5, 2.5))

    def _wait_for_variant_update(self, driver: webdriver.Chrome, timeout: float = 5.0) -> None:
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     ".price, .price__current, .price-item--sale, [data-product-price], span.money")
                )
            )
        except TimeoutException:
            pass
        time.sleep(1.0)

    @staticmethod
    def _clean_soup(soup_obj: BeautifulSoup) -> BeautifulSoup:
        for sel in [
            "footer", "header", "nav", ".footer", ".header",
            ".recommendations", ".related-products", ".cross-sells",
            ".recently-viewed", ".you-may-also-like",
            '[data-section-type="related-products"]',
            '[data-section-type="recently-viewed-products"]',
        ]:
            for el in soup_obj.select(sel):
                el.decompose()
        return soup_obj

    def _collect_html_variants_from_select(self, driver: webdriver.Chrome) -> List[Dict]:
        variants = []
        try:
            selects = driver.find_elements(By.CSS_SELECTOR, "select[name='id'], select.product-select")
            for sel_el in selects:
                if not sel_el.is_displayed():
                    continue
                try:
                    select_obj = Select(sel_el)
                    for option in select_obj.options:
                        val = option.get_attribute("value") or ""
                        raw_text = (
                            driver.execute_script(
                                "return arguments[0].innerText || arguments[0].textContent;",
                                option
                            ) or ""
                        ).strip()

                        if not val or any(x in raw_text.lower() for x in ["choose", "select", "---"]):
                            continue

                        is_oos = bool(re.search(
                            r"(?i)sold\s*out|out\s*of\s*stock|unavailable|backorder",
                            raw_text
                        ))

                        price_in_label = ""
                        pm = re.search(r"\$\s*([\d,]+\.\d{2})", raw_text)
                        if pm:
                            price_in_label = f"${float(pm.group(1).replace(',', '')):.2f}"

                        clean_label = re.sub(r"\s*[-–]\s*\$[\d,.]+", "", raw_text).strip()
                        clean_label = re.sub(
                            r"(?i)\s*(sold\s*out|out\s*of\s*stock|unavailable).*$",
                            "", clean_label
                        ).strip()

                        variants.append({
                            "id": val,
                            "title": clean_label,
                            "option1": clean_label,
                            "option2": None,
                            "option3": None,
                            "price": price_in_label,
                            "available": not is_oos,
                            "_source": "html_select",
                            "_raw_text": raw_text,
                        })

                except StaleElementReferenceException:
                    continue
                except Exception:
                    continue

        except Exception as e:
            if self.log:
                self.log(f"CE: HTML SELECT variant collection failed: {e}", "warning")

        return variants

    def _collect_html_variants_from_swatches(self, driver: webdriver.Chrome) -> List[Dict]:
        variants = []
        swatch_selectors = [
            ".swatch__element", ".color-swatch", ".size-swatch",
            "[data-value]", ".product-form__option",
            "fieldset label", ".variant-button", ".option-button"
        ]

        try:
            for sel in swatch_selectors:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                if not elements:
                    continue

                for el in elements:
                    if not el.is_displayed():
                        continue

                    try:
                        raw_text = (
                            driver.execute_script(
                                "return arguments[0].innerText || arguments[0].getAttribute('data-value') "
                                "|| arguments[0].getAttribute('aria-label') || '';",
                                el
                            ) or ""
                        ).strip()

                        if not raw_text or raw_text in ["+", "-"]:
                            continue

                        cls = (el.get_attribute("class") or "").lower()
                        aria_disabled = el.get_attribute("aria-disabled") == "true"
                        is_oos = (
                            "sold-out" in cls or "unavailable" in cls or
                            "disabled" in cls or aria_disabled or
                            bool(re.search(r"(?i)sold\s*out|out\s*of\s*stock|unavailable", raw_text))
                        )

                        if any(v["title"].lower() == raw_text.lower() for v in variants):
                            continue

                        variants.append({
                            "id": raw_text,
                            "title": raw_text,
                            "option1": raw_text,
                            "option2": None,
                            "option3": None,
                            "price": "",
                            "available": not is_oos,
                            "_source": "html_swatch",
                        })

                    except StaleElementReferenceException:
                        continue
                    except Exception:
                        continue

                if variants:
                    break

        except Exception as e:
            if self.log:
                self.log(f"CE: Swatch variant collection failed: {e}", "warning")

        return variants

    def _click_variant_in_dom(self, driver: webdriver.Chrome, variant: Dict, logs: list) -> bool:
        source = variant.get("_source", "")
        variant_id = variant.get("id", "")
        title = variant.get("title", "")

        try:
            if source == "html_select":
                select_els = driver.find_elements(
                    By.CSS_SELECTOR, "select[name='id'], select.product-select"
                )
                for sel_el in select_els:
                    if not sel_el.is_displayed():
                        continue
                    try:
                        Select(sel_el).select_by_value(variant_id)
                        driver.execute_script(
                            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                            sel_el
                        )
                        return True
                    except Exception:
                        continue

            elif source == "html_swatch":
                for sel in [
                    f'[data-value="{title}"]',
                    f'label[for*="{title.lower()}"]',
                    f'.swatch__element[data-value="{title}"]',
                ]:
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in els:
                        if el.is_displayed():
                            driver.execute_script("arguments[0].click();", el)
                            return True

                for el in driver.find_elements(By.CSS_SELECTOR, ".swatch__element, [data-value], .variant-button"):
                    try:
                        el_text = (
                            driver.execute_script(
                                "return arguments[0].innerText || arguments[0].getAttribute('data-value') || '';",
                                el
                            ) or ""
                        ).strip().lower()
                        if el_text == title.lower() and el.is_displayed():
                            driver.execute_script("arguments[0].click();", el)
                            return True
                    except StaleElementReferenceException:
                        continue

        except Exception as e:
            logs.append((f"CE: Variant DOM click failed for '{title}': {e}", "warning", None))

        return False

    def scrape(
        self,
        url: str,
        target_variation: Optional[str] = None
    ) -> Tuple[str, str, str, str, List[Dict[str, Any]], List[Tuple[str, str, Optional[str]]]]:

        logs: List[Tuple[str, str, Optional[str]]] = []
        driver = None
        is_standalone = False

        try:
            if self.browser_manager:
                driver = self.browser_manager.get_driver()
            else:
                driver = self._init_standalone_driver()
                is_standalone = True

            if not driver:
                logs.append(("Browser initialization failed", "error", None))
                return "Error", "", "OOS", "", [], logs

            driver.get(url)
            self._wait_for_page(driver)

            html = driver.page_source
            if any(kw in html.lower() for kw in ["captcha", "security check", "are you a human"]):
                logs.append(("Captcha detected on CollectionsEtc", "error", None))
                return "Error", "", "Captcha detected", "", [], logs

            soup = self._clean_soup(BeautifulSoup(html, "html.parser"))

            title_el = soup.select_one("h1, .product__title, .product-title")
            name = title_el.get_text(" ", strip=True) if title_el else "Unknown"

            visible_page_text = soup.get_text(" ", strip=True)

            is_hidden_price = self.parser.check_if_hidden_price(driver, soup)

            shopify_data = self.parser.fetch_shopify_json(driver, url)

            if shopify_data:
                try:
                    html = driver.page_source
                    soup = self._clean_soup(BeautifulSoup(html, "html.parser"))
                    visible_page_text = soup.get_text(" ", strip=True)
                except Exception:
                    pass

            if not shopify_data:
                shopify_data = self.parser.extract_embedded_shopify_data(soup)
                if shopify_data:
                    logs.append(("CE: Using embedded Shopify JSON from page scripts.", "info", None))

            html_variants: List[Dict] = []
            if not shopify_data:
                logs.append(("CE: Shopify JSON unavailable — falling back to HTML variant collection.", "warning", None))
                html_variants = self._collect_html_variants_from_select(driver)
                if not html_variants:
                    html_variants = self._collect_html_variants_from_swatches(driver)

            all_variants: List[Dict] = []
            if shopify_data:
                all_variants = shopify_data.get("variants", [])
            elif html_variants:
                all_variants = html_variants

            logs.append((
                f"CE START → URL: {url} | Target Variation: '{target_variation}' "
                f"| Hidden Price: {is_hidden_price} "
                f"| Option groups found: {len(all_variants)} "
                f"| Source: {'ShopifyJSON' if shopify_data else 'HTML' if html_variants else 'NONE'}",
                "info", None
            ))

            variants_out: List[Dict[str, Any]] = []
            chosen_variant: Optional[Dict] = None
            price = ""
            stock = "OOS"

            if all_variants:
                matched = self.parser.match_variant(all_variants, target_variation)

                if matched:
                    if shopify_data or matched.get("_source") in (None, "html_select", "html_swatch"):
                        price, stock = self.parser.evaluate_from_variant(
                            matched,
                            driver=driver,
                            soup=soup,
                            page_text=visible_page_text
                        )

                        if not price and matched.get("_source") in ("html_select", "html_swatch"):
                            logs.append((
                                f"CE: Clicking variant '{matched.get('title')}' to read live price.",
                                "info", None
                            ))
                            click_ok = self._click_variant_in_dom(driver, matched, logs)
                            if click_ok:
                                self._wait_for_variant_update(driver)
                                v_html = driver.page_source
                                v_soup = self._clean_soup(BeautifulSoup(v_html, "html.parser"))
                                v_text = v_soup.get_text(" ", strip=True)
                                price = self.parser.parse_price_from_html(driver, v_soup, v_text)
                                stock = self.parser.parse_stock_from_html(v_soup, v_text, driver=driver)
                                if stock == "OOS":
                                    price = ""

                    chosen_variant = matched
                else:
                    logs.append((
                        f"CE: No match for '{target_variation}'. Using page default.",
                        "warning", None
                    ))
                    if shopify_data:
                        first_available = next(
                            (v for v in all_variants if v.get("available", True)), all_variants[0]
                        )
                        price, stock = self.parser.evaluate_from_variant(
                            first_available,
                            driver=driver,
                            soup=soup,
                            page_text=visible_page_text
                        )
                        chosen_variant = first_available
                    else:
                        price, stock = self.parser.evaluate_from_html(
                            driver, soup, visible_page_text, url, is_hidden_price=is_hidden_price
                        )
            else:
                logs.append(("CE: No variant data found — treating as simple product.", "info", None))
                if is_hidden_price:
                    price, stock = self.parser.HIDDEN_SENTINEL, "In Stock"
                else:
                    price, stock = self.parser.evaluate_from_html(
                        driver, soup, visible_page_text, url, is_hidden_price=is_hidden_price
                    )

            label = (
                target_variation.strip()
                if target_variation and target_variation.strip()
                else (chosen_variant.get("title", "Default") if chosen_variant else "Default")
            )
            variants_out.append({"label": label, "price": price, "stock": stock})

            if stock == "OOS":
                price = ""

            logs.append((
                f"CE FINAL DECISION → Price: {price} | Stock: {stock} "
                f"| Hidden: {is_hidden_price} | Variation: '{target_variation}'",
                "price", None
            ))

            return "Success", price, stock, name, variants_out, logs

        except Exception as e:
            logs.append((f"Error scraping CollectionsEtc: {str(e)}", "error", None))
            return "Error", "", "OOS", "", [], logs

        finally:
            if is_standalone and driver:
                try:
                    driver.quit()
                except Exception:
                    pass