import sys
import os
import time
import re
import json
import random
from typing import Tuple, List, Dict
from bs4 import BeautifulSoup

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

from core.base_scraper import BaseScraper

class WebstaurantScraper(BaseScraper):
    """
    WebstaurantStore.com Scraper (Ultra-Stable Native Selenium)
    Removed `undetected_chromedriver` to prevent infinite initialization hangs.
    Includes built-in human velocity delays, JSON-LD pricing extraction,
    and strict checks for "Reference Only" discontinued ghost pages.
    """

    def __init__(self, browser_manager=None, logger_func=None, use_physical_browser=False):
        super().__init__(browser_manager, logger_func)
        self.use_physical_browser = use_physical_browser and browser_manager is not None
        self.cloudscraper = cloudscraper.create_scraper() if HAS_CLOUDSCRAPER else None
        
        if self.log:
            mode = "Physical Browser" if self.use_physical_browser else "Native Selenium Stealth"
            self.log(f"WEB: Browser mode = {mode}", "info")

    def _quick_http_probe(self, url: str) -> str:
        if not self.cloudscraper:
            return ""
        try:
            response = self.cloudscraper.get(
                url,
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.webstaurantstore.com/",
                }
            )
            if response.status_code == 200:
                return response.text
        except Exception:
            pass
        return ""

    def _load_page_with_browser(self, url: str, driver: webdriver.Chrome) -> Tuple[BeautifulSoup, str, str]:
        # Bulletproof timeout to prevent infinite page-loading hangs
        driver.set_page_load_timeout(35)

        try:
            driver.get(url)

            print("=" * 80)
            print("REQUESTED URL :", url)
            print("CURRENT URL   :", driver.current_url)
            print("=" * 80)

        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            pass

        try:
            WebDriverWait(driver, 5).until(
                lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
            )
        except TimeoutException:
            pass

        time.sleep(3.5)

        try:
            # Scroll down to trigger lazy-loaded Datadome/Pricing scripts
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 3);")
            time.sleep(1)
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

        raw_html = driver.page_source

        print("TITLE         :", driver.title)
        print("FINAL URL     :", driver.current_url)
        print("HTML LENGTH   :", len(raw_html))
        print("HTML HEAD:")
        print(raw_html[:500])
        print("=" * 80)
        
        soup = BeautifulSoup(raw_html, "html.parser")
        visible_text = soup.get_text(" ", strip=True)
        
        return soup, visible_text, raw_html

    def _aggressive_price_hunt(self, soup: BeautifulSoup, raw_html: str) -> str:
        # 1. Search JSON-LD Structured Data (Most accurate)
        json_ld = soup.find_all('script', type='application/ld+json')
        for script in json_ld:
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if 'offers' in item:
                        offers = item['offers']
                        price = offers[0].get('price') if isinstance(offers, list) else offers.get('price')
                        if price:
                            val = float(price)
                            if val > 0:
                                return f"${val:.2f}"
            except Exception:
                continue

        # 2. Search Standard Webstaurant DOM Elements
        selectors = [
            '[data-testid="price"]', '.price', '.pricing__price', 
            '.product-price', '#price', '[itemprop="price"]'
        ]
        for sel in selectors:
            for el in soup.select(sel):
                txt = el.get_text(" ", strip=True)
                m = re.search(r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)', txt)
                if m:
                    val = float(m.group(1).replace(',', ''))
                    if val > 0:
                        return f"${val:.2f}"

        # 3. Fallback Regex hunt in HTML
        match = re.search(r'(?i)(?:price|cost).{0,150}?\$(\d+(?:,\d{3})*(?:\.\d{2})?)', raw_html)
        if match:
            val = float(match.group(1).replace(',', ''))
            if val > 0: 
                return f"${val:.2f}"

        return ""

    def scrape(self, url: str, target_variation: str = "") -> Tuple[str, str, str, str, List[Dict], List]:
        logs = []
        status = "Success"
        price = ""
        stock = "OOS"
        title = "Unknown"
        variants = []
        driver = None
        is_standalone_driver = False

        try:
            # --- DATADOME ANTI-VELOCITY DELAY ---
            delay = random.uniform(3.0, 6.5)
            self._write_log(logs, f"WEB: Sleeping for {delay:.1f}s to prevent Datadome rate-limiting...", "info")
            time.sleep(delay)

            probe_html = self._quick_http_probe(url)
            
            if self.use_physical_browser and self.browser_manager:
                driver = self.browser_manager.get_driver()
            else:
                driver = self._get_driver_with_stealth()
                is_standalone_driver = True

            if not driver:
                raise RuntimeError("Failed to initialize Chrome driver. Please check background Chrome processes.")
            
            soup, visible_text, raw_html = self._load_page_with_browser(url, driver)

            # Ensure logs directory exists before writing debug file
            os.makedirs("logs", exist_ok=True)
            safe_name = re.sub(r"[^A-Za-z0-9]", "_", url)
            debug_file = os.path.join("logs", f"WEB_{safe_name}.html")

            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(raw_html)

            self._write_log(logs, f"Saved debug HTML: {debug_file}", "info")

            lower_text = visible_text.lower()

            # --- CAPTCHA & 404 CHECKS ---
            if "the page you requested could not be found" in lower_text or "404" in lower_text:
                self._write_log(logs, "WEB: 404 Page Not Found detected -> OOS", "oos")
                return "Success", "", "OOS", "Unknown Product (404)", [{"label": target_variation or "Default", "price": "", "stock": "OOS"}], logs

            if self._has_captcha_element(soup, lower_text):
                raise RuntimeError("Datadome/Cloudflare CAPTCHA blocked the request.")

            # --- TITLE EXTRACTION ---
            h1 = soup.select_one("h1.page-title, h1[data-testid='itemDescription'], h1")
            if h1:
                title = h1.get_text(" ", strip=True)

            # --- GHOST PAGE / DISCONTINUED CHECK (WEBSTAURANT SPECIFIC) ---
            is_definitively_oos = False
            
            discontinued_phrases = [
                "this product is no longer available",
                "we've left this page up for reference only"
            ]
            
            if any(phrase in lower_text for phrase in discontinued_phrases):
                self._write_log(logs, "WEB: 'Reference Only' discontinued ghost page detected. Forcing OOS.", "warning")
                is_definitively_oos = True
                stock = "OOS"
                price = ""
            else:
                # --- PRICE PARSING ---
                price = self._aggressive_price_hunt(soup, raw_html)

                # --- STOCK VALIDATION ---
                oos_indicators = ["out of stock", "currently unavailable", "sold out"]
                if any(phrase in lower_text for phrase in oos_indicators):
                    is_definitively_oos = True

                is_btn_disabled = True
                try:
                    # Check for active Add to Cart buttons
                    cart_btns = driver.find_elements(By.CSS_SELECTOR, "button#buyButton, [data-testid='add-to-cart'], button.add-to-cart, input#buyButton")
                    for btn in cart_btns:
                        btn_txt = (btn.get_attribute("textContent") or btn.get_attribute("value") or "").lower()
                        
                        if "add to cart" in btn_txt:
                            if btn.is_displayed() and btn.is_enabled():
                                if "disabled" not in (btn.get_attribute("class") or "").lower() and not btn.get_attribute("disabled"):
                                    is_btn_disabled = False
                                    break
                except Exception:
                    is_btn_disabled = True

                if is_definitively_oos:
                    stock = "OOS"
                elif not is_btn_disabled:
                    stock = "In Stock"
                else:
                    stock = "OOS"

            # --- ZERO-DOLLAR FAILSAFE ---
            if price == "$0.00" or price == "0.00":
                stock = "OOS"
                price = ""

            # --- BLANK PRICE GHOST ITEM OVERRIDE ---
            if stock == "In Stock" and not str(price).strip():
                self._write_log(logs, "WEB: Evaluated as In Stock but no valid price was found. Forcing OOS.", "warning")
                stock = "OOS"
                price = ""

            variants = [{"label": target_variation or "Default", "price": price, "stock": stock}]
            
        except Exception as e:
            status = "Error"
            stock = "OOS"
            error_msg = f"WEB error: {str(e)[:150]}"
            self._write_log(logs, error_msg, "error")
        
        finally:
            if is_standalone_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass

        return status, price, stock, title, variants, logs

    def _write_log(self, logs, msg, tag):
        logs.append((msg, tag, None))
        if self.log: self.log(msg, tag)

    def _has_captcha_element(self, soup: BeautifulSoup, lower_text: str) -> bool:
        """
        Strict CAPTCHA detection. 
        Ignores generic 'recaptcha' scripts and explicitly looks for hard-blocking 
        Cloudflare/Datadome/PerimeterX splash pages.
        """
        title = soup.title.string.lower() if soup.title and soup.title.string else ""
        if "just a moment" in title or "pardon our interruption" in title or "attention required" in title:
            return True

        if soup.find("div", {"id": "challenge-error-title"}) or soup.find("div", {"id": "px-captcha"}):
            return True

        if "pardon our interruption" in lower_text and "verify you are a human" in lower_text:
            return True
            
        if "enable javascript and cookies" in lower_text and "access" in lower_text:
            return True

        return False

    def _get_driver_with_stealth(self):
        """
        Returns a Native Selenium driver with CDP stealth properties.
        COMPLETELY removes `undetected_chromedriver` to stop infinite thread hanging.
        """
        try:
            options = Options()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-blink-features=AutomationControlled")
            
            # Remove Selenium flags from the browser signature
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            
            options.add_argument("--headless=new")
            options.add_argument(
                "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
            
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            
            # Mask the webdriver signature via Chrome DevTools Protocol
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"},
            )
            
            return driver
        
        except Exception as e:
            if self.log: self.log(f"WEB: Failed to initialize native driver - {e}", "error")
            return None