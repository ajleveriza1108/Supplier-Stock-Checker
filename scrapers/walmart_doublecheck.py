"""
Walmart Double-Check Scraper - STRICT BUY-BOX PROTOCOL

- Blocks "Ghost Data" from Recommended Items / Carousels.
- Safe-guards against 'NoneType' crashes caused by JSON null values.
- Retains specific user stock conditions (Low Stock, Only X Remaining)
"""

import time
import random
import re
import json
from bs4 import BeautifulSoup
from typing import Tuple, List, Dict, Optional, Any
from selenium.common.exceptions import TimeoutException

from core.base_scraper import BaseScraper

INITIAL_WAIT_MIN = 12
INITIAL_WAIT_MAX = 15

class WalmartDoubleCheckScraper(BaseScraper):

    def __init__(self, browser_manager=None, logger_func=None, **kwargs):
        super().__init__(browser_manager, logger_func)

    def scrape(
        self,
        url: str,
        target_variation: Optional[str] = None,
    ) -> Tuple[str, str, str, str, List[Dict[str, Any]], List]:

        status = "Success"
        price  = ""
        stock  = "OOS"  
        title  = "Unknown"
        logs: List = []

        try:
            if not self.browser_manager:
                return "Error", "", "OOS", "", [], [("No browser manager", "error", url)]

            try:
                driver = self.browser_manager.get_driver()
            except Exception as e:
                return "Error", "", "OOS", "", [], [(f"get_driver: {str(e)[:60]}", "error", url)]

            item_id_match = re.search(r'/(\d+)(?:[/?#]|$)', url)
            target_id = item_id_match.group(1) if item_id_match else None

            try:
                driver.set_page_load_timeout(35)
                driver.get(url)
            except TimeoutException:
                pass 
            except Exception as e:
                if self.log: 
                    self.log(f"WAL-DC: Page load issue: {str(e)[:50]} - continuing", "warning")

            wait_secs = random.uniform(INITIAL_WAIT_MIN, INITIAL_WAIT_MAX)
            if self.log:
                self.log(f"WAL-DC: waiting {wait_secs:.1f}s for page render…", "info")
            time.sleep(wait_secs)

            html = driver.page_source or ""
            soup = BeautifulSoup(html, "html.parser")

            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)

            json_found = False
            qty = None
            raw_json_str = ""

            next_data_script = soup.find("script", id="__NEXT_DATA__")
            
            if next_data_script and next_data_script.string:
                try:
                    data = json.loads(next_data_script.string)
                    if isinstance(data, dict):
                        json_found = True
                        raw_json_str = json.dumps(data)
                        if self.log: 
                            self.log("WAL-DC: __NEXT_DATA__ JSON intercepted.", "info")
                        
                        props = data.get("props") or {}
                        pageProps = props.get("pageProps") or {}
                        initialData = pageProps.get("initialData") or {}
                        d = initialData.get("data") or {}
                        product_data = d.get("product") or {}
                        
                        prod_id = str(product_data.get("id", ""))
                        us_item_id = str(product_data.get("usItemId", ""))
                        
                        item_data = None
                        if target_id and target_id in [prod_id, us_item_id]:
                            item_data = product_data
                        elif target_id:
                            item_data = self._strict_recursive_search(data, target_id)
                        else:
                            item_data = product_data

                        if item_data:
                            availability = item_data.get("availabilityStatus", "")
                            buy_box = item_data.get("buyBox") or {}
                            is_oos = buy_box.get("isOutOfStock", False)
                            
                            if is_oos or availability in ["OUT_OF_STOCK", "PREORDER", "UNKNOWN"]:
                                stock = "OOS"
                            else:
                                cta_obj = buy_box.get("cta") or {}
                                cta_text = cta_obj.get("text", "").lower()
                                if cta_text in ["add to cart", "choose options"]:
                                    stock = "In Stock"
                                elif availability == "IN_STOCK":
                                    stock = "In Stock"
                                else:
                                    stock = "OOS"

                            try:
                                if "availableQuantity" in item_data:
                                    qty = int(item_data["availableQuantity"])
                            except Exception:
                                pass

                            price_info = item_data.get("priceInfo") or {}
                            current_price_obj = price_info.get("currentPrice") or {}
                            current_price = current_price_obj.get("price")
                            
                            if current_price is not None:
                                price = f"${float(current_price):.2f}"
                                
                        else:
                            if self.log: 
                                self.log(f"WAL-DC: Item ID {target_id} not found in valid JSON blocks.", "oos")
                            stock = "OOS"

                except json.JSONDecodeError:
                    if self.log: 
                        self.log("WAL-DC: Failed to decode __NEXT_DATA__ JSON.", "error")

            if not json_found:
                if self.log: 
                    self.log("WAL-DC: Falling back to Visual Buy-Box extraction.", "warning")
                stock = self._fallback_visual_stock(html, soup)
                if stock == "In Stock":
                    price = self._fallback_visual_price(soup)

            # --- APPLY GRANULAR USER STOCK CONDITIONS ---
            if stock == "In Stock":
                stock = self._refine_stock(html + " " + raw_json_str, qty)

            if stock == "OOS":
                price = ""

            variants = [{"label": target_variation or "Default", "price": price, "stock": stock}]
            if self.log: 
                self.log(f"WAL-DC FINAL → Price: {price} | Stock: {stock}", "price" if stock != "OOS" else "oos")

            return status, price, stock, title, variants, logs

        except Exception as e:
            if self.log: 
                self.log(f"WAL-DC: unhandled exception: {str(e)[:80]}", "error")
            return "Error", "", "OOS", "Unknown", [], [(f"Exception: {str(e)[:60]}", "error", url)]

    def _refine_stock(self, text_payload: str, qty: Optional[int]) -> str:
        """Parses the data specifically for user's granular stock phrases."""
        text_lower = text_payload.lower()
        
        if qty is not None and 0 < qty <= 10:
            return f"Only {qty} remaining"
            
        m = re.search(r'only\s+(\d+)\s+(?:left|remaining)', text_lower)
        if m:
            return f"Only {m.group(1)} remaining"
            
        if "low stock" in text_lower:
            return "Low Stock"
            
        if "limited stock" in text_lower:
            return "Limited Stock"
            
        return "In Stock"

    def _strict_recursive_search(self, d: Any, target_id: str) -> Optional[Dict]:
        if isinstance(d, dict):
            if str(d.get("usItemId", "")) == target_id or str(d.get("id", "")) == target_id:
                if "availabilityStatus" in d or "priceInfo" in d:
                    return d
            
            for k, v in d.items():
                if k in ["modules", "itemCarousel", "relatedProducts", "sponsoredItems", "reviews"]:
                    continue
                result = self._strict_recursive_search(v, target_id)
                if result: 
                    return result
                    
        elif isinstance(d, list):
            for item in d:
                result = self._strict_recursive_search(item, target_id)
                if result: 
                    return result
        return None

    def _fallback_visual_stock(self, html: str, soup: BeautifulSoup) -> str:
        t = html.lower()
        if any(p in t for p in ["we couldn't find this page", "page not found"]):
            return "OOS"
            
        buy_box = soup.find("div", {"data-testid": "buy-box"}) or soup.find("div", class_=re.compile("buybox", re.I))
        if buy_box:
            bb_text = buy_box.get_text(separator=" ", strip=True).lower()
            if any(p in bb_text for p in ["out of stock", "sold out", "currently unavailable"]):
                return "OOS"
            if "add to cart" in bb_text:
                return "In Stock"
        
        atc_btn = soup.find("button", {"data-automation-id": "add-to-cart"})
        if atc_btn:
            if atc_btn.has_attr("disabled"):
                return "OOS"
            return "In Stock"
            
        return "OOS"

    def _fallback_visual_price(self, soup: BeautifulSoup) -> str:
        try:
            price_node = soup.find(attrs={"itemprop": "price"})
            if price_node:
                raw_price = price_node.get_text(strip=True)
                m = re.search(r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)', raw_price)
                if m: 
                    return f"${m.group(1)}"
            for el in soup.find_all(["span", "div"]):
                if el.has_attr("class") and any("price" in c.lower() for c in el["class"]):
                    text = el.get_text(strip=True)
                    m = re.search(r'^\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)$', text)
                    if m: 
                        return f"${m.group(1)}"
        except Exception:
            pass
        return ""