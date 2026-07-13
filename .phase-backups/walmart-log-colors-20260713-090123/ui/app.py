import sys
import os

def fix_import_paths():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = script_dir

    possible_paths = [
        project_root,
        os.path.abspath(os.path.join(script_dir, '..')),
        os.path.abspath(os.path.join(script_dir, '../..')),
        os.path.join(project_root, "scrapers"),
        os.path.join(project_root, "core"),
        os.path.join(project_root, "ui"),
    ]

    for p in possible_paths:
        if p and p not in sys.path: 
            sys.path.insert(0, p)

    if 'scrapers' not in sys.modules:
        try: 
            import scrapers
        except ImportError: 
            pass

    if not hasattr(fix_import_paths, "already_logged"):
        print(f"✅ Import paths fixed for app.py. Project root: {project_root}")
        fix_import_paths.already_logged = True

fix_import_paths()

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import queue
import webbrowser
import threading
import time
from datetime import datetime
import atexit

from core.config_manager import ConfigManager
from core.sheets import GoogleSheetsManager
from core.engine import ScrapingEngine
from config.settings import SHEET_COLS, LOG_DIR

from scrapers.browser import BraveDebugManager
from scrapers.webstaurant import WebstaurantScraper
from scrapers.walmart import WalmartScraper
from scrapers.sportsmans_guide import SportsmansGuideScraper
from scrapers.harbor_freight import HarborFreightScraper
from scrapers.menards import MenardsScraper
from scrapers.lakeside import LakesideScraper
from scrapers.collectionsetc import CollectionsEtcScraper

