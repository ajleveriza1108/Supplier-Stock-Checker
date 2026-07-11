import re
import json
import time
from bs4 import BeautifulSoup
from typing import Tuple, Optional, List, Dict, Any
from selenium import webdriver


class CEPriceParser:
    """
    Price and stock parser for CollectionsEtc.com (Shopify platform).
    Full unreduced version - all original logic preserved.
    """

    HIDDEN_SENTINEL = "See Member Price in Checkout"

    def __init__(self, logger_func=None):
        self.log = logger_func

    def _format_price(self, raw) -> str:
        if raw is None:
            return ""
        try:
            val = float(str(raw).replace(",", "").strip())
        except (ValueError, TypeError):
            return ""

        if val >= 100 and val == int(val) and "." not in str(raw):
            val = val / 100.0

        if val <= 0 or val > 50_000:
            return ""
        return f"${val:.2f}"

    def _clean_price_str(self, text: str) -> str:
        if not text:
            return ""
        clean = re.sub(
            r"(?i)(?:save|savings|off\b|less\b|discount\s*:?)[^\$]{0,25}\$[\s]*[0-9,]+\.\d{2}",
            "", text
        )
        clean = re.sub(r"(?i)(?:\d+-Pay)[^$]{0,30}\$[\s]*[0-9,]+\.\d{2}", "", clean)
        clean = re.sub(
            r"(?i)(?:payments?|mo\.|month|trial|interest-free|sezzle|afterpay|zip)[^$]{0,30}\$[\s]*[0-9,]+\.\d{2}",
            "", clean
        )
        clean = re.sub(r"(?i)(?:shipping|orders over)[^$]{0,30}\$[\s]*[0-9,]+\.\d{2}", "", clean)
        return clean

    def _extract_float(self, text: str) -> Optional[float]:
        if not text:
            return None
        clean = self._clean_price_str(text)
        m = re.search(r"\$?([0-9,]+\.\d{2})", clean)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except Exception:
                pass
        return None

    def _get_buy_box_text(self, driver: Optional[webdriver.Chrome], soup: BeautifulSoup) -> str:
        if driver:
            try:
                js_text = driver.execute_script("""
                    var sel = ".product-form, form[action*='/cart/add'], .product-detail, .product-options";
                    var c = document.querySelector(sel) || document.body;
                    return c.innerText || '';
                """)
                if js_text:
                    return re.sub(r"\s+", " ", js_text.lower())
            except Exception:
                pass
        selectors = (
            ".product-form, .add-to-cart-form, .buy-box, "
            ".product-options, .product-detail, "
            "form[action*='/cart/add'], .shopify-section-product-main"
        )
        area = soup.select_one(selectors) or soup
        return re.sub(r"\s+", " ", area.get_text(" ", strip=True).lower())

    def fetch_shopify_json(self, driver: Optional[webdriver.Chrome], product_url: str) -> Optional[Dict]:
        if not driver:
            return None

        base_url = product_url.split("?")[0].rstrip("/")
        json_url = base_url + ".json"
        original_url = driver.current_url

        try:
            driver.get(json_url)
            time.sleep(1.5)

            page_src = driver.page_source
            soup_tmp = BeautifulSoup(page_src, "html.parser")

            pre = soup_tmp.find("pre")
            if pre:
                text = pre.get_text(strip=True)
            else:
                body = soup_tmp.find("body")
                text = body.get_text(strip=True) if body else ""

            if not text or text.strip().startswith("<"):
                return None

            data = json.loads(text)
            product = data.get("product")

            if isinstance(product, dict) and product.get("variants"):
                if self.log:
                    self.log(f"CE: Shopify JSON API OK — {len(product['variants'])} variant(s).", "info")
                return product

        except Exception as e:
            if self.log:
                self.log(f"CE: Shopify JSON fetch failed: {e}", "warning")
        finally:
            try:
                if driver.current_url != original_url:
                    driver.get(original_url)
                    time.sleep(2.0)
            except Exception:
                pass

        return None

    def extract_embedded_shopify_data(self, soup: BeautifulSoup) -> Optional[Dict]:
        scripts = soup.find_all("script")
        for script in scripts:
            if not script.string:
                continue
            src = script.string

            m = re.search(r'ShopifyAnalytics\.meta\s*=\s*(\{.*?\});', src, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(1))
                    prod = obj.get("product") or obj
                    if isinstance(prod, dict) and prod.get("variants"):
                        return prod
                except Exception:
                    pass

            m = re.search(r'window\.meta\s*=\s*(\{.*?\});', src, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(1))
                    prod = obj.get("product") or obj
                    if isinstance(prod, dict) and prod.get("variants"):
                        return prod
                except Exception:
                    pass

            for pat in [
                r'var\s+product\s*=\s*(\{.*?\});',
                r'window\.product\s*=\s*(\{.*?\});'
            ]:
                m = re.search(pat, src, re.DOTALL)
                if m:
                    try:
                        obj = json.loads(m.group(1))
                        if isinstance(obj, dict) and obj.get("variants"):
                            return obj
                    except Exception:
                        pass

            if '"variants"' in src and '"available"' in src:
                m = re.search(r'"variants"\s*:\s*(\[.*?\])\s*[,}]', src, re.DOTALL)
                if m:
                    try:
                        variants = json.loads(m.group(1))
                        if isinstance(variants, list) and len(variants) > 0:
                            return {"variants": variants}
                    except Exception:
                        pass

        for script in soup.find_all("script", {"type": "application/json"}):
            sid = script.get("id", "")
            if "ProductJson" in sid or "product-json" in sid.lower() or "variant" in sid.lower():
                try:
                    obj = json.loads(script.string or "")
                    if isinstance(obj, dict) and obj.get("variants"):
                        return obj
                except Exception:
                    pass

        for script in soup.find_all("script", {"type": "application/json"}):
            try:
                obj = json.loads(script.string or "")
                if isinstance(obj, dict) and obj.get("variants"):
                    return obj
            except Exception:
                pass

        return None

    def match_variant(self, variants: List[Dict], target_variation: Optional[str]) -> Optional[Dict]:
        if not variants:
            return None

        if not target_variation or not target_variation.strip():
            available = [v for v in variants if v.get("available", True)]
            return available[0] if available else variants[0]

        t_norm = re.sub(r"\s+", " ", target_variation.lower().strip())
        t_tokens = set(re.findall(r"[a-z0-9]+", t_norm))

        tier_results: Dict[int, Optional[Dict]] = {1: None, 2: None, 3: None, 4: None, 5: None}

        for v in variants:
            title = str(v.get("title", "")).lower().strip()
            opt1 = str(v.get("option1", "")).lower().strip()
            opt2 = str(v.get("option2", "")).lower().strip()
            opt3 = str(v.get("option3", "")).lower().strip()
            combined = re.sub(r"\s+", " ", " ".join(filter(None, [title, opt1, opt2, opt3]))).strip()
            c_tokens = set(re.findall(r"[a-z0-9]+", combined))

            if t_norm == title or t_norm == combined:
                tier_results[1] = v
                break
            if t_norm in combined and not tier_results[2]:
                tier_results[2] = v
            elif combined in t_norm and not tier_results[3]:
                tier_results[3] = v
            elif t_tokens and t_tokens.issubset(c_tokens) and not tier_results[4]:
                tier_results[4] = v
            elif t_tokens and t_tokens.intersection(c_tokens) and not tier_results[5]:
                tier_results[5] = v

        for tier in sorted(tier_results.keys()):
            if tier_results[tier] is not None:
                matched = tier_results[tier]
                if self.log:
                    self.log(
                        f"CE variant match TIER-{tier}: "
                        f"target='{target_variation}' → '{matched.get('title', '?')}'",
                        "info"
                    )
                return matched

        if self.log:
            self.log(
                f"CE: No variant match for '{target_variation}'. "
                f"Available titles: {[v.get('title') for v in variants[:8]]}",
                "warning"
            )
        return None

    def parse_price_from_variant(self, variant: Dict) -> str:
        for key in ("price", "compare_at_price", "presentment_price"):
            raw = variant.get(key)
            if raw is not None:
                formatted = self._format_price(raw)
                if formatted:
                    return formatted
        return ""

    def parse_price_from_html(self, driver: Optional[webdriver.Chrome], soup: BeautifulSoup, page_text: str) -> str:
        if driver:
            try:
                js_price = driver.execute_script("""
                    function isVis(el) {
                        if (!el) return false;
                        var s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                    }
                    var PRICE_SEL = [
                        '.price__current', '.price-item--sale', '.price-item--regular',
                        '.price__sale', '.product__price', '[class*="product-price"]',
                        '.price', '[data-product-price]', '.current-price',
                        '[itemprop="price"]', 'span.money'
                    ];
                    var container = document.querySelector(
                        'form[action*="/cart/add"], .product-form, .product__info-wrapper'
                    ) || document;
                    for (var i = 0; i < PRICE_SEL.length; i++) {
                        var el = container.querySelector(PRICE_SEL[i]);
                        if (el && isVis(el)) {
                            var t = el.innerText || el.getAttribute('content') || '';
                            if (t.trim()) return t.trim();
                        }
                    }
                    return '';
                """)
                if js_price:
                    val = self._extract_float(js_price)
                    if val and 0 < val <= 50_000:
                        return f"${val:.2f}"
            except Exception:
                pass

        buy_box = soup.select_one(
            "form[action*='/cart/add'], .product-form, .product__info-wrapper, .product-single__meta"
        )
        area = buy_box if buy_box else soup

        for sel in [
            ".price__current", ".price-item--sale", ".price-item--regular",
            ".price__sale", ".product__price", "[class*='product-price']",
            ".price", "[data-product-price]", ".current-price", "span.money"
        ]:
            el = area.select_one(sel)
            if el:
                txt = el.get("content", "") or el.get_text(" ", strip=True)
                val = self._extract_float(txt)
                if val and 0 < val <= 50_000:
                    return f"${val:.2f}"

        clean = self._clean_price_str(page_text)
        prices = re.findall(r"\$\s*([0-9,]+\.\d{2})", clean)
        valid = [float(p.replace(",", "")) for p in prices if 0 < float(p.replace(",", "")) <= 50_000]
        if valid:
            return f"${min(valid):.2f}"

        return ""

    def parse_stock_from_variant(self, variant: Dict) -> str:
        available = variant.get("available")
        if available is False:
            return "OOS"
        if available is True:
            return "In Stock"
        qty = variant.get("inventory_quantity")
        if qty is not None:
            return "In Stock" if qty > 0 else "OOS"
        return "In Stock"

    def parse_stock_from_html(self, soup: BeautifulSoup, page_text: str, driver: Optional[webdriver.Chrome] = None) -> str:
        tl = page_text.lower()

        hard_oos = [
            "sold out", "out of stock",
            "this item is currently out of stock",
            "currently unavailable", "no longer available",
            "item is not available",
        ]
        for phrase in hard_oos:
            if phrase in tl:
                if self.log:
                    self.log(f"CE Stock OOS: '{phrase}' in page text", "oos")
                return "OOS"

        if driver:
            try:
                js_stock = driver.execute_script("""
                    var atcBtn = document.querySelector(
                        '[name="add"], button[type="submit"][id*="add"], '
                        + '.product-form__cart-submit, [data-add-to-cart], '
                        + 'button[class*="add-to-cart"]'
                    );
                    if (!atcBtn) return 'NO_BUTTON';
                    if (atcBtn.disabled || atcBtn.getAttribute('aria-disabled') === 'true') return 'DISABLED';
                    var txt = (atcBtn.innerText || '').toUpperCase().trim();
                    if (txt.indexOf('SOLD OUT') > -1 || txt.indexOf('UNAVAILABLE') > -1
                        || txt.indexOf('OUT OF STOCK') > -1) return 'SOLD_OUT';
                    if (txt.indexOf('ADD TO CART') > -1 || txt.indexOf('ADD TO BAG') > -1
                        || txt.indexOf('BUY NOW') > -1) return 'IN_STOCK';
                    return 'UNKNOWN';
                """)
                if js_stock in ("DISABLED", "SOLD_OUT", "NO_BUTTON"):
                    return "OOS"
                if js_stock == "IN_STOCK":
                    return "In Stock"
            except Exception:
                pass

        buy_box = soup.select_one(
            "form[action*='/cart/add'], .product-form, .product__info-wrapper"
        ) or soup

        for btn in buy_box.find_all(["button", "input"]):
            cls = " ".join(btn.get("class", [])).lower()
            txt = btn.get_text(" ", strip=True).lower()
            val_attr = (btn.get("value") or "").lower()
            combined = txt + " " + val_attr + " " + cls

            if not any(x in combined for x in ["add", "cart", "buy", "purchase"]):
                continue
            if btn.has_attr("disabled") or "sold-out" in cls or "disabled" in cls:
                return "OOS"
            if any(x in combined for x in ["sold out", "unavailable", "out of stock"]):
                return "OOS"
            return "In Stock"

        if "add to cart" in tl or "add to bag" in tl:
            return "In Stock"

        return "OOS"

    def check_if_hidden_price(self, driver: Optional[webdriver.Chrome], soup: BeautifulSoup) -> bool:
        tier1 = [
            "see member price in checkout",
            "see price in checkout",
            "too low to show",
            "add to cart to see price",
            "add to cart for price",
            "log in to see price",
        ]
        clean_text = self._get_buy_box_text(driver, soup)
        for phrase in tier1:
            if phrase in clean_text:
                if self.log:
                    self.log(f"CE Hidden price: '{phrase}'", "price")
                return True
        return False

    def evaluate_from_variant(self, variant: Dict, driver: Optional[webdriver.Chrome] = None, soup: Optional[BeautifulSoup] = None, page_text: str = "") -> Tuple[str, str]:
        stock = self.parse_stock_from_variant(variant)
        if stock == "OOS":
            return "", "OOS"

        price = self.parse_price_from_variant(variant)

        if not price and driver and soup is not None:
            price = self.parse_price_from_html(driver, soup, page_text)

        return price, stock

    def evaluate_from_html(self, driver: Optional[webdriver.Chrome], soup: BeautifulSoup, page_text: str, url: str = "", is_hidden_price: bool = False) -> Tuple[str, str]:
        if is_hidden_price:
            return self.HIDDEN_SENTINEL, "In Stock"

        stock = self.parse_stock_from_html(soup, page_text, driver=driver)
        if stock == "OOS":
            return "", "OOS"

        price = self.parse_price_from_html(driver, soup, page_text)
        return price, stock