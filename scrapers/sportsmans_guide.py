import sys
import os
import time
import re
import json
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
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager

try:
    import undetected_chrome as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False

from core.base_scraper import BaseScraper
from scrapers.sg_price_parser import SGPriceParser

# --- GLOBAL DEBUG CONTROL ---
DEBUG_BUYBOX = False


class SportsmansGuideScraper(BaseScraper):
    """
    SportsmansGuide.com Scraper
    Features Punctuation-Agnostic Fuzzy Matching and AI Integration to bypass variation text formatting issues.
    Includes Smart DOM Parsing, React-Native Variation Clicks, and Anti-False OOS logic.
    """

    def __init__(self, browser_manager=None, logger_func=None, use_physical_browser=False):
        super().__init__(browser_manager, logger_func)
        self.parser = SGPriceParser(logger_func=logger_func)
        self.use_physical_browser = use_physical_browser and browser_manager is not None
        self.cloudscraper = cloudscraper.create_scraper() if HAS_CLOUDSCRAPER else None
        
        if self.log and DEBUG_BUYBOX:
            mode = "Physical Browser" if self.use_physical_browser else "Selenium"
            self.log(f"SG: Browser mode = {mode}", "info")

    def _quick_http_probe(self, url: str) -> str:
        if not self.cloudscraper:
            return ""
        try:
            response = self.cloudscraper.get(
                url,
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.sportsmansguide.com/",
                }
            )
            if response.status_code == 200:
                return response.text
        except Exception:
            pass
        return ""

    def _load_page_with_browser(self, url: str, driver: webdriver.Chrome) -> Tuple[BeautifulSoup, str, str]:
        driver.set_page_load_timeout(45)
        driver.get(url)

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            pass

        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
            )
        except TimeoutException:
            pass

        try:
            WebDriverWait(driver, 8).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, ".price, .ad-Price, [class*='price'], [data-testid*='price'], .clubprice, .club-price") or \
                          "the page you requested could not be found" in d.page_source.lower()
            )
        except TimeoutException:
            pass

        time.sleep(4)

        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
            time.sleep(1.2)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.8)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.5)
        except Exception:
            pass

        raw_html = driver.page_source
        soup = BeautifulSoup(raw_html, "html.parser")
        visible_text = soup.get_text(" ", strip=True)
        
        return soup, visible_text, raw_html

    def _inject_and_score_buy_box(self, driver, logs, silent=False):
        """
        Dynamically executes JavaScript to evaluate ALL containers on the page.
        Scores them based on Add To Cart, Qty, Club Price, and Stock text presence.
        Assigns the winning container the ID 'sg-primary-buy-box' for seamless Selenium/Soup sync.
        """
        js_script = r"""
        function scoreSGContainers() {
            let candidates = document.querySelectorAll('div, form, main, section, article');
            let toxic = ['related', 'recommend', 'accessory', 'accessories', 'footer', 'sidebar', 'header', 'nav', 'carousel', 'similar', 'modal', 'popup', 'setup'];
            let scored = [];
            
            for (let i = 0; i < candidates.length; i++) {
                let el = candidates[i];
                let cls = (el.className || '').toLowerCase();
                let id = (el.id || '').toLowerCase();
                
                if (toxic.some(t => cls.includes(t) || id.includes(t))) continue;
                
                let text = el.innerText || '';
                let lowerText = text.toLowerCase();
                if (!lowerText.trim()) continue;
                
                let score = 0;
                let reason = [];
                
                if (el.querySelectorAll('button, a, input').length > 0) {
                    let btns = el.querySelectorAll('button, a, input');
                    let hasATC = false;
                    for (let j = 0; j < btns.length; j++) {
                        let bt = (btns[j].textContent || btns[j].value || btns[j].title || btns[j].ariaLabel || '').toLowerCase();
                        if (bt.includes('add to cart') || bt.includes('add to bag') || bt.includes('add to basket')) {
                            hasATC = true; break;
                        }
                    }
                    if (hasATC) { score += 40; }
                }
                
                if (el.querySelector('input[name*="qty" i], input[id*="qty" i], select[name*="qty" i], select[id*="qty" i], .qty, .quantity')) { score += 25; }
                if (lowerText.includes("buyer's club") || lowerText.includes("buyers club") || lowerText.includes("member price") || lowerText.includes("club price") || el.querySelector('.club-price, .price-buyer-club, .member-price')) { score += 20; }
                if (lowerText.match(/in stock|item currently sold out|out of stock|sold out|currently unavailable|no longer available|backorder|preorder|notify me|temporarily unavailable/i)) { score += 15; }
                if (lowerText.match(/\$\s*\d+\.\d{2}/)) { score += 10; }
                
                if (score > 0) {
                    scored.push({ element: el, score: score, length: text.length, tag: el.tagName.toLowerCase(), id: id, cls: cls.substring(0, 30) });
                }
            }
            
            if (scored.length === 0) return null;
            
            scored.sort((a, b) => {
                if (b.score !== a.score) return b.score - a.score;
                return a.length - b.length;
            });
            
            let old = document.querySelectorAll('#sg-primary-buy-box');
            for(let k=0; k<old.length; k++) { old[k].removeAttribute('id'); }
            
            let best = scored[0];
            best.element.setAttribute('id', 'sg-primary-buy-box');
            
            return scored.map(s => ({ tag: s.tag, id: s.id, cls: s.cls, score: s.score, length: s.length }));
        }
        return scoreSGContainers();
        """
        try:
            results = driver.execute_script(js_script)
            if results and not silent and DEBUG_BUYBOX:
                self._write_log(logs, f"SG: Generic Buy Box discovery executed. Number of candidate Buy Boxes: {len(results)}", "info")
                for i, c in enumerate(results[:5]):
                    name = f"{c['tag']}#{c['id']}" if c['id'] else f"{c['tag']}.{c['cls']}"
                    self._write_log(logs, f"SG: Score of candidate [{name}]: {c['score']} (Len: {c['length']})", "info")
                
                best = results[0]
                self._write_log(logs, f"SG: Selected PRIMARY BUY BOX score: {best['score']}", "info")
            return True
        except Exception:
            return False

    def _aggressive_price_hunt(self, soup, raw_html: str) -> str:
        selectors = [
            '.price-buyer', '.club-price', '.buyer-club-price', '.price-club',
            '.non-member-price', '.price-non-member', '.price-retail',
            '[itemprop="price"]', '.price-amount', '.price', '.ad-Price', '.ad-Price-amount'
        ]
        for sel in selectors:
            for el in soup.select(sel):
                txt = el.get_text(" ", strip=True)
                m = re.search(r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)', txt)
                if m:
                    val = float(m.group(1).replace(',', ''))
                    if val > 0:
                        return f"${val:.2f}"

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

        meta_price = soup.find("meta", {"itemprop": "price"}) or soup.find("meta", {"property": "product:price:amount"})
        if meta_price and meta_price.get("content"):
            try:
                val = float(meta_price.get("content").replace(',', ''))
                if val > 0:
                    return f"${val:.2f}"
            except Exception:
                pass

        bc_match = re.search(r'(?i)Buyer\'s Club.{0,150}?\$(\d+(?:,\d{3})*(?:\.\d{2})?)', raw_html)
        if bc_match:
            val = float(bc_match.group(1).replace(',', ''))
            if val > 0: return f"${val:.2f}"

        reg_match = re.search(r'(?i)(?:Non-Member|Price).{0,150}?\$(\d+(?:,\d{3})*(?:\.\d{2})?)', raw_html)
        if reg_match:
            val = float(reg_match.group(1).replace(',', ''))
            if val > 0: return f"${val:.2f}"

        return ""

    def scrape(self, url: str, target_variation: str = "") -> Tuple[str, str, str, str, List[Dict], List]:
        logs = []
        status = "Success"
        price = ""
        stock = "OOS"
        title = ""
        variants = []
        driver = None
        is_standalone_driver = False
        variation_not_found = False

        try:
            probe_html = self._quick_http_probe(url)
            
            if self.use_physical_browser and self.browser_manager:
                driver = self.browser_manager.get_driver()
            else:
                driver = self._get_driver_with_stealth()
                is_standalone_driver = True

            if not driver:
                raise RuntimeError("Failed to initialize any browser driver")

            soup, visible_text, raw_html = self._load_page_with_browser(url, driver)
            lower_text = visible_text.lower()

            if "the page you requested could not be found" in lower_text or "404" in lower_text:
                if self.log:
                    self.log("SG: 404 Page Not Found detected -> OOS", "oos")
                return "Success", "", "OOS", "Unknown Product (404)", [{"label": target_variation or "Default", "price": "", "stock": "OOS"}], logs

            if self._has_captcha_element(raw_html):
                raise RuntimeError("CAPTCHA detected")

            h1 = soup.select_one("h1[class*='product'], h1, .product-title")
            title = h1.get_text(" ", strip=True) if h1 else "Unknown"

            is_hidden = self.parser.check_if_hidden_price(soup, visible_text, driver=None)

            # --- VARIATION HANDLING FIRST ---
            if target_variation and target_variation.strip() and target_variation.lower() != "default":
                try:
                    target_norm = re.sub(r'[^a-z0-9]', '', target_variation.lower())
                    
                    for attempt in range(2):
                        previous_price = self._aggressive_price_hunt(soup, raw_html)

                        if self._click_variation(driver, target_variation):
                            start_time = time.time()
                            stabilized = False
                            
                            while time.time() - start_time < 12.0:
                                current_raw_html = driver.page_source
                                current_soup = BeautifulSoup(current_raw_html, "html.parser")
                                current_price = self._aggressive_price_hunt(current_soup, current_raw_html)
                                
                                if current_price and current_price != previous_price:
                                    time.sleep(0.3)
                                    check_raw_html = driver.page_source
                                    check_soup = BeautifulSoup(check_raw_html, "html.parser")
                                    check_price = self._aggressive_price_hunt(check_soup, check_raw_html)
                                    
                                    if current_price == check_price:
                                        stabilized = True
                                        break
                                
                                time.sleep(0.25)
                            
                            is_match = False
                            try:
                                active_elements = driver.find_elements(By.CSS_SELECTOR, ".selected, .active, option:checked, [aria-selected='true']")
                                for el in active_elements:
                                    txt = (el.get_attribute("textContent") or el.get_attribute("innerText") or el.get_attribute("value") or "").strip()
                                    if txt and target_norm == re.sub(r'[^a-z0-9]', '', txt.lower()):
                                        is_match = True
                                        break
                            except Exception:
                                pass

                            if is_match:
                                break
                            else:
                                if attempt == 1:
                                    variation_not_found = True
                        else:
                            variation_not_found = True
                            break
                except Exception:
                    variation_not_found = True

            # --- RUN GENERIC BUY BOX DISCOVERY ---
            self._inject_and_score_buy_box(driver, logs, silent=False)
            
            raw_html = driver.page_source
            soup = BeautifulSoup(raw_html, "html.parser")

            # --- PRODUCT STATE VERIFICATION ---
            def capture_product_state():
                self._inject_and_score_buy_box(driver, logs, silent=True)
                html = driver.page_source
                temp_soup = BeautifulSoup(html, "html.parser")
                
                bb = temp_soup.find(id="sg-primary-buy-box")
                if not bb:
                    bb = temp_soup.find("main") or temp_soup
                
                state = {
                    "club_price": " ".join([el.get_text(strip=True) for el in bb.select(".club-price, .price-buyer-club, .member-price, .clubprice")]),
                    "reg_price": " ".join([el.get_text(strip=True) for el in bb.select(".non-member-price, .price-retail, .regular-price, .regularprice")]),
                    "sel_price": self._aggressive_price_hunt(bb, str(bb)),
                    "avail_text": bb.get_text(" ", strip=True),
                    "url": driver.current_url,
                    "btn_state": "",
                    "variation": ""
                }
                
                try:
                    bb_sel = None
                    try:
                        bb_sel = driver.find_element(By.ID, 'sg-primary-buy-box')
                    except Exception:
                        pass
                    ctx = bb_sel if bb_sel else driver
                    
                    btns = ctx.find_elements(By.CSS_SELECTOR, "button, input[type='button'], input[type='submit'], a.add-to-cart")
                    state["btn_state"] = "|".join([f"{b.is_enabled()}-{b.is_displayed()}" for b in btns])
                except Exception:
                    pass
                
                try:
                    active = driver.find_elements(By.CSS_SELECTOR, ".selected, .active, option:checked, [aria-selected='true']")
                    state["variation"] = "|".join([(e.get_attribute("textContent") or "").strip() for e in active])
                except Exception:
                    pass
                    
                return state, html, temp_soup

            final_html = raw_html
            final_soup = soup
            for attempt in range(3):
                state_1, _, _ = capture_product_state()
                time.sleep(0.3)
                state_2, final_html, final_soup = capture_product_state()
                if state_1 == state_2:
                    break

            raw_html = final_html
            soup = final_soup
            visible_text = soup.get_text(" ", strip=True)
            lower_text = visible_text.lower()
            is_hidden = self.parser.check_if_hidden_price(soup, visible_text, driver=driver)

            buy_box = soup.find(id="sg-primary-buy-box")
            if not buy_box:
                buy_box = soup.find("main") or soup
                
            bb_text = buy_box.get_text(" ", strip=True).lower() if buy_box else ""

            # --- INITIAL PRICE PARSING ---
            price = self.parser.parse_price(buy_box, str(buy_box), url, is_hidden_price=is_hidden, driver=driver)

            if "see member price" in bb_text or "see member price" in lower_text or "see member price" in str(price).lower():
                price = "See Member Price in Checkout"
            elif not price or price.strip() == "" or price == "N/A" or price == "$0.00":
                fallback_price = self._aggressive_price_hunt(buy_box, str(buy_box))
                if fallback_price:
                    price = fallback_price
                    is_hidden = False 

            if is_hidden and not price:
                price = ""

            # --- STRICT STOCK VALIDATION ---
            is_definitively_oos = False
            
            try:
                bb_sel = driver.find_element(By.ID, 'sg-primary-buy-box')
                bb_visible_text = bb_sel.text.lower()
            except Exception:
                bb_visible_text = bb_text

            if bb_visible_text:
                # Highly specific strict phrases derived directly from explicit rules
                oos_phrases = [
                    "item currently sold out",
                    "item is on backorder",
                    "backordered item ships",
                    "backorder",
                    "out of stock"
                ]
                for phrase in oos_phrases:
                    if phrase in bb_visible_text:
                        is_definitively_oos = True
                        if "backorder" in phrase:
                            self._write_log(logs, "SG: Backorder detected", "info")
                        else:
                            self._write_log(logs, "SG: OOS detected", "info")
                        break

            is_explicitly_in_stock = False
            if "in stock" in bb_visible_text and not is_definitively_oos:
                is_explicitly_in_stock = True
            
            is_btn_disabled = True
            try:
                search_context = driver
                bbs = driver.find_elements(By.ID, 'sg-primary-buy-box')
                if bbs:
                    search_context = bbs[0]
                
                cart_btns = search_context.find_elements(By.CSS_SELECTOR, "button, input[type='button'], input[type='submit'], a.add-to-cart")
                for btn in cart_btns:
                    btn_txt = (btn.get_attribute("textContent") or btn.get_attribute("value") or btn.get_attribute("title") or btn.get_attribute("aria-label") or "").lower()
                    
                    if "preorder" in btn_txt:
                        self._write_log(logs, "SG: Preorder button detected.", "info")
                        is_definitively_oos = True
                        break

                    if any(phrase in btn_txt for phrase in ["add to cart", "add to bag", "add to basket"]):
                        if btn.is_displayed() and btn.is_enabled():
                            if "disabled" not in (btn.get_attribute("class") or "").lower() and not btn.get_attribute("disabled"):
                                is_btn_disabled = False
                                break
            except Exception:
                is_btn_disabled = True

            if is_explicitly_in_stock and not is_definitively_oos:
                is_btn_disabled = False

            stock = "In Stock"

            if is_definitively_oos:
                stock = "OOS"
                price = ""
            elif not is_btn_disabled:
                stock = "In Stock"
            else:
                stock = self.parser.parse_stock_fixed(soup, visible_text, price)

            if variation_not_found:
                stock = "OOS"
                price = ""
            elif stock == "OOS" or is_definitively_oos or price == "$0.00" or price == "0.00":
                stock = "OOS"
                price = ""
            elif stock == "In Stock" and not str(price).strip():
                stock = "OOS"
                price = ""

            # --- FINAL VERIFICATION PASS ---
            price1 = price
            stock1 = stock

            time.sleep(2.0)
            
            self._inject_and_score_buy_box(driver, logs, silent=True)
            
            raw_html_v = driver.page_source
            soup_v = BeautifulSoup(raw_html_v, "html.parser")
            visible_text_v = soup_v.get_text(" ", strip=True)
            lower_text_v = visible_text_v.lower()
            is_hidden_v = self.parser.check_if_hidden_price(soup_v, visible_text_v, driver=driver)

            buy_box_v = soup_v.find(id="sg-primary-buy-box")
            if not buy_box_v:
                buy_box_v = soup_v.find("main") or soup_v
                
            bb_text_v = buy_box_v.get_text(" ", strip=True).lower() if buy_box_v else ""

            price_v = self.parser.parse_price(buy_box_v, str(buy_box_v), url, is_hidden_price=is_hidden_v, driver=driver)

            if "see member price" in bb_text_v or "see member price" in lower_text_v or "see member price" in str(price_v).lower():
                price_v = "See Member Price in Checkout"
            elif not price_v or price_v.strip() == "" or price_v == "N/A" or price_v == "$0.00":
                fallback_price_v = self._aggressive_price_hunt(buy_box_v, str(buy_box_v))
                if fallback_price_v:
                    price_v = fallback_price_v
                    is_hidden_v = False 

            if is_hidden_v and not price_v:
                price_v = ""

            is_definitively_oos_v = False
            
            try:
                bb_sel_v = driver.find_element(By.ID, 'sg-primary-buy-box')
                bb_visible_text_v = bb_sel_v.text.lower()
            except Exception:
                bb_visible_text_v = bb_text_v

            if bb_visible_text_v:
                oos_phrases = [
                    "item currently sold out",
                    "item is on backorder",
                    "backordered item ships",
                    "backorder",
                    "out of stock"
                ]
                for phrase in oos_phrases:
                    if phrase in bb_visible_text_v:
                        is_definitively_oos_v = True
                        break
            
            is_explicitly_in_stock_v = False
            if "in stock" in bb_visible_text_v and not is_definitively_oos_v:
                is_explicitly_in_stock_v = True
            
            is_btn_disabled_v = True
            try:
                search_context_v = driver
                bbs_v = driver.find_elements(By.ID, 'sg-primary-buy-box')
                if bbs_v:
                    search_context_v = bbs_v[0]
                
                cart_btns_v = search_context_v.find_elements(By.CSS_SELECTOR, "button, input[type='button'], input[type='submit'], a.add-to-cart")
                for btn in cart_btns_v:
                    btn_txt = (btn.get_attribute("textContent") or btn.get_attribute("value") or btn.get_attribute("title") or btn.get_attribute("aria-label") or "").lower()
                    if "preorder" in btn_txt:
                        is_definitively_oos_v = True
                        break
                    if any(phrase in btn_txt for phrase in ["add to cart", "add to bag", "add to basket"]):
                        if btn.is_displayed() and btn.is_enabled():
                            if "disabled" not in (btn.get_attribute("class") or "").lower() and not btn.get_attribute("disabled"):
                                is_btn_disabled_v = False
                                break
            except Exception:
                is_btn_disabled_v = True

            if is_explicitly_in_stock_v and not is_definitively_oos_v:
                is_btn_disabled_v = False

            stock_v = "In Stock"

            if is_definitively_oos_v:
                stock_v = "OOS"
                price_v = ""
            elif not is_btn_disabled_v:
                stock_v = "In Stock"
            else:
                stock_v = self.parser.parse_stock_fixed(soup_v, visible_text_v, price_v)
                
            if variation_not_found:
                stock_v = "OOS"
                price_v = ""
            elif stock_v == "OOS" or is_definitively_oos_v or price_v == "$0.00" or price_v == "0.00":
                stock_v = "OOS"
                price_v = ""
            elif stock_v == "In Stock" and not str(price_v).strip():
                stock_v = "OOS"
                price_v = ""

            if price1 == price_v and stock1 == stock_v:
                pass
            else:
                price = price_v
                stock = stock_v

            # --- CLEAN PRODUCTION LOGS ---
            if price and stock == "In Stock":
                self._write_log(logs, f"SG: Club Price = {price}", "info")
            
            self._write_log(logs, f"SG: Stock = {stock}", "info")

            variants = [{"label": target_variation or "Default", "price": price, "stock": stock}]
            
        except Exception as e:
            status = "Error"
            stock = "OOS"
            error_msg = f"SG error: {str(e)[:150]}"
            self._write_log(logs, error_msg, "error")
        
        finally:
            if is_standalone_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass

        return status, price, stock, title, variants, logs

    # --- HELPER METHODS ---

    def _write_log(self, logs, msg, tag):
        logs.append((msg, tag, None))
        if self.log: self.log(msg, tag)

    def _get_driver_with_stealth(self):
        try:
            if HAS_UC:
                try:
                    options = uc.ChromeOptions()
                    options.add_argument("--no-sandbox")
                    options.add_argument("--disable-dev-shm-usage")
                    options.add_argument("--disable-blink-features=AutomationControlled")
                    options.add_argument(
                        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    )
                    driver = uc.Chrome(options=options, version_main=None, suppress_welcome=True)
                    driver.set_page_load_timeout(45)
                    return driver
                except Exception:
                    pass
            
            options = Options()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--headless=new")
            options.add_argument(
                "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
            
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"},
            )
            
            driver.set_page_load_timeout(45)
            return driver
        
        except Exception:
            return None

    def _has_captcha_element(self, html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        
        if soup.find("iframe", {"src": re.compile(r"recaptcha|captcha", re.I)}):
            return True
        if soup.find("div", {"class": re.compile(r"recaptcha|g-recaptcha", re.I)}):
            return True
        if soup.find("div", {"id": "challenge-error-title"}):
            return True
        if soup.find("div", {"id": "px-captcha"}):
            return True
        return False

    def _click_variation(self, driver, target: str) -> bool:
        """
        Dynamically imports smart_tools to leverage AI and Token-Subset matching.
        Scans all swatches, maps them to Selenium elements, and mimics a human click.
        """
        if not target or not target.strip():
            return False

        try:
            sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from smart_tools import OllamaAI
            ai_matcher = OllamaAI()
        except ImportError:
            ai_matcher = None

        clicked = False

        # --- STEP 1: Gather all clickable swatches/options from the DOM ---
        try:
            elements = driver.find_elements(
                By.CSS_SELECTOR, 
                "a, button, label, li, div.swatch, div.attribute-value, span.radio-label, div[role='option'], div[class*='option'], option"
            )
        except Exception as e:
            if self.log and DEBUG_BUYBOX: self.log(f"SG: Could not locate variation elements - {e}", "warning")
            return False
            
        available_options = []
        element_map = {}

        for el in elements:
            try:
                # Get visible text or hidden value attribute
                txt = el.get_attribute("textContent") or el.get_attribute("innerText") or el.get_attribute("aria-label") or el.get_attribute("title") or el.get_attribute("data-value") or ""
                txt = txt.strip()
                
                # Filter out massive text blocks or empty strings
                if txt and 0 < len(txt) < 50:
                    if txt not in element_map:
                        available_options.append(txt)
                        element_map[txt] = el
            except Exception:
                continue

        if not available_options:
            if self.log and DEBUG_BUYBOX: self.log("SG: No valid variation buttons found on page.", "warning")
            return False

        matched_text = None

        # --- STEP 2: Use Smart Tools Matcher (Tokens + AI) ---
        if ai_matcher:
            matched_text = ai_matcher.match_variation(target, available_options)

        # --- STEP 3: Fallback Regex/Token Matcher (If AI fails or isn't loaded) ---
        if not matched_text:
            target_clean = re.sub(r'[^a-z0-9]', '', target.lower())
            
            for opt in available_options:
                opt_clean = re.sub(r'[^a-z0-9]', '', opt.lower())
                if target_clean == opt_clean:
                    matched_text = opt
                    break
                    
            if not matched_text:
                target_tokens = set(re.findall(r'[a-z0-9]+', target.lower()))
                best_match = None
                best_diff = float('inf')
                
                for opt in available_options:
                    opt_tokens = set(re.findall(r'[a-z0-9]+', opt.lower()))
                    if target_tokens.issubset(opt_tokens):
                        diff = len(opt_tokens) - len(target_tokens)
                        if diff < best_diff:
                            best_diff = diff
                            best_match = opt
                            
                if best_match:
                    matched_text = best_match

        # --- STEP 4: Execute the Click on the Exact Element (React-Safe) ---
        if matched_text and matched_text in element_map:
            target_element = element_map[matched_text]
            
            # Check if it's already selected to save time
            class_name = target_element.get_attribute("class") or ""
            if "selected" in class_name.lower() or "active" in class_name.lower():
                return True
                
            try:
                # Scroll element into the center of the viewport
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_element)
                time.sleep(0.5)
                
                if target_element.tag_name.lower() == 'option':
                    val = target_element.get_attribute("value")
                    parent_select = target_element.find_element(By.XPATH, "./..")
                    driver.execute_script("""
                        var sel = arguments[0];
                        var val = arguments[1];
                        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
                        if (nativeInputValueSetter) {
                            nativeInputValueSetter.call(sel, val);
                        } else {
                            sel.value = val;
                        }
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                    """, parent_select, val)
                    clicked = True
                else:
                    # Attempt 1: Native Selenium Action Chains (Looks like a real human to React)
                    try:
                        ActionChains(driver).move_to_element(target_element).click().perform()
                        clicked = True
                    except Exception:
                        # Attempt 2: Native Click
                        try:
                            target_element.click()
                            clicked = True
                        except Exception:
                            # Attempt 3: JavaScript Click Failsafe
                            driver.execute_script("arguments[0].click();", target_element)
                            clicked = True

                # Wait for React to process the click and remove spinners
                if clicked:
                    time.sleep(1) # Wait for spinner to appear
                    try:
                        WebDriverWait(driver, 5).until_not(
                            EC.presence_of_element_located((By.CSS_SELECTOR, ".loading-spinner, .overlay, .spinner"))
                        )
                    except TimeoutException:
                        pass
                
            except Exception as e:
                if self.log and DEBUG_BUYBOX: self.log(f"SG: Failed to execute variation click: {e}", "warning")
                
        return clicked