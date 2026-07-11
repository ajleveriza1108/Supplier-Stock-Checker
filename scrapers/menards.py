import time
import random
import re
from bs4 import BeautifulSoup
from typing import Tuple, List, Dict, Optional, Any

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

from core.base_scraper import BaseScraper


class MenardsScraper(BaseScraper):
    """
    Fully Selenium-based scraper for Menards.com.
    Consistent with HarborFreightScraper and WalmartScraper.
    Uses BraveDebugManager (shared browser session) for anti-bot protection.
    """

    def __init__(self, browser_manager=None, logger_func=None):
        self.browser_manager = browser_manager
        self.log = logger_func

    def _format_price(self, price_text: str) -> str:
        """Clean and standardize price to $X.XX format."""
        if not price_text or price_text == "N/A":
            return "N/A"
        
        txt = str(price_text).replace(",", "").replace("\n", " ").strip()
        m = re.search(r'\$?(\d+(?:\.\d{1,2})?)', txt)
        
        if m:
            try:
                val = float(m.group(1))
                if val < 0.00 or val > 50000:
                    return "N/A"
                return f"${val:.2f}"
            except Exception:
                return "N/A"
        return "N/A"

    def scrape(self, url: str, target_variation: Optional[str] = None) -> Tuple[str, str, str, str, List[Dict[str, Any]], List[tuple]]:
        logs: List[tuple] = []
        
        if not url.startswith('https://www.menards.com'):
            msg = f"Skipping {url}: Not a menards.com URL"
            if self.log:
                self.log(msg, "error")
            logs.append((msg, "error", None))
            return "Error", "N/A", "N/A", "N/A", [], logs

        if not self.browser_manager:
            logs.append(("Browser manager not initialized for Menards", "error", None))
            return "Error", "N/A", "OOS", "Unknown Product", [], logs

        try:
            driver = self.browser_manager.get_driver()
            if not driver:
                logs.append(("Failed to get Selenium driver for Menards", "error", None))
                return "Error", "N/A", "OOS", "Unknown Product", [], logs

            # === STEALTH / SPEED LIMITER ===
            delay = random.uniform(3.0, 7.0)
            self._write_log(logs, f"Menards → Waiting {delay:.1f}s to prevent CAPTCHA trigger...", "info")
            time.sleep(delay)

            # === PAGE LOAD ===
            self._write_log(logs, f"Menards → Loading {url}", "info")
            driver.set_page_load_timeout(45)
            driver.get(url)

            # Wait for JS, but explicitly look at the browser title as well
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
                )
                
                WebDriverWait(driver, 15).until(
                    lambda d: d.execute_script("""
                        var txt = document.body ? document.body.innerText.toLowerCase() : '';
                        var title = document.title.toLowerCase();
                        return txt.includes('add to cart') || 
                               txt.includes('ship to home') || 
                               txt.includes('out of stock') || 
                               txt.includes('product not found') || 
                               txt.includes('no longer available') || 
                               txt.includes('access denied') || 
                               txt.includes('security check') ||
                               title.includes('security check') ||
                               title.includes('access denied');
                    """)
                )
            except TimeoutException:
                self._write_log(logs, "Menards: Timed out waiting for React/JS elements. Checking for CAPTCHA...", "warning")

            try:
                driver.execute_script("window.scrollTo(0, 500);")
                time.sleep(1.5)
                driver.execute_script("window.scrollTo(0, 0);")
            except Exception:
                pass

            time.sleep(random.uniform(2.0, 4.0))

            html = driver.page_source
            soup = BeautifulSoup(html, 'html.parser')
            page_text = soup.get_text(" ", strip=True).lower()
            
            # --- THE IRONCLAD CAPTCHA FIX ---
            # We must check the physical browser tab title, because DataDome CAPTCHAs 
            # hide their text inside iframes that BeautifulSoup cannot see.
            page_title = driver.title.lower()
            
            anti_bot_phrases = [
                "access denied", 
                "pardon our interruption", 
                "additional security check", 
                "security check is required",
                "captcha"
            ]

            if any(phrase in page_text for phrase in anti_bot_phrases) or any(phrase in page_title for phrase in anti_bot_phrases):
                self._write_log(logs, f"Menards: CAPTCHA / Anti-Bot Blocked. Title: '{driver.title}'", "error")
                return "Error", "N/A", "Captcha", "Blocked by Menards", [], logs

            # === PAGE NOT FOUND CHECK ===
            if any(phrase in page_text for phrase in [
                "it may have moved or no longer exists",
                "the page you are looking for can not be found",
                "we're sorry, but the product you are looking for is no longer available"
            ]):
                self._write_log(logs, "Menards: Product page not found → OOS", "oos")
                return "Success", "N/A", "OOS", "Product Not Found", [], logs

            # === TITLE ===
            title_elem = soup.select_one("[data-at-id='product-title'], h1.item-title, h1")
            if title_elem:
                title = title_elem.get_text(strip=True)
            else:
                title_elem = soup.find('title')
                title = title_elem.get_text(strip=True).replace(" at Menards®", "") if title_elem else "Unknown Product"

            # === STOCK DETECTION ===
            stock = "OOS"
            buy_area = (
                soup.find('div', id='itemDetailBuyArea')
                or soup.find('main')
                or soup
            )

            if buy_area:
                add_to_cart_btn = None
                for btn in buy_area.find_all(['button', 'input', 'a']):
                    txt = btn.get_text(strip=True).lower()
                    if btn.name == 'input':
                        txt = (btn.get('value') or '').lower()
                        
                    data_id = (btn.get('data-at-id') or '').lower()
                    btn_id = (btn.get('id') or '').lower()
                    btn_class = ' '.join(btn.get('class', [])).lower()
                    
                    if any(trigger in txt for trigger in ['add to cart', 'ship to home']) or \
                       'add-to-cart' in data_id or 'addtocart' in btn_id or 'add-to-cart' in btn_class:
                        add_to_cart_btn = btn
                        break

                if add_to_cart_btn:
                    disabled = add_to_cart_btn.has_attr('disabled') or 'disabled' in str(add_to_cart_btn.get('class', []))
                    if not disabled:
                        stock = "In Stock"

                if stock == "In Stock":
                    buy_text = buy_area.get_text(" ", strip=True).lower()
                    if any(x in buy_text for x in ["out of stock online", "currently out of stock"]):
                        stock = "OOS"

            # === PRICE (only if In Stock) ===
            raw_price = "N/A"
            if stock == "In Stock" and buy_area:
                price_elem = (
                    buy_area.find(attrs={"data-at-id": "full-price-discount-edlp"})
                    or buy_area.select_one('.price-amount, [itemprop="price"], .final-price')
                )
                if price_elem:
                    raw_price = price_elem.get_text(strip=True)
                else:
                    price_match = re.search(r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)', buy_area.get_text(strip=True))
                    if price_match:
                        raw_price = price_match.group(1)
                    else:
                        js_price = driver.execute_script("""
                            let el = document.querySelector('[data-at-id="full-price-discount-edlp"], .price-amount');
                            return el ? el.innerText : null;
                        """)
                        if js_price:
                            raw_price = js_price

            price = self._format_price(raw_price)

            if stock == "OOS":
                price = "N/A" if price == "N/A" else ""

            self._write_log(logs, f"Menards → {title} | Price: {price} | Stock: {stock}", "price")
            return "Success", price, stock, title, [], logs

        except Exception as e:
            error_msg = f"Menards scrape error: {str(e)[:150]}"
            if self.log:
                self.log(error_msg, "error")
            logs.append((error_msg, "error", None))
            return "Error", "N/A", "OOS", "Unknown Product", [], logs

    def _write_log(self, logs: list, message: str, tag: str = "info"):
        """Helper for consistent logging."""
        logs.append((message, tag, None))
        if self.log:
            self.log(message, tag)