import threading
import queue
import time
import random
import sqlite3
import json
import os
import re
from typing import List, Dict, Callable, Any, Tuple, Optional
from datetime import datetime
from config.settings import SUPPLIER_ORDER, SHEET_COLS, SUPPLIER_LOG_FILES
from core.vpn_manager import SurfsharkVPN
from core.validator import SuspicionValidator
from core.engine_guard import EngineResultGuard

from smart_tools import AIMemory, OllamaAI

class ScrapingEngine:
    def __init__(self, sheets_manager, config_manager, scrapers: Dict[str, Any]):
        self.sheets_manager = sheets_manager
        self.config_manager = config_manager
        self.scrapers = scrapers.copy()

        if "WAL" in self.scrapers:
            try:
                from scrapers.walmart_doublecheck import WalmartDoubleCheckScraper
                original = self.scrapers["WAL"]
                self.scrapers["WAL"] = WalmartDoubleCheckScraper(
                    browser_manager=getattr(original, 'browser_manager', None),
                    logger_func=getattr(original, 'log', None)
                )
            except Exception:
                pass 

        self.queue = queue.Queue()
        self.is_running = False
        self.is_paused = False
        self.stop_requested = False
        
        self.active_threads = 0
        self.is_multi_thread = False
       
        self.thread_lock = threading.Lock()
        self._result_ack_condition = threading.Condition()
        self._result_ack_tokens = set()
        self.vpn_lock = threading.Lock()
       
        self.vpn_manager = SurfsharkVPN()
        self.validator = SuspicionValidator()
        self.result_guard = EngineResultGuard(self.queue)
        # STRUCTURED-SCRAPER-FIX:ENGINE-PATCHED
        self.ai_memory = AIMemory() 
       
        self.use_vpn = False
        self.vpn_connected = False
        
        self.ai_mode = "Regular (Regex)"
        self.cpu_limit_enabled = False
        self.memory_monitor_enabled = False
        self.multi_tab_enabled = False
        
        self.review_mode = False
        
        self.recovery_file = os.path.join("config", "crash_recovery.json")
        self.pending_updates = []
        self._load_recovery_data()
       
        self.links_processed = 0
        self.supplier_indexes = {s: 0 for s in list(self.scrapers.keys())}
        
        self.session_timestamp = None
       
        self.stats = {
            "total": 0, "processed": 0, "oos": 0, "instock": 0,
            "updates": 0, "stock_updates": 0, "in2oos": 0, "oos2in": 0, "errors": 0
        }
       
        self.db_conn = sqlite3.connect('scraper_cache.db', check_same_thread=False)
        self._init_db()

    def _load_recovery_data(self):
        try:
            if os.path.exists(self.recovery_file):
                with open(self.recovery_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    self.pending_updates = data.get("verified", [])
                    
                    if hasattr(self.validator, 'pending_items'):
                        self.validator.pending_items = data.get("unverified", [])
                        
                    if hasattr(self.validator, 'pass1_data'):
                        self.validator.pass1_data = {
                            int(k): v for k, v in data.get("pass1", {}).items() if str(k).isdigit()
                        }
        except Exception as e:
            print(f"Error loading recovery data: {e}")

    def save_recovery_data(self):
        try:
            os.makedirs(os.path.dirname(self.recovery_file), exist_ok=True)
            
            unverified = getattr(self.validator, 'pending_items', [])
            pass1 = getattr(self.validator, 'pass1_data', {})
            
            data = {
                "verified": self.pending_updates,
                "unverified": unverified,
                "pass1": pass1
            }
            with open(self.recovery_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            self.queue.put(("LOG", f"Failed to save recovery cache: {e}", "error", None))

    def _init_db(self):
        cursor = self.db_conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_cache (
                row_num INTEGER PRIMARY KEY,
                supplier TEXT,
                url TEXT,
                title TEXT,
                prev_price TEXT,
                prev_stock TEXT,
                new_price TEXT,
                new_stock TEXT,
                status TEXT,
                action_note TEXT,
                last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.db_conn.commit()

    def _flush_logs(self, logs: List[Tuple]):
        for message, tag, url in logs:
            self.queue.put(("LOG", message, tag, url))
            if url and tag in ["price", "oos", "error", "info"]:
                supplier = None
                url_lower = url.lower()
                if "sportsmansguide.com" in url_lower:
                    supplier = "SG"
                elif "walmart.com" in url_lower:
                    supplier = "WAL"
                elif "harborfreight.com" in url_lower:
                    supplier = "HF"
                elif "webstaurantstore.com" in url_lower:
                    supplier = "WEB"
                elif "menards.com" in url_lower:
                    supplier = "MN"
                elif "lakeside.com" in url_lower:
                    supplier = "LS"
                elif "collectionsetc.com" in url_lower:
                    supplier = "CE"
                
                if supplier:
                    self._log_to_supplier_file(supplier, message, tag)

    def _log_to_supplier_file(self, supplier: str, message: str, tag: str):
        base_path = SUPPLIER_LOG_FILES.get(supplier)
        if not base_path or not self.session_timestamp:
            return
            
        log_file = f"{base_path}_{self.session_timestamp}.txt"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{tag.upper()}] {message}\n"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _log_change_to_file(self, supplier: str, row_num: int, url: str, prev_price: str, new_price: str, prev_stock: str, new_stock: str):
        base_path = SUPPLIER_LOG_FILES.get(supplier)
        if not base_path or not self.session_timestamp:
            return
            
        log_file = f"{base_path}_{self.session_timestamp}.txt"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] Row {row_num} | URL: {url}\n"
       
        if prev_price != new_price and new_price and new_price != "N/A" and new_price != "See Member Price in Checkout":
            line += f" Price changed: {prev_price} → {new_price}\n"
        if prev_stock != new_stock:
            line += f" Stock changed: {prev_stock} → {new_stock}\n"
       
        line += "-" * 90 + "\n"
       
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            self.queue.put(("LOG", f"Failed to write to {log_file}: {e}", "error", None))

    def disconnect_vpn(self):
        if self.use_vpn and self.vpn_connected:
            self.queue.put(("LOG", "Engine run complete. Disconnecting VPN...", "info", None))
            try:
                self.vpn_manager.disconnect()
            except Exception:
                pass
            self.vpn_connected = False

    def cleanup_vpn(self):
        try:
            if self.use_vpn and self.vpn_connected:
                self.queue.put(("LOG", "App closing - safely severing VPN...", "info", None))
                self.vpn_manager.disconnect()
        except Exception:
            pass


    # STRUCTURED-SCRAPER-FIX:ACK-METHODS-BEGIN
    @staticmethod
    def _result_ack_token(supplier, row_num, is_verify):
        return str(supplier), int(row_num), bool(is_verify)

    def _mark_result_ack(self, supplier, row_num, is_verify):
        token = self._result_ack_token(supplier, row_num, is_verify)
        with self._result_ack_condition:
            self._result_ack_tokens.add(token)
            self._result_ack_condition.notify_all()

    def _wait_for_result_ack(
        self,
        supplier,
        row_num,
        is_verify,
        timeout=180.0,
    ):
        token = self._result_ack_token(supplier, row_num, is_verify)
        deadline = time.monotonic() + float(timeout)
        with self._result_ack_condition:
            while (
                token not in self._result_ack_tokens
                and not self.stop_requested
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.queue.put((
                        "LOG",
                        (
                            f"[{supplier}] Row {row_num} result acknowledgement "
                            "timed out; continuing safely."
                        ),
                        "warning",
                        None,
                    ))
                    return False
                self._result_ack_condition.wait(
                    timeout=min(remaining, 1.0)
                )
            self._result_ack_tokens.discard(token)
        return not self.stop_requested
    # STRUCTURED-SCRAPER-FIX:ACK-METHODS-END

    def start_check(self, retry_list=None, is_scheduled=False):
        if not (self.scrape_web_var.get() or self.scrape_wal_var.get() or self.scrape_sg_var.get() or self.scrape_hf_var.get() or self.scrape_mn_var.get() or self.scrape_ls_var.get() or self.scrape_ce_var.get()):
            self._write_log("No suppliers selected. Please select at least one supplier.", "error")
            return
            
        self.debug_bm.headless = self.headless_var.get()
        self.multi_tab_enabled = self.multi_thread_var.get()
        self.review_mode = self.review_mode_var.get()
        
        if self.sg_debug_var.get():
            self.scrapers["SG"].browser_manager = self.debug_bm
        else:
            self.scrapers["SG"].browser_manager = None
            
        self.use_vpn = self.use_vpn_var.get()
        
        self.config_manager.save_config("start_row", self.start_row_var.get())
            
        data = self.sheets_manager.get_all_rows()
        if not data or len(data) < 2:
            self._write_log("Sheet Error: Failed to fetch Google Sheet data or sheet is empty.", "error")
            return
            
        try: 
            start_row_limit = max(2, int(self.start_row_var.get().strip()))
        except ValueError: 
            start_row_limit = 2

        rows = data[1:]
        supplier_urls = { "WEB": [], "WAL": [], "SG": [], "HF": [], "MN": [], "LS": [], "CE": [] }
        supplier_rows = { "WEB": [], "WAL": [], "SG": [], "HF": [], "MN": [], "LS": [], "CE": [] }
        supplier_row_nums = { "WEB": [], "WAL": [], "SG": [], "HF": [], "MN": [], "LS": [], "CE": [] }
        supplier_targets = { "WEB": [], "WAL": [], "SG": [], "HF": [], "MN": [], "LS": [], "CE": [] }

        url_col = SHEET_COLS.get("url", 4)
        variation_col = SHEET_COLS.get("variation", 8)

        for i, row in enumerate(rows):
            row_num = i + 2
            if row_num < start_row_limit: 
                continue
            if len(row) <= url_col or not row[url_col]: 
                continue
            
            url = row[url_col].strip()
            
            if retry_list and url not in retry_list:
                continue

            target_var = ""
            if i > 0 and len(rows[i-1]) > variation_col and str(rows[i-1][variation_col]).strip():
                target_var = str(rows[i-1][variation_col]).strip()
            elif len(row) > variation_col and str(row[variation_col]).strip():
                target_var = str(row[variation_col]).strip()

            if self.scrape_web_var.get() and "webstaurantstore.com" in url.lower():
                supplier_urls["WEB"].append(url)
                supplier_rows["WEB"].append(row)
                supplier_row_nums["WEB"].append(row_num)
                supplier_targets["WEB"].append(target_var)
            elif self.scrape_wal_var.get() and "walmart.com" in url.lower():
                supplier_urls["WAL"].append(url)
                supplier_rows["WAL"].append(row)
                supplier_row_nums["WAL"].append(row_num)
                supplier_targets["WAL"].append(target_var)
            elif self.scrape_sg_var.get() and "sportsmansguide.com" in url.lower():
                supplier_urls["SG"].append(url)
                supplier_rows["SG"].append(row)
                supplier_row_nums["SG"].append(row_num)
                supplier_targets["SG"].append(target_var)
            elif self.scrape_hf_var.get() and "harborfreight.com" in url.lower():
                supplier_urls["HF"].append(url)
                supplier_rows["HF"].append(row)
                supplier_row_nums["HF"].append(row_num)
                supplier_targets["HF"].append(target_var)
            elif self.scrape_mn_var.get() and "menards.com" in url.lower():
                supplier_urls["MN"].append(url)
                supplier_rows["MN"].append(row)
                supplier_row_nums["MN"].append(row_num)
                supplier_targets["MN"].append(target_var)
            elif self.scrape_ls_var.get() and "lakeside.com" in url.lower():
                supplier_urls["LS"].append(url)
                supplier_rows["LS"].append(row)
                supplier_row_nums["LS"].append(row_num)
                supplier_targets["LS"].append(target_var)
            elif self.scrape_ce_var.get() and "collectionsetc.com" in url.lower():
                supplier_urls["CE"].append(url)
                supplier_rows["CE"].append(row)
                supplier_row_nums["CE"].append(row_num)
                supplier_targets["CE"].append(target_var)

        total_urls = sum(len(urls) for urls in supplier_urls.values())
        if total_urls == 0:
            self._write_log(f"No valid URLs found for selected supplier(s).", "error")
            return

        if not is_scheduled and (getattr(self, 'pending_updates', []) or getattr(self.validator, 'pending_items', [])):
            self._write_log("Existing updates found. New scrape data will append to your Review Window.", "info")

        if not is_scheduled:
            if hasattr(self, 'failed_urls'): 
                self.failed_urls.clear()
            if hasattr(self, 'captcha_urls'): 
                self.captcha_urls.clear()
            
            self.session_timestamp = None
            self.vpn_connected = False
            
            self.is_paused = False
            self.stats = {
                "total": total_urls, "processed": 0, "oos": 0, "instock": 0, 
                "updates": 0, "stock_updates": 0, "in2oos": 0, "oos2in": 0, "errors": 0
            }
            self.supplier_indexes = {s: 0 for s in supplier_urls.keys()}
        
        self.queue.put(("LOG", f"Review Mode (Hold Updates): {'ON' if self.review_mode else 'OFF'}", "info", None))
        
        if self.multi_tab_enabled:
            self.queue.put(("LOG", "Initiating Concurrent Engine...", "info", None))
            self.start_concurrent_scrape(supplier_urls, supplier_rows, supplier_row_nums, supplier_targets)
        else:
            self.queue.put(("LOG", "Initiating Sequential Engine...", "info", None))
            self.start_sequential_scrape(supplier_urls, supplier_rows, supplier_row_nums, supplier_targets)

    def _write_log(self, message, tag="info", url=None):
        self.queue.put(("LOG", message, tag, url))

    def start_verification_phase(self):
        v_list = self.validator.pop_pending()
        
        if not hasattr(self.validator, 'pending_items'):
            self.validator.pending_items = []
        self.validator.pending_items.extend(v_list)
        self.save_recovery_data()
        
        supplier_urls = {s: [] for s in self.scrapers}
        supplier_rows = {s: [] for s in self.scrapers}
        supplier_row_nums = {s: [] for s in self.scrapers}
        supplier_targets = {s: [] for s in self.scrapers}
        
        for item in v_list:
            sup = item["supplier"]
            supplier_urls[sup].append(item["url"])
            supplier_rows[sup].append(item["row"])
            supplier_row_nums[sup].append(item["row_num"])
            supplier_targets[sup].append("VERIFY:" + item["target_var"])
            
            self.validator.pass1_data[item["row_num"]] = {
                "price": item["pass1_price"],
                "stock": item["pass1_stock"]
            }
            
        self.supplier_indexes = {s: 0 for s in list(self.scrapers.keys())}
            
        self.queue.put(("LOG", "Starting Phase 2 Suspicion Verification Scrape...", "info", None))
        
        if self.is_multi_thread:
            self.start_concurrent_scrape(supplier_urls, supplier_rows, supplier_row_nums, supplier_targets)
        else:
            self.start_sequential_scrape(supplier_urls, supplier_rows, supplier_row_nums, supplier_targets)

    def start_sequential_scrape(self, supplier_urls, supplier_rows, supplier_row_nums, targets):
        self.is_multi_thread = False
        self.is_running = True
        self.is_paused = False
        self.stop_requested = False
        self.active_threads = 1
        
        if not self.session_timestamp:
            self.session_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self.links_processed = 0
        
        t = threading.Thread(target=self._sequential_worker, args=(supplier_urls, supplier_rows, supplier_row_nums, targets), daemon=True)
        t.start()

    def _sequential_worker(self, supplier_urls, supplier_rows, supplier_row_nums, targets):
        for supplier in list(self.scrapers.keys()):
            urls = supplier_urls.get(supplier, [])
            scraper = self.scrapers.get(supplier)
            if not scraper or not urls: 
                continue

            if supplier in ["SG", "HF", "CE"] and self.use_vpn:
                if not self.vpn_connected:
                    self.queue.put(("LOG", f"Connecting to VPN for {supplier}...", "price", None))
                    success, msg = self.vpn_manager.connect()
                    if success:
                        self.queue.put(("LOG", msg, "price", None))
                        self.vpn_connected = True
                    else:
                        self.queue.put(("LOG", f"VPN Failed for {supplier}: {msg}. Continuing without VPN.", "error", None))

            try:
                for i in range(self.supplier_indexes.get(supplier, 0), len(urls)):
                    url = urls[i]
                    row_num = supplier_row_nums[supplier][i]
                    target_var = targets[supplier][i]
                    row = supplier_rows[supplier][i]
                    
                    if self.cpu_limit_enabled:
                        time.sleep(0.05)
                   
                    is_verify = False
                    if isinstance(target_var, str) and target_var.startswith("VERIFY:"):
                        is_verify = True
                        target_var = target_var.replace("VERIFY:", "")
                   
                    while True:
                        while self.is_paused and not self.stop_requested:
                            time.sleep(1)
                           
                        if not self.is_running or self.stop_requested:
                            break
                   
                        prefix = "[VERIFY]" if is_verify else f"[{supplier}]"
                        self.queue.put(("LOG", f"{prefix} Processing row {row_num}: {url} | Target Var: '{target_var}'", "info", None))
                        
                        if (
                            not is_verify
                            and self.ai_mode not in (
                                "OFF",
                                "Regular (Regex)",
                            )
                            and self.ai_memory.is_known_problem(url)
                        ):
                            self.queue.put(("LOG", f"[{supplier}] Memory triggered: Bypassing standard scraper for known problem URL.", "warning", url))
                            ai = OllamaAI(model_string=self.ai_mode)
                            price, stock = ai.manual_ai_extraction(url)
                            status = "Success"
                            title = "AI Auto-Extracted"
                            variants = [{"label": target_var or "Default", "price": price, "stock": stock}]
                            logs = [("AI manually extracted data using learned rules.", "info", url)]
                            if str(stock).strip().upper() == "UNKNOWN":
                                status = "Error"
                                title = "AI Extraction Inconclusive"
                                price = ""
                                logs = [(
                                    "AI extraction was inconclusive; no change accepted.",
                                    "error",
                                    url,
                                )]
                            time.sleep(1) 
                        else:
                            max_retries = 3
                            for attempt in range(max_retries):
                                if getattr(scraper, 'browser_manager', None):
                                    scraper.browser_manager.wait_for_js_interactive(timeout=8)
                                   
                                status, price, stock, title, variants, logs = scraper.scrape(url, target_variation=target_var)
                                
                                if status == "Error" and "Captcha" not in stock:
                                    if attempt < max_retries - 1:
                                        self._flush_logs(logs)
                                        self.queue.put(("LOG", f"[{supplier}] Strike {attempt+1}! Retrying {url}...", "error", None))
                                        time.sleep(random.uniform(3.0, 6.0))
                                        continue
                                break
                            self._flush_logs(logs)
                            if status == "Error" and "Captcha" in stock:
                                self.queue.put(("CAPTCHA_PAUSE", url, supplier))
                                self.is_paused = True
                                continue
                           
                        self.queue.put((supplier, url, price, stock, status, title, row, row_num, variants, is_verify))
                        # STRUCTURED-SCRAPER-FIX:SEQUENTIAL-ORDER
                        self._wait_for_result_ack(
                            supplier,
                            row_num,
                            is_verify,
                        )
                        self.supplier_indexes[supplier] = i + 1
                       
                        self.links_processed += 1
                        if self.memory_monitor_enabled and self.links_processed % 50 == 0:
                            if getattr(scraper, 'browser_manager', None):
                                scraper.browser_manager.restart_browser()
                                
                        break
                   
                    if not self.is_running or self.stop_requested:
                        break
            finally:
                pass 
                   
            if not self.is_running or self.stop_requested:
                break
                
        with self.thread_lock:
            self.active_threads -= 1
            if self.active_threads == 0 and not self.stop_requested:
                self.queue.put(("COMPLETE", None))

    def start_concurrent_scrape(self, supplier_urls, supplier_rows, supplier_row_nums, targets):
        self.is_multi_thread = True
        self.is_running = True
        self.is_paused = False
        self.stop_requested = False
        self.active_threads = 0
        
        if not self.session_timestamp:
            self.session_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self.links_processed = 0
        
        t = threading.Thread(target=self._concurrent_spawner, args=(supplier_urls, supplier_rows, supplier_row_nums, targets), daemon=True)
        t.start()
       
    def _concurrent_spawner(self, supplier_urls, supplier_rows, supplier_row_nums, targets):
        for supplier in list(self.scrapers.keys()):
            urls = supplier_urls.get(supplier, [])
            scraper = self.scrapers.get(supplier)
            if not scraper or not urls: 
                continue
           
            if self.supplier_indexes.get(supplier, 0) < len(urls):
                with self.thread_lock:
                    self.active_threads += 1
                
                t = threading.Thread(
                    target=self._concurrent_worker,
                    args=(supplier, urls, supplier_rows[supplier], supplier_row_nums[supplier], targets[supplier]),
                    daemon=True
                )
                t.start()
               
        if self.active_threads == 0:
            self.queue.put(("COMPLETE", None))

    def _concurrent_worker(self, supplier, urls, rows, row_nums, targets):
        scraper = self.scrapers.get(supplier)
       
        if supplier in ["SG", "HF", "CE"] and self.use_vpn:
            with self.vpn_lock:
                if not self.vpn_connected:
                    self.queue.put(("LOG", f"Connecting to VPN for {supplier}...", "price", None))
                    success, msg = self.vpn_manager.connect()
                    if success:
                        self.queue.put(("LOG", msg, "price", None))
                        self.vpn_connected = True
                    else:
                        self.queue.put(("LOG", f"VPN Failed for {supplier}: {msg}. Continuing without VPN.", "error", None))

        try:
            for i in range(self.supplier_indexes.get(supplier, 0), len(urls)):
                url = urls[i]
                row_num = row_nums[i]
                target_var = targets[i]
                row = rows[i]
               
                if self.cpu_limit_enabled: 
                    time.sleep(0.05)
               
                is_verify = False
                if isinstance(target_var, str) and target_var.startswith("VERIFY:"):
                    is_verify = True
                    target_var = target_var.replace("VERIFY:", "")
               
                while True:
                    while self.is_paused and not self.stop_requested:
                        time.sleep(1)
                       
                    if not self.is_running or self.stop_requested:
                        break
                 
                    prefix = "[VERIFY]" if is_verify else f"[{supplier}]"
                    self.queue.put(("LOG", f"{prefix} Processing row {row_num}: {url} | Target Var: '{target_var}'", "info", None))
                   
                    if (
                        not is_verify
                        and self.ai_mode not in (
                            "OFF",
                            "Regular (Regex)",
                        )
                        and self.ai_memory.is_known_problem(url)
                    ):
                        self.queue.put(("LOG", f"[{supplier}] Memory triggered: Bypassing standard scraper for known problem URL.", "warning", url))
                        ai = OllamaAI(model_string=self.ai_mode)
                        price, stock = ai.manual_ai_extraction(url)
                        status = "Success"
                        title = "AI Auto-Extracted"
                        variants = [{"label": target_var or "Default", "price": price, "stock": stock}]
                        logs = [("AI manually extracted data using learned rules.", "info", url)]
                        if str(stock).strip().upper() == "UNKNOWN":
                            status = "Error"
                            title = "AI Extraction Inconclusive"
                            price = ""
                            logs = [(
                                "AI extraction was inconclusive; no change accepted.",
                                "error",
                                url,
                            )]
                        tab_handle = None
                        if self.multi_tab_enabled and getattr(scraper, 'browser_manager', None):
                            tab_handle = scraper.browser_manager.get_new_tab()
                            scraper.browser_manager.close_tab(tab_handle)
                    else:
                        max_retries = 3
                        tab_handle = None
                        for attempt in range(max_retries):
                            if getattr(scraper, 'browser_manager', None):
                                scraper.browser_manager.wait_for_js_interactive()
                               
                            if self.multi_tab_enabled and getattr(scraper, 'browser_manager', None):
                                tab_handle = scraper.browser_manager.get_new_tab()
                                scraper.browser_manager.switch_to_tab(tab_handle)
                               
                            status, price, stock, title, variants, logs = scraper.scrape(url, target_variation=target_var)
                           
                            if self.multi_tab_enabled and getattr(scraper, 'browser_manager', None) and tab_handle:
                                scraper.browser_manager.close_tab(tab_handle)
                                
                            if status == "Error" and "Captcha" not in stock:
                                if attempt < max_retries - 1:
                                    time.sleep(random.uniform(3.0, 6.0))
                                    continue
                            break
                           
                        self._flush_logs(logs)
                        if status == "Error" and "Captcha" in stock:
                            self.queue.put(("CAPTCHA_PAUSE", url, supplier))
                            self.is_paused = True
                            continue
                       
                    self.queue.put((supplier, url, price, stock, status, title, row, row_num, variants, is_verify))
                    self.supplier_indexes[supplier] = i + 1
                   
                    self.links_processed += 1
                    if self.memory_monitor_enabled and self.links_processed % 50 == 0:
                        if getattr(scraper, 'browser_manager', None):
                            scraper.browser_manager.restart_browser()
                            
                    break
                   
                if not self.is_running or self.stop_requested:
                    break
        finally:
            pass
            
            with self.thread_lock:
                self.active_threads -= 1
                if self.active_threads == 0 and not self.stop_requested:
                    self.queue.put(("COMPLETE", None))


    # STRUCTURED-SCRAPER-FIX:PROCESS-WRAPPER-BEGIN
    def process_result_item(self, item: tuple):
        supplier = item[0] if len(item) > 0 else ""
        row_num = item[7] if len(item) > 7 else 0
        is_verify = item[9] if len(item) > 9 else False
        try:
            return self._process_result_item_impl(item)
        finally:
            try:
                self._mark_result_ack(
                    supplier,
                    row_num,
                    is_verify,
                )
            except Exception:
                pass
    # STRUCTURED-SCRAPER-FIX:PROCESS-WRAPPER-END

    def _process_result_item_impl(self, item: tuple):
        supplier, url, price, stock, status, title, row, row_num, variants, is_verify = item
        
        # STRUCTURED-SCRAPER-FIX:ENGINE-GATE-BEGIN
        decision = self.result_guard.evaluate(
            supplier=supplier,
            url=url,
            row_num=row_num,
            price=price,
            stock=stock,
            status=status,
            title=title,
            variants=variants,
            is_verify=is_verify,
        )
        if not decision.accept:
            if not is_verify:
                self.stats['processed'] += 1
            if decision.count_as_error:
                self.stats['errors'] += 1
            self.queue.put((
                'LOG',
                decision.message,
                decision.tag,
                url,
            ))
            self._log_to_supplier_file(
                supplier,
                decision.message,
                decision.tag,
            )
            return
        price = decision.price
        stock = decision.stock
        status = decision.status
        # STRUCTURED-SCRAPER-FIX:ENGINE-GATE-END
        if not is_verify:
            self.stats["processed"] += 1

        # --- LOW-STOCK FAILSAFE OVERRIDE ---
        stock_lower = stock.lower()
        if "low stock" in stock_lower or "limited stock" in stock_lower or re.search(r'only\s+\d+\s+(remaining|left|in stock)', stock_lower):
            stock = "OOS"
            price = ""
       
        is_oos = stock.upper() in ["OOS"]
        stock_col = SHEET_COLS.get("stock_status", 2)
        price_col = SHEET_COLS.get("price", 15)
        var_col = SHEET_COLS.get("variation", 8)
       
        prev_stock_note = str(row[stock_col]).strip() if len(row) > stock_col else ""
        prev_stock_upper = prev_stock_note.upper()
        
        # --- NEW BLANK/NA LOGIC ---
        is_sheet_blank = prev_stock_upper in ["", "N/A", "NONE", "BLANK", "N / A"]
        
        prev_price = str(row[price_col]).strip() if len(row) > price_col else ""
       
        price_changed = (status == "Success") and (price != prev_price) and price and price != "N/A" and price != "See Member Price in Checkout" and not is_oos
        
        stock_changed = False
        if status == "Success" and stock != "":
            if is_sheet_blank and stock.upper() == "IN STOCK":
                stock_changed = False
            elif stock.upper() != prev_stock_upper:
                stock_changed = True

        def revert_phase1_stats(p1):
            if not p1: 
                return
            p1_is_oos = p1["stock"].upper() in ["OOS"]
            p1_price_changed = (p1["price"] != prev_price) and p1["price"] and p1["price"] != "N/A" and p1["price"] != "See Member Price in Checkout" and not p1_is_oos
            
            p1_stock_changed = False
            if p1["stock"] != "":
                if is_sheet_blank and p1["stock"].upper() == "IN STOCK":
                    p1_stock_changed = False
                elif p1["stock"].upper() != prev_stock_upper:
                    p1_stock_changed = True

            if p1_stock_changed:
                self.stats["stock_updates"] -= 1
                if p1_is_oos and prev_stock_upper != "OOS": 
                    self.stats["in2oos"] -= 1
                elif not p1_is_oos and prev_stock_upper == "OOS": 
                    self.stats["oos2in"] -= 1
            if p1_price_changed:
                self.stats["updates"] -= 1

            if p1_is_oos and prev_stock_upper != "OOS":
                self.stats["oos"] -= 1
                self.stats["instock"] += 1
            elif not p1_is_oos and prev_stock_upper == "OOS":
                self.stats["instock"] -= 1
                self.stats["oos"] += 1

        p1_data = None
        if is_verify and row_num in self.validator.pass1_data:
            p1_data = self.validator.pass1_data[row_num].copy()

        if status == "Error":
            if not is_verify:
                self.stats["errors"] += 1
            else:
                revert_phase1_stats(p1_data)
                if row_num in self.validator.pass1_data:
                    del self.validator.pass1_data[row_num]
                self.queue.put(("LOG", f"Row {row_num} - Phase 2 Verification Errored. Reverted Phase 1 stats.", "error", url))

            self.queue.put(("LOG", f"Row {row_num} - Error for {url}.", "error", None))
            self._log_to_supplier_file(supplier, f"Row {row_num} | SCRAPE ERROR | URL: {url}", "error")
            return

        ai_status_note = ""

        # ==========================================
        # PHASE 1: INITIAL SCRAPE
        # ==========================================
        if not is_verify:
            if is_oos: 
                self.stats["oos"] += 1
            else: 
                self.stats["instock"] += 1

            if price_changed or stock_changed:
                if stock_changed:
                    self.stats["stock_updates"] += 1
                    if is_oos and prev_stock_upper != "OOS": 
                        self.stats["in2oos"] += 1
                    elif not is_oos and prev_stock_upper == "OOS": 
                        self.stats["oos2in"] += 1
                if price_changed:
                    self.stats["updates"] += 1

                if self.ai_mode != "OFF":
                    t_var = str(row[var_col]).strip() if len(row) > var_col else ""
                    self.validator.flag({
                        "supplier": supplier, "url": url, "row": row, "row_num": row_num,
                        "target_var": t_var, "pass1_price": price, "pass1_stock": stock
                    })
                    
                    self.save_recovery_data()
                    
                    change_details = []
                    if stock_changed: 
                        change_details.append(f"Stock: {prev_stock_note or 'N/A'} → {stock}")
                    if price_changed: 
                        change_details.append(f"Price: {prev_price or 'N/A'} → {price}")
                    
                    detail_str = " | ".join(change_details)
                    self.queue.put(("LOG", f"Row {row_num} - Suspicious change detected ({detail_str}). Queued for Phase 2...", "warning", url))
                    return 
                else:
                    self.queue.put(("LOG", f"Row {row_num} - Change detected. AI Verification OFF. Auto-confirming...", "warning", url))
                    ai_status_note = "Auto-Bypassed (AI OFF)"
            else:
                try:
                    cursor = self.db_conn.cursor()
                    cursor.execute('''
                        INSERT OR REPLACE INTO product_cache
                        (row_num, supplier, url, title, prev_price, prev_stock, new_price, new_stock, status, action_note, last_checked)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ''', (row_num, supplier, url, title, prev_price, prev_stock_note, price, stock, status, "No Change"))
                    self.db_conn.commit()
                except Exception as e:
                    self.queue.put(("LOG", f"SQLite Cache Error: {str(e)}", "error", None))

                self.queue.put(("LOG", f"Row {row_num} | Price: {price} | Stock: {stock} | Status: {status}", "info", url))
                return 

        # ==========================================
        # PHASE 2: BACKGROUND VERIFICATION
        # ==========================================
        else:
            if hasattr(self.validator, 'pending_items'):
                self.validator.pending_items = [i for i in self.validator.pending_items if i.get('row_num') != row_num]

            verification = self.validator.verify(
                row_num,
                url,
                price,
                stock,
                self.ai_mode,
                supplier=supplier,
            )
            reason = getattr(self.validator, "last_reason", "")

            if verification is True:
                self.queue.put((
                    "LOG",
                    f"Row {row_num} - UPDATE CONFIRMED. {reason}",
                    "info",
                    url,
                ))
                ai_status_note = "Verified"

            elif verification is False:
                self.queue.put((
                    "LOG",
                    f"Row {row_num} - VERIFICATION REJECTED. "
                    f"Holding for manual review. {reason}",
                    "warning",
                    url,
                ))
                ai_status_note = "Verification Rejected"
                self.ai_memory.remember_failure(
                    url,
                    reason or "Verification rejected",
                )

            else:
                self.queue.put((
                    "LOG",
                    f"Row {row_num} - VERIFICATION INCONCLUSIVE. "
                    f"Holding for manual review. {reason}",
                    "warning",
                    url,
                ))
                ai_status_note = "Verification Inconclusive"
        # ==========================================
        # PHASE 3: PAYLOAD PREPARATION
        # ==========================================
        updates = []
        if price_changed or stock_changed:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            updates.append({"range": f"A{row_num}", "values": [[f"Updated: {timestamp}"]]} )

            if stock_changed:
                updates.append({"range": f"C{row_num}", "values": [[stock]]})
            if price_changed:
                updates.append({"range": f"P{row_num}", "values": [[price]]})

            note = ""
            if stock_changed and price_changed: 
                note = "Stock and Price update"
            elif stock_changed: 
                note = "Stock change"
            elif price_changed: 
                note = "Price update"

            self._log_change_to_file(supplier, row_num, url, prev_price, price, prev_stock_note, stock)
            self.queue.put(("LOG", f"Row {row_num} - {note}: {prev_price} → {price} | Stock: {prev_stock_note} → {stock}", "price", url))

            try:
                cursor = self.db_conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO product_cache
                    (row_num, supplier, url, title, prev_price, prev_stock, new_price, new_stock, status, action_note, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (row_num, supplier, url, title, prev_price, prev_stock_note, price, stock, status, note))
                self.db_conn.commit()
            except Exception as e:
                self.queue.put(("LOG", f"SQLite Cache Error: {str(e)}", "error", None))

            force_manual_review = ai_status_note in {
                "Verification Rejected",
                "Verification Inconclusive",
            }

            if self.review_mode or force_manual_review:
                self.pending_updates = [item for item in self.pending_updates if item['row_num'] != row_num]
                
                self.pending_updates.append({
                    'row_num': row_num,
                    'supplier': supplier,
                    'url': url,
                    'prev_price': prev_price,
                    'price': price,
                    'prev_stock': prev_stock_note,
                    'stock': stock,
                    'updates_payload': updates,
                    'note': note,
                    'ai_status': ai_status_note
                })
                self.save_recovery_data()
                self.queue.put(("LOG", f"Row {row_num} - HELD FOR USER REVIEW", "info", url))
            else:
                if updates:
                    self.sheets_manager.batch_update(updates)
