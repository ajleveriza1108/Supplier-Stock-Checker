import re
import json
from bs4 import BeautifulSoup
from typing import Tuple, Optional
from price_parser import Price


class SGPriceParser:
    HIDDEN_SENTINEL = "See Member Price in Checkout"

    def __init__(self, logger_func=None):
        self.log = logger_func

    # ── PRICE EXTRACTION (COMPREHENSIVE MULTI-METHOD) ────────────────────────

    def parse_price(self, soup: BeautifulSoup, page_text: str, url: str = "",
                    is_hidden_price: bool = False, driver=None) -> str:
        """
        Extracts ONLY the Buyer's Club / Member price.
        Ignores all regular, sale, and MSRP prices per strict business rules.
        Never falls back to generic prices. Returns "" if Club price is not found.
        """
        if is_hidden_price:
            return ""

        candidates = []

        def add_candidate(val_float: float, source: str):
            price_str = f"${val_float:.2f}"
            candidates.append({
                "price": price_str,
                "value": val_float,
                "source": source
            })
            if self.log:
                self.log(f"SG: Candidate Found | Source: {source} | Price: {price_str} | Classification: Buyer Club", "info")

        # ISOLATE DOM: Prevent bleeding from related items and cross-sells
        main_selectors = ["#pdp-right-column", ".product-buy-box", ".buy-box", ".pdp-right", ".product-info", "#content"]
        main_box = None
        for sel in main_selectors:
            main_box = soup.select_one(sel)
            if main_box:
                break
        
        search_soup = main_box if main_box else soup
        visible_text = search_soup.get_text(" ", strip=True)

        # METHOD 1: JavaScript extraction (Strictly Club Selectors)
        if driver:
            try:
                # Scroll to trigger lazy-loaded prices
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
                import time
                time.sleep(0.8)
                driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(0.5)
                
                js_results = driver.execute_script("""
                    var results = [];
                    var root = document.querySelector('#pdp-right-column, .product-buy-box, .buy-box, .pdp-right, .product-info, #content') || document;
                    
                    var selectors = [
                        '.price-buyer-club .price', '.price-buyer-club', '.clubprice', '.club-price',
                        '.member-price', '[class*="club-price"]', '[class*="clubprice"]', '[class*="member-price"]'
                    ];
                    for (var i = 0; i < selectors.length; i++) {
                        var els = root.querySelectorAll(selectors[i]);
                        for(var j=0; j<els.length; j++) {
                            var el = els[j];
                            var txt = (el.textContent || el.innerText || '').trim();
                            var m = txt.match(/\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)/);
                            if (m) {
                                var val = parseFloat(m[1].replace(/,/g, ''));
                                if (val > 0.00 && val <= 100000) {
                                    var context = (el.className || '') + ' ' + (el.id || '') + ' ' + txt;
                                    results.push([val.toFixed(2), "JS CSS " + selectors[i]]);
                                }
                            }
                        }
                    }
                    
                    var dataEls = root.querySelectorAll('[data-clubprice], [data-club-price]');
                    for (var j = 0; j < dataEls.length; j++) {
                        var attrs = ['data-clubprice', 'data-club-price'];
                        for (var k = 0; k < attrs.length; k++) {
                            var dp = dataEls[j].getAttribute(attrs[k]);
                            if (dp && dp.match(/^\d+\.?\d{0,2}$/)) {
                                var v = parseFloat(dp);
                                if (v > 0.00 && v <= 100000) {
                                    results.push([v.toFixed(2), "JS attr " + attrs[k]]);
                                }
                            }
                        }
                    }
                    
                    // Strict label extraction from text
                    var allText = root.innerText || '';
                    var priceMatches = allText.match(/(?:buyer'?s?\s*club|member\s*price|club\s*price)[^$]{0,30}\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?/gi);
                    if (priceMatches && priceMatches.length > 0) {
                        for (var k = 0; k < priceMatches.length; k++) {
                            var m = priceMatches[k].match(/\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)/);
                            if(m) {
                                var num = parseFloat(m[1].replace(/[$,\s]/g, ''));
                                if (num > 0.00 && num <= 100000) {
                                    results.push([num.toFixed(2), "JS Strict Text Parsing"]);
                                }
                            }
                        }
                    }
                    
                    return results;
                """)
                
                if isinstance(js_results, list):
                    for val_str, source in js_results:
                        try:
                            add_candidate(float(val_str), source)
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                if self.log:
                    self.log(f"SG: JS extraction failed: {str(e)[:80]}", "warning")

        # METHOD 2: Hidden input fields (Strictly Club Inputs)
        hidden_input_ids = [
            "hdnClubPrice", "hdnClubPriceAmt", "ClubPrice", "clubPrice"
        ]
        for input_id in hidden_input_ids:
            el = search_soup.find("input", {"id": re.compile(input_id, re.I)})
            if not el:
                el = search_soup.find("input", {"name": re.compile(input_id, re.I)})
            if el and el.get("value"):
                parsed = Price.fromstring(str(el.get("value")))
                if parsed.amount_float is not None and 0.00 < parsed.amount_float <= 100000:
                    add_candidate(parsed.amount_float, f"Hidden Input {input_id}")

        # METHOD 3: CSS selectors (Strictly Club Classes)
        css_selectors = [
            ".price-buyer-club .price", ".price-buyer-club", "span[class*='club-price']",
            "span[class*='clubprice']", "div[class*='club-price']", "div[class*='clubprice']",
            ".member-price", ".club-price", ".clubprice"
        ]
        for selector in css_selectors:
            try:
                for el in search_soup.select(selector):
                    txt = el.get_text(" ", strip=True)
                    if not txt or "see" in txt.lower():
                        continue
                    m = re.search(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?|[0-9]+(?:\.\d{2})?)", txt)
                    if m:
                        parsed = Price.fromstring(m.group(1))
                        if parsed.amount_float is not None and 0.00 < parsed.amount_float <= 100000:
                            add_candidate(parsed.amount_float, f"CSS {selector}")
            except Exception:
                continue

        # METHOD 4: Strict Visible Text Parsing (Only near Club labels)
        try:
            matches = re.finditer(r"(?i)(buyer'?s?\s*club|member\s*price|club\s*price)[^$]{0,30}\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?|[0-9]+(?:\.\d{2})?)", visible_text)
            for match in matches:
                parsed = Price.fromstring(match.group(2))
                if parsed.amount_float is not None and 0.00 < parsed.amount_float <= 100000:
                    add_candidate(parsed.amount_float, "Strict Visible Text Label")
        except Exception:
            pass

        # ── EXTRACT REGULAR PRICE ONLY FOR THE SUMMARY LOG ──
        reg_price_str = "Not Found"
        try:
            reg_el = search_soup.select_one('.non-member-price, .price-retail, .regular-price, .regularprice, .price')
            if reg_el:
                m = re.search(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?|[0-9]+(?:\.\d{2})?)", reg_el.get_text(" ", strip=True))
                if m: reg_price_str = f"${float(m.group(1).replace(',', '')):.2f}"
            if reg_price_str == "Not Found":
                m2 = re.search(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?|[0-9]+(?:\.\d{2})?)", visible_text)
                if m2: reg_price_str = f"${float(m2.group(1).replace(',', '')):.2f}"
        except Exception:
            pass

        # Final Evaluation & Logging
        if candidates:
            # All candidates are Club Prices. Pick the first one safely discovered.
            best_candidate = candidates[0]
            
            if self.log:
                summary = (
                    "\n------------------------------------\n"
                    f"Buyer Club Price : {best_candidate['price']}\n"
                    f"Regular Price    : {reg_price_str}\n"
                    f"Selected Price   : {best_candidate['price']}\n"
                    f"Selection Reason : Buyer Club has higher priority\n"
                    "------------------------------------"
                )
                self.log(summary, "info")
            
            return best_candidate["price"]

        # No Club price found -> Enforce Rule 4
        if self.log:
            summary = (
                "\n------------------------------------\n"
                f"Buyer Club Price : Not Found\n"
                f"Regular Price    : {reg_price_str}\n"
                f"Selected Price   : \n"
                f"Selection Reason : Only Buyer Club is permitted. Returning empty string.\n"
                "------------------------------------"
            )
            self.log(summary, "warning")
            
        return ""

    # ── HIDDEN CART PRICING ──────────────────────────────────────────────────

    def check_if_hidden_price(self, soup: BeautifulSoup, visible_text: str, driver=None) -> bool:
        """
        Determines if the price is hidden and requires adding to cart to view.
        """
        indicators = [
            "see member price in checkout",
            "add to cart to see price",
            "price hidden",
            "too low to show",
            "see price in cart",
            "log in for price"
        ]
        
        # 1. Quick text check
        low_text = visible_text.lower()
        if any(ind in low_text for ind in indicators):
            return True
            
        # 2. Check specific SG classes
        hidden_els = soup.select(".hidden-price, .see-price-in-cart, .price-login-required")
        if hidden_els:
            return True

        # 3. Check dynamically via driver if available
        if driver:
            try:
                js_hidden = driver.execute_script("""
                    var text = document.body.innerText.toLowerCase();
                    return text.includes('see member price in checkout') || 
                           text.includes('add to cart to see price') ||
                           text.includes('too low to show');
                """)
                if js_hidden:
                    return True
            except Exception:
                pass
                
        return False

    # ── STOCK EVALUATION (FALLBACK FOR API) ──────────────────────────────────

    def parse_stock_fixed(self, soup: BeautifulSoup, visible_text: str, price: str) -> str:
        """
        Final safety net evaluation for stock status using strict Buy Box isolation.
        """
        buy_box = None
        
        # 1. Expand the Buy Box locator
        # Find Add To Cart elements first to build our anchor
        atc_elements = soup.select(
            "button.add-to-cart, button#add-to-cart, "
            ".btn-add-to-cart, [data-action='add-to-cart'], "
            "input[value*='Cart'], button.ad-AddToCart, .ad-Button--addToCart"
        )
        
        # If CSS selectors miss it, find by text
        if not atc_elements:
            for btn in soup.find_all(['button', 'a', 'input']):
                txt = btn.get_text(strip=True).lower()
                if btn.name == 'input':
                    txt = str(btn.get('value', '')).lower()
                if 'add to cart' in txt or 'add to bag' in txt:
                    atc_elements.append(btn)

        # Traverse up from ATC to find the tightest container with Qty and Club Price
        for atc_el in atc_elements:
            parent = atc_el.parent
            while parent and parent.name not in ['body', 'html']:
                parent_text = parent.get_text(" ", strip=True).lower()
                
                # Check for Quantity selector
                has_qty = bool(parent.select_one("input[name*='qty' i], input[id*='qty' i], select[name*='qty' i], select[id*='qty' i], .qty, .quantity"))
                
                # Check for Club Price
                has_club = any(c in parent_text for c in ["buyer's club", "buyers club", "member price", "club price"]) or bool(parent.select_one(".club-price, .price-buyer-club, .member-price"))
                
                if has_qty and has_club:
                    buy_box = parent
                    break
                parent = parent.parent
            if buy_box:
                break

        # Fallback to standard wrappers if strict search fails
        if not buy_box:
            for sel in [".ad-ProductDetails", ".product-buy-box", ".buy-box", ".product-shop", ".pdp-right"]:
                candidate = soup.select_one(sel)
                if candidate:
                    buy_box = candidate
                    break
                    
        # Ultimate fallback
        if not buy_box:
            buy_box = soup.find("main") or soup

        if self.log:
            self.log(f"SG: Buy Box found (Tag: {buy_box.name})", "info")

        bb_text = buy_box.get_text(" ", strip=True).lower()

        # 2. Perform ALL stock checks ONLY inside that container.
        atc_found = False
        atc_enabled = False
        
        for btn in buy_box.find_all(['button', 'input', 'a']):
            txt = btn.get_text(strip=True).lower()
            if btn.name == 'input':
                txt = str(btn.get('value', '')).lower()
            if btn.has_attr('title'):
                txt += " " + str(btn.get('title', '')).lower()
                
            if 'add to cart' in txt or 'add to bag' in txt:
                atc_found = True
                disabled_attr = btn.has_attr('disabled')
                disabled_class = 'disabled' in str(btn.get('class', [])).lower()
                if not disabled_attr and not disabled_class:
                    atc_enabled = True
                    break

        if self.log:
            self.log(f"SG: Add To Cart found: {atc_found} (Enabled: {atc_enabled})", "info")

        # Quantity selector verification logging
        qty_found = bool(buy_box.select_one("input[name*='qty' i], input[id*='qty' i], select[name*='qty' i], select[id*='qty' i], .qty, .quantity"))
        if self.log:
            self.log(f"SG: Quantity selector found: {qty_found}", "info")

        if self.log:
            snippet = bb_text[:150].replace('\n', ' ')
            self.log(f"SG: Availability text: {snippet}...", "info")

        # 4. Check Override Phrases (OOS overrides everything)
        oos_phrases = [
            "out of stock", "sold out", "currently unavailable", "no longer available", 
            "discontinued", "notify me", "backorder", "backordered", "item is on backorder", 
            "on backorder", "available for backorder", "preorder", "pre-order", 
            "available for preorder", "ships when available", "temporarily unavailable", 
            "expected availability", "estimated ship date", "usually ships in", 
            "ships in", "available to ship on"
        ]
        
        for phrase in oos_phrases:
            if phrase in bb_text:
                if self.log:
                    self.log(f"SG: Final stock decision: OOS (Override phrase found: '{phrase}')", "info")
                return "OOS"

        # 3. If the Buy Box contains BOTH enabled Add To Cart AND In Stock -> In Stock
        if atc_enabled and "in stock" in bb_text:
            if self.log:
                self.log("SG: Final stock decision: In Stock (ATC enabled + In Stock text)", "info")
            return "In Stock"

        # Fallback if ATC is enabled but "in stock" text is missing
        if atc_enabled:
            if self.log:
                self.log("SG: Final stock decision: In Stock (ATC enabled)", "info")
            return "In Stock"

        if self.log:
            self.log("SG: Final stock decision: OOS (Default)", "info")
        return "OOS"

    # ── UTILITIES ────────────────────────────────────────────────────────────

    def _extract_price_from_json(self, data: dict) -> Optional[float]:
        """Recursively search a parsed JSON object for price fields."""
        price_keys = ['price', 'salePrice', 'clubPrice', 'memberPrice', 'amount']
        
        if isinstance(data, dict):
            for k, v in data.items():
                if k.lower() in [pk.lower() for pk in price_keys] and isinstance(v, (int, float)):
                    if 0.00 <= float(v) <= 100000:
                        return float(v)
                res = self._extract_price_from_json(v)
                if res is not None:
                    return res
        elif isinstance(data, list):
            for item in data:
                res = self._extract_price_from_json(item)
                if res is not None:
                    return res
        return None