from ui.review_window import ReviewUpdatesWindow

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class StockPriceCheckerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Stock & Price Checker - Dashboard")
        
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        self.geometry(f"{screen_width}x{screen_height}+0+0")
        self.minsize(900, 600)
        
        try: 
            self.after(0, lambda: self.state('zoomed'))
        except Exception: 
            pass

        self.config_manager = ConfigManager()
        self.sheets_manager = GoogleSheetsManager()
        
        self.debug_bm = BraveDebugManager(port=9222)

        self.scrapers = {
            "WEB": WebstaurantScraper(
   		 browser_manager=self.debug_bm,
   		 use_physical_browser=True
		),
            "WAL": WalmartScraper(browser_manager=self.debug_bm),
            "SG": SportsmansGuideScraper(browser_manager=None),
            "HF": HarborFreightScraper(browser_manager=self.debug_bm),
            "MN": MenardsScraper(browser_manager=self.debug_bm),
            "LS": LakesideScraper(browser_manager=self.debug_bm),
            "CE": CollectionsEtcScraper(browser_manager=None)
        }

        self.engine = ScrapingEngine(self.sheets_manager, self.config_manager, self.scrapers)

        # --- LOAD SAVED CONFIGURATIONS ---
        self.start_row_var = ctk.StringVar(value=str(self.config_manager.get("start_row", "2")))
        self.end_row_var = ctk.StringVar(value=str(self.config_manager.get("end_row", "")))
        self.schedule_interval = ctk.StringVar(value=str(self.config_manager.get("schedule_interval", "120")))
        
        self.multi_thread_var = ctk.BooleanVar(value=self.config_manager.get("multi_thread", False))
        self.review_mode_var = ctk.BooleanVar(value=self.config_manager.get("review_mode", True)) 
        
        self.enable_ai_var = ctk.BooleanVar(value=self.config_manager.get("enable_ai", True))
        self.ai_engine_var = ctk.StringVar(value=self.config_manager.get("ai_engine", "Qwen2.5:14b (~9GB VRAM)"))
        
        self.headless_var = ctk.BooleanVar(value=self.config_manager.get("headless", False))
        self.use_vpn_var = ctk.BooleanVar(value=self.config_manager.get("use_vpn", True))
        self.sg_debug_var = ctk.BooleanVar(value=self.config_manager.get("sg_debug", False))
        
        self.scrape_web_var = ctk.BooleanVar(value=self.config_manager.get("scrape_web", False))
        self.scrape_wal_var = ctk.BooleanVar(value=self.config_manager.get("scrape_wal", False))
        self.scrape_sg_var = ctk.BooleanVar(value=self.config_manager.get("scrape_sg", True))
        self.scrape_hf_var = ctk.BooleanVar(value=self.config_manager.get("scrape_hf", False))
        self.scrape_mn_var = ctk.BooleanVar(value=self.config_manager.get("scrape_mn", False))
        self.scrape_ls_var = ctk.BooleanVar(value=self.config_manager.get("scrape_ls", True))
        self.scrape_ce_var = ctk.BooleanVar(value=self.config_manager.get("scrape_ce", True))

        self.captcha_urls = []
        self.failed_urls = []
        
        self.is_scheduling = False
        self.countdown_seconds = 0

        self.setup_ui()
        self.after(100, self._poll_engine_queue)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        atexit.register(self.cleanup_on_exit)

    def save_all_settings(self):
        """Flushes the current state of every UI widget to the config file."""
        try:
            self.config_manager.save_config("start_row", self.start_row_var.get())
            self.config_manager.save_config("end_row", self.end_row_var.get())
            self.config_manager.save_config("schedule_interval", self.schedule_interval.get())
            
            self.config_manager.save_config("multi_thread", self.multi_thread_var.get())
            self.config_manager.save_config("review_mode", self.review_mode_var.get())
            self.config_manager.save_config("enable_ai", self.enable_ai_var.get())
            self.config_manager.save_config("ai_engine", self.ai_engine_var.get())
            self.config_manager.save_config("headless", self.headless_var.get())
            self.config_manager.save_config("use_vpn", self.use_vpn_var.get())
            self.config_manager.save_config("sg_debug", self.sg_debug_var.get())
            
            self.config_manager.save_config("scrape_web", self.scrape_web_var.get())
            self.config_manager.save_config("scrape_wal", self.scrape_wal_var.get())
            self.config_manager.save_config("scrape_sg", self.scrape_sg_var.get())
            self.config_manager.save_config("scrape_hf", self.scrape_hf_var.get())
            self.config_manager.save_config("scrape_mn", self.scrape_mn_var.get())
            self.config_manager.save_config("scrape_ls", self.scrape_ls_var.get())
            self.config_manager.save_config("scrape_ce", self.scrape_ce_var.get())
        except Exception as e:
            print(f"Failed to save UI settings: {e}")

    def on_closing(self):
        self.cleanup_on_exit()
        self.destroy()

    def cleanup_on_exit(self):
        # Save all UI checkbox and text field states exactly as they are on close
        self.save_all_settings()
        
        try:
            if hasattr(self, 'engine'): 
                self.engine.save_recovery_data()
            if hasattr(self.engine, 'cleanup_vpn'): 
                self.engine.cleanup_vpn()
            if hasattr(self, 'debug_bm') and self.debug_bm: 
                self.debug_bm.quit()
        except Exception: 
            pass

    def validate_number_input(self, new_value):
        return new_value == "" or new_value.isdigit()

    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar_frame = ctk.CTkFrame(self, width=260, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(4, weight=1)

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="STOCK CHECKER", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(15, 5))

        self.supplier_label = ctk.CTkLabel(self.sidebar_frame, text="Target Suppliers:", anchor="w")
        self.supplier_label.grid(row=1, column=0, padx=20, pady=(5, 0), sticky="w")
        
        self.supplier_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.supplier_frame.grid(row=2, column=0, padx=15, pady=0, sticky="ew")
        self.supplier_frame.grid_columnconfigure((0, 1), weight=1)
        
        self.web_check = ctk.CTkCheckBox(self.supplier_frame, text="Webstaurant", variable=self.scrape_web_var)
        self.web_check.grid(row=0, column=0, pady=(5, 0), padx=5, sticky="w")
        
        self.ls_check = ctk.CTkCheckBox(self.supplier_frame, text="Lakeside", variable=self.scrape_ls_var)
        self.ls_check.grid(row=0, column=1, pady=(5, 0), padx=5, sticky="w")
        
        self.wal_check = ctk.CTkCheckBox(self.supplier_frame, text="Walmart", variable=self.scrape_wal_var)
        self.wal_check.grid(row=1, column=0, pady=(5, 0), padx=5, sticky="w")
        
        self.ce_check = ctk.CTkCheckBox(self.supplier_frame, text="Collections Etc.", variable=self.scrape_ce_var)
        self.ce_check.grid(row=1, column=1, pady=(5, 0), padx=5, sticky="w")
        
        self.sg_check = ctk.CTkCheckBox(self.supplier_frame, text="Sportsman's G.", variable=self.scrape_sg_var)
        self.sg_check.grid(row=2, column=0, columnspan=2, pady=(5, 0), padx=5, sticky="w")
        
        self.hf_check = ctk.CTkCheckBox(self.supplier_frame, text="Harbor Freight", variable=self.scrape_hf_var)
        self.hf_check.grid(row=3, column=0, columnspan=2, pady=(5, 0), padx=5, sticky="w")
        
        self.mn_check = ctk.CTkCheckBox(self.supplier_frame, text="Menards", variable=self.scrape_mn_var)
        self.mn_check.grid(row=4, column=0, columnspan=2, pady=(5, 10), padx=5, sticky="w")

        self.settings_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.settings_frame.grid(row=3, column=0, padx=15, pady=0, sticky="ew")
        
        vcmd = (self.register(self.validate_number_input), '%P')

        # --- ROW 0: Start Row ---
        ctk.CTkLabel(self.settings_frame, text="Start Row:").grid(row=0, column=0, sticky="w")
        self.start_row_entry = ctk.CTkEntry(self.settings_frame, textvariable=self.start_row_var, width=60, height=24, validate="key", validatecommand=vcmd)
        self.start_row_entry.grid(row=0, column=1, sticky="e")

        # --- ROW 1: End Row ---
        ctk.CTkLabel(self.settings_frame, text="End Row:").grid(row=1, column=0, sticky="w", pady=(2,0))
        self.end_row_entry = ctk.CTkEntry(self.settings_frame, textvariable=self.end_row_var, width=60, height=24, validate="key", validatecommand=vcmd, placeholder_text="All")
        self.end_row_entry.grid(row=1, column=1, sticky="e", pady=(2,0))

        # --- ROW 2: Interval ---
        ctk.CTkLabel(self.settings_frame, text="Interval (m):").grid(row=2, column=0, sticky="w", pady=(2,0))
        self.interval_entry = ctk.CTkEntry(self.settings_frame, textvariable=self.schedule_interval, width=60, height=24, validate="key", validatecommand=vcmd)
        self.interval_entry.grid(row=2, column=1, sticky="e", pady=(2,0))
        
        # --- ROW 3: AI Toggle ---
        self.ai_toggle = ctk.CTkSwitch(self.settings_frame, text="Enable AI (Phase 2)", variable=self.enable_ai_var, progress_color="#1ABC9C", switch_width=32, switch_height=16)
        self.ai_toggle.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8,0))
        
        # --- ROW 4/5: Engine Select ---
        ctk.CTkLabel(self.settings_frame, text="AI Engine:").grid(row=4, column=0, columnspan=2, sticky="w", pady=(4,2))
        self.engine_dropdown = ctk.CTkOptionMenu(
            self.settings_frame, 
            variable=self.ai_engine_var,
            values=["Regular (Regex)", "Qwen2.5:7b (~5GB VRAM)", "Qwen2.5:14b (~9GB VRAM)"],
            height=24,
            fg_color="#34495E", button_color="#2C3E50", button_hover_color="#1ABC9C"
        )
        self.engine_dropdown.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0,5))
        
        # --- ROW 6-10: Checkboxes ---
        self.headless_check = ctk.CTkCheckBox(self.settings_frame, text="Headless Mode", variable=self.headless_var, checkbox_width=16, checkbox_height=16)
        self.headless_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=(5,0))
        
        self.vpn_check = ctk.CTkCheckBox(self.settings_frame, text="Surfshark VPN (SG/HF/CE)", variable=self.use_vpn_var, checkbox_width=16, checkbox_height=16, text_color="#1ABC9C")
        self.vpn_check.grid(row=7, column=0, columnspan=2, sticky="w", pady=(5,0))
        
        self.sg_debug_check = ctk.CTkCheckBox(self.settings_frame, text="SG: Use Chrome Debug", variable=self.sg_debug_var, checkbox_width=16, checkbox_height=16, text_color="#3498DB")
        self.sg_debug_check.grid(row=8, column=0, columnspan=2, sticky="w", pady=(5,0))

        self.thread_switch = ctk.CTkSwitch(self.settings_frame, text="Concurrent", variable=self.multi_thread_var, progress_color="#8E44AD", switch_width=32, switch_height=16)
        self.thread_switch.grid(row=9, column=0, columnspan=2, sticky="w", pady=(10,0))
        
        self.review_check = ctk.CTkCheckBox(self.settings_frame, text="Review Mode (Hold Updates)", variable=self.review_mode_var, checkbox_width=16, checkbox_height=16, text_color="#F1C40F")
        self.review_check.grid(row=10, column=0, columnspan=2, sticky="w", pady=(10,0))

        self.button_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.button_frame.grid(row=5, column=0, padx=15, pady=(0, 20), sticky="ew")
        self.button_frame.grid_columnconfigure((0, 1), weight=1)

        self.check_btn = ctk.CTkButton(self.button_frame, text="Start Scraping", command=self.start_check, fg_color="#2FA572", hover_color="#1F7A50")
        self.check_btn.grid(row=0, column=0, columnspan=2, pady=(0, 8), sticky="ew")
        
        self.pause_btn = ctk.CTkButton(self.button_frame, text="Pause", command=self.pause_checking, state="disabled", fg_color="#E67E22", hover_color="#D35400")
        self.pause_btn.grid(row=1, column=0, padx=(0, 4), pady=(0, 8), sticky="ew")
        
        self.resume_btn = ctk.CTkButton(self.button_frame, text="Resume", command=self.resume_scrape, state="disabled")
        self.resume_btn.grid(row=1, column=1, padx=(4, 0), pady=(0, 8), sticky="ew")
        
        self.stop_btn = ctk.CTkButton(self.button_frame, text="Stop", command=self.stop_checking, state="disabled", fg_color="#E74C3C", hover_color="#C0392B")
        self.stop_btn.grid(row=2, column=0, padx=(0, 4), pady=(0, 8), sticky="ew")
        
        self.retry_btn = ctk.CTkButton(self.button_frame, text="Retry", command=self.retry_failed, fg_color="#8E44AD", hover_color="#732D91")
        self.retry_btn.grid(row=2, column=1, padx=(4, 0), pady=(0, 8), sticky="ew")

        self.schedule_btn = ctk.CTkButton(self.button_frame, text="Schedule", command=self.toggle_schedule)
        self.schedule_btn.grid(row=3, column=0, columnspan=2, pady=(0, 8), sticky="ew")

        self.captcha_btn = ctk.CTkButton(self.button_frame, text="Solve CAPTCHA", command=self.open_browser, fg_color="transparent", border_width=2, text_color=("gray10", "#DCE4EE"))
        self.captcha_btn.grid(row=4, column=0, columnspan=2, pady=(0, 8), sticky="ew")

        self.review_btn = ctk.CTkButton(self.button_frame, text="Review Pending Updates", command=self.open_review_window, fg_color="#F39C12", hover_color="#D4AC0D", text_color="black", font=ctk.CTkFont(weight="bold"))
        self.review_btn.grid(row=5, column=0, columnspan=2, pady=(0, 0), sticky="ew")

        # ==================== MAIN AREA ====================
        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(2, weight=1)

        self.stats_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.stats_frame.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        
        self.stats_row1 = ctk.CTkFrame(self.stats_frame, fg_color="transparent")
        self.stats_row1.pack(fill="x", pady=(0, 5))
        self.stats_row1.grid_columnconfigure((0, 1, 2, 3), weight=1)
        
        self.stats_row2 = ctk.CTkFrame(self.stats_frame, fg_color="transparent")
        self.stats_row2.pack(fill="x")
        self.stats_row2.grid_columnconfigure((0, 1, 2, 3), weight=1)

        def create_stat_card(parent, title, col):
            card = ctk.CTkFrame(parent, corner_radius=10)
            card.grid(row=0, column=col, padx=5, sticky="ew")
            lbl_title = ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=12, weight="bold"), text_color="gray60")
            lbl_title.pack(pady=(10, 0))
            lbl_val = ctk.CTkLabel(card, text="0", font=ctk.CTkFont(size=24, weight="bold"))
            lbl_val.pack(pady=(0, 10))
            return lbl_val

        self.total_val = create_stat_card(self.stats_row1, "TOTAL LINKS", 0)
        self.instock_val = create_stat_card(self.stats_row1, "IN-STOCK", 1)
        self.oos_val = create_stat_card(self.stats_row1, "OUT OF STOCK", 2)
        self.updates_val = create_stat_card(self.stats_row1, "PRICE UPDATES", 3)

        self.stock_update_val = create_stat_card(self.stats_row2, "STOCK UPDATES", 0)
        self.in2oos_val = create_stat_card(self.stats_row2, "IN → OOS", 1)
        self.oos2in_val = create_stat_card(self.stats_row2, "OOS → IN", 2)
        self.errors_val = create_stat_card(self.stats_row2, "ERRORS", 3)

        self.progress_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.progress_frame.grid(row=1, column=0, sticky="ew", pady=(0, 15))
        self.progress_frame.grid_columnconfigure(0, weight=1)

        self.progress_info_frame = ctk.CTkFrame(self.progress_frame, fg_color="transparent")
        self.progress_info_frame.pack(fill="x")
        
        self.progress_label = ctk.CTkLabel(self.progress_info_frame, text="Ready", font=ctk.CTkFont(size=12))
        self.progress_label.pack(side="left")
        
        self.countdown_label = ctk.CTkLabel(self.progress_info_frame, text="Next scrape: N/A", font=ctk.CTkFont(size=12), text_color="gray60")
        self.countdown_label.pack(side="right")

        self.progress = ctk.CTkProgressBar(self.progress_frame, height=10)
        self.progress.pack(fill="x", pady=(5, 0))
        self.progress.set(0)

        # ==================== LOG SECTION ====================
        self.log_frame = ctk.CTkFrame(self.main_frame)
        self.log_frame.grid(row=2, column=0, sticky="nsew", padx=5, pady=5)
        
        ctk.CTkLabel(self.log_frame, text="Live Logs", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=10, pady=(8, 4))

        self.log_text = ctk.CTkTextbox(self.log_frame, font=ctk.CTkFont(family="Consolas", size=13), wrap="word", corner_radius=10)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        self.log_text.tag_config("error", foreground="#FF6B6B")
        self.log_text.tag_config("oos", foreground="#4DABF7")
        self.log_text.tag_config("price", foreground="#FCC419")
        self.log_text.tag_config("info", foreground="#868E96")
        self.log_text.tag_config("warning", foreground="#F39C12")
        self.log_text.tag_config("hyperlink", foreground="#339AF0", underline=1)
        self.log_text.tag_bind("hyperlink", "<Button-1>", self.open_url)
        self.log_text.configure(state='disabled')

        btn_frame = ctk.CTkFrame(self.log_frame, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.copy_btn = ctk.CTkButton(
            btn_frame, 
            text="📋 Copy All Logs", 
            command=self.copy_logs_to_clipboard, 
            width=180, 
            height=35, 
            font=ctk.CTkFont(size=14, weight="bold"), 
            fg_color="#1f6aa5"
        )
        self.copy_btn.pack(side="left", padx=(0, 8))

        self.clear_btn = ctk.CTkButton(
            btn_frame, 
            text="🗑 Clear Logs", 
            command=self.clear_logs, 
            width=130, 
            height=35, 
            font=ctk.CTkFont(size=14)
        )
        self.clear_btn.pack(side="left")

    def copy_logs_to_clipboard(self):
        try:
            all_logs = self.log_text.get("1.0", "end").strip()
            if not all_logs:
                messagebox.showinfo("Copy Logs", "The log is empty!")
                return
            
            self.clipboard_clear()
            self.clipboard_append(all_logs)
            self.update()
            
            original_text = self.copy_btn.cget("text")
            self.copy_btn.configure(text="✅ Copied to Clipboard!", fg_color="green")
            self.after(2200, lambda: self.copy_btn.configure(text=original_text, fg_color="#1f6aa5"))
            
            self._write_log("📋 All logs copied to clipboard", "info")
        except Exception as e:
            messagebox.showerror("Copy Failed", f"Could not copy logs:\n{str(e)}")

    def clear_logs(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', tk.END)
        self.log_text.configure(state='disabled')
        self._write_log("🗑 Log window cleared", "info")

    def _write_log(self, message, tag="info", url=None):
        self.log_text.configure(state='normal')
        if url and tag == "hyperlink":
            self.log_text.insert(tk.END, f"{message}\n", tag)
            start_idx = self.log_text.index(tk.END + "-2l")
            end_idx = self.log_text.index(tk.END + "-1c")
            self.log_text.tag_add("hyperlink", start_idx, end_idx)
            self.log_text.tag_bind("hyperlink", "<Button-1>", lambda e, u=url: self.open_url(e, u))
        else:
            self.log_text.insert(tk.END, f"{message}\n", tag)
            
        self.log_text.see(tk.END)
        self.log_text.configure(state='disabled')

    def open_url(self, event, url=None):
        if url:
            webbrowser.open(url)
            self._write_log(f"Opened browser for {url}", "info")

    def open_review_window(self):
        verified_items = getattr(self.engine, 'pending_updates', [])
        unverified_items = []
        if hasattr(self.engine, 'validator') and hasattr(self.engine.validator, 'pending_items'):
            unverified_items = self.engine.validator.pending_items
            
        ReviewUpdatesWindow(self, verified_items, unverified_items, self.sheets_manager, self.engine, self._write_log)

    def _poll_engine_queue(self):
        try:
            while True:
                item = self.engine.queue.get_nowait()
                if item[0] == "LOG":
                    self._write_log(item[1], item[2], item[3])
                elif item[0] == "COMPLETE":
                    self.finish_checking()
                elif item[0] == "CAPTCHA_PAUSE":
                    self.handle_captcha_pause(item[1], item[2])
                elif len(item) == 10:
                    if item[4] == "Error":
                        self.failed_urls.append(item[1])
                    self.engine.process_result_item(item)
                    self.update_stats()
        except queue.Empty: 
            pass
            
        self.after(100, self._poll_engine_queue)

    def update_stats(self):
        total = self.engine.stats["total"]
        processed = self.engine.stats["processed"]
        
        self.total_val.configure(text=str(total))
        self.instock_val.configure(text=str(self.engine.stats["instock"]))
        self.oos_val.configure(text=str(self.engine.stats["oos"]))
        self.updates_val.configure(text=str(self.engine.stats["updates"]))
        self.stock_update_val.configure(text=str(self.engine.stats["stock_updates"]))
        self.in2oos_val.configure(text=str(self.engine.stats["in2oos"]))
        self.oos2in_val.configure(text=str(self.engine.stats["oos2in"]))
        self.errors_val.configure(text=str(self.engine.stats["errors"]))
        
        if total > 0:
            percent = processed / total
            self.progress.set(percent)
            self.progress_label.configure(text=f"Updating... {int(percent * 100)}%")

    def start_check(self, retry_list=None, is_scheduled=False):
        # Save all config states the moment scraping begins so nothing is lost
        self.save_all_settings()

        if not (self.scrape_web_var.get() or self.scrape_wal_var.get() or self.scrape_sg_var.get() or self.scrape_hf_var.get() or self.scrape_mn_var.get() or self.scrape_ls_var.get() or self.scrape_ce_var.get()):
            self._write_log("No suppliers selected. Please select at least one supplier.", "error")
            return
            
        self.debug_bm.headless = self.headless_var.get()
        self.engine.multi_tab_enabled = self.multi_thread_var.get()
        self.engine.review_mode = self.review_mode_var.get()
        
        if self.enable_ai_var.get():
            self.engine.ai_mode = self.ai_engine_var.get()
            self._write_log(f"Verification Engine selected: {self.engine.ai_mode}", "info")
        else:
            self.engine.ai_mode = "OFF"
            self._write_log("AI Verification (Phase 2) is DISABLED. Updates will auto-confirm.", "warning")
        
        if self.sg_debug_var.get(): 
            self.scrapers["SG"].browser_manager = self.debug_bm
        else: 
            self.scrapers["SG"].browser_manager = None
            
        self.engine.use_vpn = self.use_vpn_var.get()
            
        data = self.sheets_manager.get_all_rows()
        if not data or len(data) < 2:
            self._write_log("Sheet Error: Failed to fetch Google Sheet data or sheet is empty.", "error")
            return
            
        try: 
            start_row_limit = max(2, int(self.start_row_var.get().strip()))
        except ValueError: 
            start_row_limit = 2

        end_row_str = self.end_row_var.get().strip()
        try: 
            end_row_limit = int(end_row_str) if end_row_str else float('inf')
        except ValueError: 
            end_row_limit = float('inf')

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
            if row_num > end_row_limit:
                break
                
            if len(row) <= url_col or not row[url_col]: 
                continue
            
            url = row[url_col].strip()
            if retry_list and url not in retry_list: 
                continue

            target_var = ""
            if i > 0 and len(rows[i-1]) > variation_col: 
                target_var = str(rows[i-1][variation_col]).strip()
            elif len(row) > variation_col: 
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
            self._write_log(f"No valid URLs found within the selected row range.", "error")
            return

        if not is_scheduled and (getattr(self.engine, 'pending_updates', []) or getattr(self.engine.validator, 'pending_items', [])):
            response = messagebox.askyesno("Clear Previous Data?", "You have un-pushed updates in the Review Window from a previous run.\n\nDo you want to clear them out before starting this fresh run?")
            if response:
                self.engine.pending_updates.clear()
                if hasattr(self.engine.validator, 'pending_items'): 
                    self.engine.validator.pending_items.clear()
                if hasattr(self.engine.validator, 'pass1_data'): 
                    self.engine.validator.pass1_data.clear()
                self.engine.save_recovery_data()

        if not is_scheduled:
            self.log_text.configure(state='normal')
            self.log_text.delete('1.0', tk.END)
            self.log_text.configure(state='disabled')
            self.failed_urls.clear()
            self.captcha_urls.clear()
            
            self.engine.session_timestamp = None
            self.engine.vpn_connected = False
            self.engine.is_paused = False
            self.engine.stats = { "total": total_urls, "processed": 0, "oos": 0, "instock": 0, "updates": 0, "stock_updates": 0, "in2oos": 0, "oos2in": 0, "errors": 0 }
            self.engine.supplier_indexes = {s: 0 for s in supplier_urls.keys()}
        
        self.update_stats()
        self.progress.set(0)
        
        self.disable_main_buttons()
        self._write_log(f"Review Mode (Hold Updates): {'ON' if self.review_mode_var.get() else 'OFF'}", "info")
        
        if self.multi_thread_var.get():
            self._write_log("Initiating Concurrent Engine...", "info")
            self.progress_label.configure(text="Updating (Concurrent)...")
            self.engine.start_concurrent_scrape(supplier_urls, supplier_rows, supplier_row_nums, supplier_targets)
        else:
            self._write_log("Initiating Sequential Engine...", "info")
            self.progress_label.configure(text="Updating (Sequential)...")
            self.engine.start_sequential_scrape(supplier_urls, supplier_rows, supplier_row_nums, supplier_targets)

    def disable_main_buttons(self):
        for btn in [self.check_btn, self.thread_switch, self.headless_check, self.review_check, self.vpn_check, self.sg_debug_check, self.engine_dropdown, self.resume_btn, self.retry_btn, self.captcha_btn, self.interval_entry, self.ai_toggle, self.clear_btn, self.end_row_entry, self.start_row_entry, self.web_check, self.wal_check, self.sg_check, self.hf_check, self.mn_check, self.ls_check, self.ce_check]:
            btn.configure(state='disabled')
        for btn in [self.pause_btn, self.stop_btn]:
            btn.configure(state='normal')

    def set_paused_buttons(self):
        for btn in [self.check_btn, self.thread_switch, self.headless_check, self.review_check, self.vpn_check, self.sg_debug_check, self.engine_dropdown, self.pause_btn, self.interval_entry, self.ai_toggle, self.clear_btn, self.end_row_entry, self.start_row_entry, self.web_check, self.wal_check, self.sg_check, self.hf_check, self.mn_check, self.ls_check, self.ce_check]:
            btn.configure(state='disabled')
        for btn in [self.resume_btn, self.stop_btn, self.retry_btn, self.captcha_btn]:
            btn.configure(state='normal')

    def enable_main_buttons(self):
        for btn in [self.check_btn, self.thread_switch, self.headless_check, self.review_check, self.vpn_check, self.sg_debug_check, self.engine_dropdown, self.retry_btn, self.captcha_btn, self.interval_entry, self.ai_toggle, self.clear_btn, self.end_row_entry, self.start_row_entry, self.web_check, self.wal_check, self.sg_check, self.hf_check, self.mn_check, self.ls_check, self.ce_check]:
            btn.configure(state='normal')
        for btn in [self.pause_btn, self.resume_btn, self.stop_btn]:
            btn.configure(state='disabled')

    def pause_checking(self):
        if self.engine.is_running:
            self.engine.is_paused = True
            self._write_log("Pause requested. Waiting for active threads to finish current row...", "error")
            self.progress_label.configure(text="Pausing...")
            self.set_paused_buttons()

    def handle_captcha_pause(self, captcha_url, supplier):
        self.engine.is_paused = True
        self.captcha_urls.append(captcha_url)
        self._write_log(f"[{supplier}] CAPTCHA detected at: {captcha_url}", "error")
        self._write_log("All threads pausing. Solve the CAPTCHA in the open Brave window, then click 'Resume'.", "error")
        self.set_paused_buttons()
        self.progress_label.configure(text="Paused (CAPTCHA)")
        
    def resume_scrape(self):
        self._write_log("Resuming engine...", "info")
        self.disable_main_buttons()
        self.engine.is_paused = False
        self.progress_label.configure(text="Updating...")

    def stop_checking(self):
        self.engine.stop_requested = True
        self.engine.is_running = False
        self._write_log("Stopping threads. Please wait...", "error")
        self.enable_main_buttons()
        self.progress_label.configure(text="Checking stopped")
        
    def finish_checking(self):
        if self.engine.ai_mode != "OFF" and self.engine.validator.has_pending():
            self._write_log("Phase 1 Complete. Waiting 15s before starting Suspicion Loop (Background Verification)...", "warning")
            self.after(15000, self.engine.start_verification_phase)
            return

        self.engine.is_running = False
        self.engine.disconnect_vpn()
        self._write_log("Engine Check Completed.")
        
        if self.engine.review_mode and self.engine.pending_updates:
            summary = {}
            summary_lines = []
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            summary_lines.append(f"=== VERIFIED UPDATES SUMMARY ({timestamp}) ===")
            
            for item in self.engine.pending_updates:
                sup = item['supplier']
                summary[sup] = summary.get(sup, 0) + 1
                row = item['row_num']
                old_s, new_s = item['prev_stock'], item['stock']
                old_p, new_p = item['prev_price'], item['price']
                url = item['url']
                summary_lines.append(f"Row {row:<4} [{sup}] | Stock: {old_s} -> {new_s} | Price: {old_p} -> {new_p} | URL: {url}")
            
            self._write_log("\n--- FINAL VERIFIED UPDATES ---", "warning")
            for sup, count in summary.items():
                self._write_log(f"[{sup}]: {count} verified update(s) waiting in Review Window.", "price")
            self._write_log("Click 'Review Pending Updates' to push them to your Google Sheet.", "info")
            self._write_log("------------------------------\n", "warning")

            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                file_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                log_file = os.path.join(LOG_DIR, f"verified_updates_{file_timestamp}.txt")
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(summary_lines))
                self._write_log(f"Saved master update summary to {log_file}", "info")
            except Exception as e:
                self._write_log(f"Failed to write summary log file: {e}", "error")

        self.progress_label.configure(text="Updated")
        self.enable_main_buttons()
        if self.debug_bm: 
            self.debug_bm.quit()

    def retry_failed(self):
        if not self.failed_urls:
            self._write_log("No failed URLs to retry.", "info")
            return
        self._write_log(f"Retrying {len(self.failed_urls)} failed URLs...", "info")
        self.start_check(retry_list=self.failed_urls)

    def open_browser(self):
        urls = self.captcha_urls or self.failed_urls
        if urls:
            webbrowser.open(urls[-1])
            self._write_log(f"Opened default browser for {urls[-1]}", "info")
            self._write_log("Fix the CAPTCHA in the automated browser window if it's still open, then click Resume.", "error")

    def toggle_schedule(self):
        if self.is_scheduling: 
            self.stop_schedule()
        else: 
            self.schedule_scrape()

    def schedule_scrape(self):
        try:
            interval = int(self.schedule_interval.get())
            if interval < 120:
                self._write_log("Interval must be 120 minutes or more.", "error")
                return
        except ValueError:
            self._write_log("Please enter a valid number of minutes in interval.", "error")
            return
            
        self.is_scheduling = True
        self.countdown_seconds = interval * 60
        self.schedule_btn.configure(text="Stop Schedule", fg_color="#E74C3C", hover_color="#C0392B")
        
        self.start_check(is_scheduled=True)
        self.countdown_thread = threading.Thread(target=self.countdown_worker, daemon=True)
        self.countdown_thread.start()
        self._write_log(f"Scheduled scraping started with {interval} minute interval.", "info")

    def countdown_worker(self):
        while self.is_scheduling and not self.engine.stop_requested:
            if self.countdown_seconds > 0:
                minutes, seconds = divmod(self.countdown_seconds, 60)
                self.after(0, self.countdown_label.configure, {"text": f"Next scrape: {minutes:02d}:{seconds:02d}"})
                if self.countdown_seconds == 300:
                    self.engine.queue.put(("LOG", "5 minutes until next scrape. Save the log if needed, as it will be cleared.", "price", None))
                self.countdown_seconds -= 1
                time.sleep(1)
            else:
                self.countdown_seconds = int(self.schedule_interval.get()) * 60
                self.after(0, self.trigger_scheduled_check)

    def trigger_scheduled_check(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', tk.END)
        self.log_text.configure(state='disabled')
        self.start_check(is_scheduled=True)

    def stop_schedule(self):
        self.is_scheduling = False
        self.countdown_seconds = 0
        self.countdown_label.configure(text="Next scrape: N/A")
        self.schedule_btn.configure(text="Schedule", fg_color="#3498DB", hover_color="#2980B9")
        self._write_log("Scheduled scraping stopped.", "error")
        self.stop_checking()

if __name__ == "__main__":
    app = StockPriceCheckerApp()
    app.mainloop()