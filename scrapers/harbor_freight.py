import time
import random
import re
import json
from bs4 import BeautifulSoup
from typing import Tuple, List, Dict, Optional, Any
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from core.base_scraper import BaseScraper


class HarborFreightScraper(BaseScraper):
    def __init__(self, browser_manager=None, logger_func=None, **kwargs):
        self.browser_manager = browser_manager
        self.log = logger_func

    def _normalize_price_to_str(self, price_text: str) -> str:
        if not price_text:
            return ""
        m = re.search(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+(?:\.[0-9]{2}))",
                      str(price_text).replace("\xa0", " ").strip())
        if not m:
            return ""
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
            if val > 50000:
                return ""
            return f"${val:.2f}"
        except:
            return ""

    def _is_cloudflare_challenge(self, driver) -> bool:
        title = (driver.title or "").lower()
        if "just a moment" in title or "attention required" in title or "cloudflare" in title:
            return True
           
        html = driver.page_source.lower()
        if "before we continue" in html or "press & hold" in html or "cf-wrapper" in html:
            return True
           
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for iframe in iframes:
                src = (iframe.get_attribute("src") or "").lower()
                if "turnstile" in src or "cloudflare" in src:
                    return True
        except Exception:
            pass
        return False

    def _handle_cloudflare_challenge(self, driver):
        if self._is_cloudflare_challenge(driver):
            if self.log:
                self.log("⚠️ CLOUDFLARE BOT CHALLENGE DETECTED! Pausing...", "warning")
            print("\n" + "="*80)
            print("🚨 ACTION REQUIRED: CLOUDFLARE CHALLENGE DETECTED 🚨")
            print("Please go to the Brave browser window and solve the 'Press & Hold' challenge.")
            print("="*80 + "\n")
           
            while self._is_cloudflare_challenge(driver):
                time.sleep(3)
            print("\n✅ Cloudflare challenge solved! Resuming Harbor Freight scraper...\n")
            time.sleep(4)

    def _parse_stock(self, driver, page_text_lower: str) -> str:
        if self.log:
            self.log("HF: Starting stock analysis...", "info")

        # 1. STRONGEST POSITIVE SIGNAL: Add to Cart button
        try:
            atc_buttons = driver.find_elements(
                By.CSS_SELECTOR,
                'button.add-to-cart, button[data-testid="add-to-cart"], button[class*="add-to-cart"], '
                '.add-to-cart-btn, button[aria-label*="Add to cart"], button[title*="Add to cart"]'
            )
            if atc_buttons:
                for btn in atc_buttons:
                    if btn.is_displayed() and not btn.get_attribute("disabled"):
                        if self.log:
                            self.log("HF: Active Add to Cart button found → IN STOCK", "info")
                        return "In Stock"
        except Exception:
            pass

        # 2. Explicit OOS phrases
        oos_phrases = [
            "out of stock", "sold out", "temporarily out of stock",
            "currently unavailable", "not available", "sorry, this item"
        ]
        for phrase in oos_phrases:
            if phrase in page_text_lower:
                if self.log:
                    self.log(f"HF: Explicit OOS phrase '{phrase}' found → OOS", "oos")
                return "OOS"

        # 3. Positive fallback
        if any(x in page_text_lower for x in ["in stock", "add to cart", "buy now", "available now"]):
            if self.log:
                self.log("HF: Positive stock signals found → IN STOCK", "info")
            return "In Stock"

        # 4. Default (lean toward In Stock)
        if self.log:
            self.log("HF: No strong OOS signals → defaulting to IN STOCK", "info")
        return "In Stock"

    def _single_scrape(self, driver, url: str) -> Tuple[str, str, str, str, List[Tuple[str, str, Optional[str]]]]:
        logs = []
        try:
            driver.set_page_load_timeout(40)
            driver.get(url)
            if getattr(driver, "blocked_reason", ""):
                logs.append(("HF: Scrapling verification/CAPTCHA detected; no sheet change.", "error", url))
                return "Error", "", "Captcha", "Blocked by Harbor Freight", logs
            if getattr(driver, "fetch_error", "") and not getattr(driver, "page_source", ""):
                logs.append((f"HF: Scrapling fetch failed: {driver.fetch_error}", "error", url))
                return "Error", "", "Unable to Verify", "Harbor Freight fetch error", logs

            try:
                WebDriverWait(driver, 18).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except TimeoutException:
                pass

            time.sleep(random.uniform(3.0, 5.5))

            self._handle_cloudflare_challenge(driver)

            page_text_lower = driver.page_source.lower()
            stock = self._parse_stock(driver, page_text_lower)

            # Double-check if In Stock was detected
            if stock == "In Stock":
                if self.log:
                    self.log("HF: In Stock detected → performing double-check re-scrape...", "info")
                time.sleep(random.uniform(2.0, 4.0))
                page_text_lower = driver.page_source.lower()
                stock = self._parse_stock(driver, page_text_lower)

            # Price
            price = ""
            try:
                price_elem = driver.find_element(By.CSS_SELECTOR, '.price, .current-price, [data-testid="product-price"], .product-price')
                price = self._normalize_price_to_str(price_elem.text)
            except Exception:
                pass

            if not price:
                try:
                    scripts = driver.find_elements(By.TAG_NAME, "script")
                    for script in scripts:
                        if script.get_attribute("type") == "application/ld+json":
                            data = json.loads(script.get_attribute("innerHTML") or "{}")
                            if isinstance(data, dict) and "offers" in data:
                                p = data.get("offers", {}).get("price")
                                if p:
                                    price = self._normalize_price_to_str(str(p))
                                    break
                except Exception:
                    pass

            soup = BeautifulSoup(driver.page_source, "html.parser")
            title = ""
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)
            if not title:
                title = soup.title.string.strip() if soup.title else "Unknown Product"

            if stock == "OOS":
                price = ""

            if self.log:
                self.log(f"HF FINAL → Price: {price} | Stock: {stock}", "price")

            return "Success", price, stock, title, logs

        except Exception as e:
            error_msg = f"Error scraping HF: {str(e)}"
            logs.append((error_msg, "error", None))
            if self.log:
                self.log(error_msg, "error")
            return "Error", "", "OOS", "Unknown Product", logs

    def scrape(self, url: str, target_variation: Optional[str] = None) -> Tuple[str, str, str, str, List[Dict[str, Any]], List[Tuple[str, str, Optional[str]]]]:
        logs = []
        if not url.startswith("https://www.harborfreight.com"):
            logs.append((f"Skipping {url}: Not a harborfreight.com URL", "error", None))
            return "Error", "", "OOS", "Unknown Product", [], logs

        driver = self.browser_manager.get_driver()
        if not driver:
            logs.append(("Browser initialization failed", "error", None))
            return "Error", "", "OOS", "Unknown Product", [], logs

        if self.log:
            self.log(f"HF → Loading {url}", "info")

        status, price, stock, title, logs1 = self._single_scrape(driver, url)
        logs.extend(logs1)

        variants = [{"label": "Default", "price": price, "stock": stock}]

        return status, price, stock, title, variants, logs