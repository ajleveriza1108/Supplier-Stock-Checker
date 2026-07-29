import time
import random
import re
from bs4 import BeautifulSoup
from typing import Tuple, List, Dict, Optional, Any

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from core.base_scraper import BaseScraper


class LakesideScraper(BaseScraper):
    """
    Fully Selenium-based scraper for Lakeside.com.
    Made consistent with HarborFreightScraper, MenardsScraper, etc.
    No hybrid requests fallback.
    """

    def __init__(self, browser_manager: Any = None, logger_func: Optional[callable] = None):
        self.browser_manager = browser_manager
        self.log = logger_func

    def _format_price(self, price_str: str) -> str:
        if not price_str or price_str == "N/A": 
            return "N/A"
        m = re.search(r"(\d+(?:\.\d{1,2})?)", price_str.replace(",", ""))
        if m: 
            return f"${float(m.group(1)):.2f}"
        return "N/A"

    def scrape(self, url: str, target_variation: Optional[str] = None) -> Tuple[str, str, str, str, List[Dict[str, Any]], List[Tuple]]:
        logs = []
        
        if not url.startswith("https://www.lakeside.com"):
            logs.append((f"Skipping {url}: Not a lakeside.com URL", "error", None))
            return "Error", "", "OOS", "Unknown Product", [], logs

        if not self.browser_manager:
            logs.append(("Browser manager not initialized for Lakeside", "error", None))
            return "Error", "", "OOS", "Unknown Product", [], logs

        try:
            driver = self.browser_manager.get_driver()
            if not driver:
                logs.append(("Failed to get Selenium driver for Lakeside", "error", None))
                return "Error", "", "OOS", "Unknown Product", [], logs

            if self.log:
                self.log(f"Lakeside → Loading {url}", "info")

            driver.get(url)
            if getattr(driver, "blocked_reason", ""):
                logs.append(("Lakeside: Scrapling verification/CAPTCHA detected; no sheet change.", "error", url))
                return "Error", "", "Captcha", "Blocked by Lakeside", [], logs
            if getattr(driver, "fetch_error", "") and not getattr(driver, "page_source", ""):
                logs.append((f"Lakeside: Scrapling fetch failed: {driver.fetch_error}", "error", url))
                return "Error", "", "Unable to Verify", "Lakeside fetch error", [], logs
            time.sleep(random.uniform(3.0, 6.0))

            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except TimeoutException:
                pass

            html = driver.page_source
            soup = BeautifulSoup(html, "html.parser")
            
            # Title
            title_el = soup.find("h1")
            name = title_el.get_text(strip=True) if title_el else "Unknown Product"

            # Global page text for stock
            page_text = soup.get_text(" ", strip=True).lower()

            # Stock detection
            if any(phrase in page_text for phrase in ["out of stock", "sold out", "limited quantity"]):
                global_stock = "OOS"
            else:
                global_stock = "In Stock"

            # Variant handling (Shopify-style select)
            variants = []
            select_tag = soup.find("select", {"name": "id"})
            
            if select_tag:
                options = select_tag.find_all("option")
                for opt in options:
                    opt_text = opt.get_text(" ", strip=True)
                    opt_text_lower = opt_text.lower()
                    
                    if any(phrase in opt_text_lower for phrase in ["sold out", "unavailable", "limited quantity"]):
                        stock = "OOS"
                    else:
                        stock = "In Stock"
                        
                    parts = opt_text.split(" - ")
                    if len(parts) >= 2:
                        var_label = " - ".join(parts[:-1]).strip()
                        var_price = parts[-1].strip()
                    else:
                        var_label = opt_text.strip()
                        var_price = "N/A"
                        
                    variants.append({
                        "label": var_label,
                        "price": self._format_price(var_price),
                        "stock": stock
                    })
            else:
                # Default for single-variant items
                price_el = soup.select_one(".price-item--sale, .price-item--regular, .price")
                price_text = price_el.get_text(strip=True) if price_el else "N/A"
                variants.append({
                    "label": "Default",
                    "price": self._format_price(price_text),
                    "stock": global_stock
                })

            # Variation matching from Column I
            chosen = None
            if target_variation:
                target_lower = target_variation.lower().strip()
                for v in variants:
                    if v["label"].lower().strip() == target_lower or target_lower in v["label"].lower():
                        chosen = v
                        break
            
            if not chosen and variants:
                chosen = variants[0]

            if self.log:
                self.log(f"Lakeside FINAL → Price: {chosen['price']} | Stock: {chosen['stock']}", "price")

            return "Success", chosen["price"], chosen["stock"], name, variants, logs

        except Exception as e:
            error_msg = f"Error scraping Lakeside: {str(e)}"
            if self.log:
                self.log(error_msg, "error")
            logs.append((error_msg, "error", None))
            return "Error", "", "OOS", "Unknown Product", [], logs