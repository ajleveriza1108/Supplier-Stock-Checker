"""
Walmart Scraper - HYBRID JSON/REDUX EXTRACTION METHOD

Hunts for the hidden `__NEXT_DATA__` and `REDUX_STATE` JSON blocks.
Includes Price-Validation: If an item says "In Stock" but has no price, it is 
flagged as OOS to prevent "Ghost Data" false positives.
Extracts granular stock (Low Stock, Limited Stock, Only X remaining).
"""

import time
import re
import json
from bs4 import BeautifulSoup
from typing import Tuple, List, Dict, Optional, Any
from selenium.common.exceptions import TimeoutException

from core.base_scraper import BaseScraper

INITIAL_WAIT = 10 

class WalmartScraper(BaseScraper):
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
                return "Error", "", "OOS", "Unknown", [], [("No browser manager", "error", url)]
            try:
                driver = self.browser_manager.get_driver()
            except Exception as e:
                return "Error", "", "OOS", "Unknown", [], [(f"get_driver error: {str(e)[:60]}", "error", url)]

            item_id_match = re.search(r'/(\d+)(?:[/?#]|$)', url)
            target_id = item_id_match.group(1) if item_id_match else None

            if not target_id:
                self._write_log(logs, "WAL: Could not extract an Item ID from the URL.", "warning")

            try:
                driver.set_page_load_timeout(35)
                driver.get(url)
            except TimeoutException:
                self._write_log(logs, "WAL: page load timeout — continuing to parse available DOM", "warning")
            except Exception as e:
                return "Error", "", "OOS", "", [], [(f"driver.get error: {str(e)[:60]}", "error", url)]

            self._write_log(logs, f"WAL: Waiting {INITIAL_WAIT}s for payload injection...", "info")
            time.sleep(INITIAL_WAIT)

            html = driver.page_source
            soup = BeautifulSoup(html, "html.parser")

            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)

            json_found = False
            qty = None
            raw_json_str = ""
            
            # Try 1: __NEXT_DATA__
            next_data_script = soup.find("script", id="__NEXT_DATA__")
            if next_data_script and next_data_script.string:
                try:
                    data = json.loads(next_data_script.string)
                    if isinstance(data, dict):
                        json_found = True
                        raw_json_str = json.dumps(data)
                        self._write_log(logs, "WAL: __NEXT_DATA__ JSON intercepted.", "info")
                        
                        if target_id:
                            item_data = self._recursive_json_search(data, target_id)
                            if item_data:
                                availability = item_data.get("availabilityStatus", "")
                                stock = "In Stock" if availability == "IN_STOCK" else "OOS"
                                
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
                                stock = "OOS"
                                price = ""
                        else:
                            price, stock = self._fallback_json_parse(data)
                except json.JSONDecodeError:
                    pass

            # Try 2: REDUX_STATE
            if not price and stock == "OOS":
                redux_tag = soup.find("script", id="Items-REDUX_STATE") or soup.find("script", string=re.compile("REDUX_STATE"))
                if redux_tag and redux_tag.string:
                    try:
                        clean_json_match = re.search(r'\{.*\}', redux_tag.string)
                        if clean_json_match:
                            data = json.loads(clean_json_match.group(0))
                            if isinstance(data, dict):
                                json_found = True
                                raw_json_str = json.dumps(data)
                                self._write_log(logs, "WAL: REDUX_STATE JSON intercepted.", "info")
                                
                                if target_id:
                                    item_data = self._extract_from_redux(data, target_id)
                                    if item_data:
                                        stock = item_data['stock']
                                        price = item_data['price']
                    except Exception:
                        pass

            if stock == "In Stock" and not price:
                self._write_log(logs, "WAL: In Stock found but no Price detected. Flagging as OOS to prevent Ghost Update.", "oos")
                stock = "OOS"

            if not json_found or (not price and stock == "In Stock"):
                self._write_log(logs, "WAL: Falling back to UI visual extraction.", "warning")
                stock = self._fallback_visual_stock(html, soup)
                if stock == "In Stock":
                    price = self._fallback_visual_price(soup)

            # --- APPLY GRANULAR USER STOCK CONDITIONS ---
            if stock == "In Stock":
                stock = self._refine_stock(html + " " + raw_json_str, qty)

            if stock == "OOS":
                price = ""

            variants = [{"label": target_variation or "Default", "price": price, "stock": stock}]
            self._write_log(logs, f"WAL FINAL → Price: {price} | Stock: {stock}", "price" if stock != "OOS" else "oos")

            return status, price, stock, title, variants, logs

        except Exception as e:
            self._write_log(logs, f"WAL: unhandled exception: {str(e)[:80]}", "error")
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

    def _recursive_json_search(self, data: Any, target_id: str) -> Optional[Dict]:
        if isinstance(data, dict):
            if str(data.get("usItemId", "")) == target_id or str(data.get("id", "")) == target_id:
                if "availabilityStatus" in data or "priceInfo" in data:
                    return data
            for key, value in data.items():
                if key in ["modules", "related", "sponsoredItems"]: 
                    continue
                result = self._recursive_json_search(value, target_id)
                if result:
                    return result
        elif isinstance(data, list):
            for item in data:
                result = self._recursive_json_search(item, target_id)
                if result:
                    return result
        return None

    def _extract_from_redux(self, data: dict, target_id: str) -> Optional[Dict]:
        try:
            for key, val in data.items():
                if isinstance(val, dict) and str(val.get("usItemId")) == target_id:
                    avail = val.get("availabilityStatus")
                    price_obj = val.get("price") or {}
                    current_price_obj = price_obj.get("currentPrice") or {}
                    price_val = current_price_obj.get("price")
                    return {
                        'stock': "In Stock" if avail == "IN_STOCK" else "OOS", 
                        'price': f"${price_val}" if price_val else ""
                    }
        except Exception:
            pass
        return None

    def _fallback_json_parse(self, data: dict) -> Tuple[str, str]:
        price = ""
        stock = "OOS"
        if not isinstance(data, dict):
            return price, stock
        try:
            props = data.get("props") or {}
            pageProps = props.get("pageProps") or {}
            initialData = pageProps.get("initialData") or {}
            d = initialData.get("data") or {}
            product_data = d.get("product") or {}
            if product_data:
                availability = product_data.get("availabilityStatus", "")
                stock = "In Stock" if availability == "IN_STOCK" else "OOS"
                price_info = product_data.get("priceInfo") or {}
                current_price_obj = price_info.get("currentPrice") or {}
                current_price = current_price_obj.get("price")
                if current_price is not None:
                    price = f"${float(current_price):.2f}"
        except Exception:
            pass
        return price, stock

    def _fallback_visual_stock(self, html: str, soup: BeautifulSoup) -> str:
        t = html.lower()
        atc_btn = soup.find("button", {"data-automation-id": "add-to-cart"})
        if not atc_btn:
            atc_btn = soup.find("button", string=re.compile(r"Add to cart", re.I))
        if atc_btn and not atc_btn.has_attr("disabled"):
            return "In Stock"
        if any(p in t for p in ["we couldn't find this page", "page not found"]):
            return "OOS"
        if "more seller options" in t or "compare all sellers" in t:
            return "In Stock"
        
        buy_box = soup.find("div", {"data-testid": "buy-box"}) or soup.find("div", class_=re.compile("buybox", re.I))
        if buy_box:
            bb_text = buy_box.get_text(separator=" ", strip=True).lower()
            if any(p in bb_text for p in ["out of stock", "sold out", "currently unavailable"]):
                return "OOS"
        elif any(p in t for p in ["out of stock", "sold out", "currently unavailable"]):
            return "OOS"
            
        return "In Stock"

    def _fallback_visual_price(self, soup: BeautifulSoup) -> str:
        try:
            price_node = soup.find(attrs={"itemprop": "price"})
            if price_node:
                raw_price = price_node.get_text(strip=True)
                m = re.search(r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)', raw_price)
                if m: return f"${m.group(1)}"
            for el in soup.find_all(["span", "div"]):
                if el.has_attr("class") and any("price" in c.lower() for c in el["class"]):
                    text = el.get_text(strip=True)
                    m = re.search(r'^\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)$', text)
                    if m: return f"${m.group(1)}"
        except Exception:
            pass
        return ""