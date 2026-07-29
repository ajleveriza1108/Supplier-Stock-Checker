import atexit
import os
import queue
import sys
import threading
import time
import webbrowser
from datetime import datetime


def fix_import_paths():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = script_dir
    possible_paths = [
        project_root,
        os.path.abspath(os.path.join(script_dir, "..")),
        os.path.abspath(os.path.join(script_dir, "../..")),
        os.path.join(project_root, "scrapers"),
        os.path.join(project_root, "core"),
        os.path.join(project_root, "ui"),
    ]

    for path in possible_paths:
        if path and path not in sys.path:
            sys.path.insert(0, path)

    if "scrapers" not in sys.modules:
        try:
            import scrapers  # noqa: F401
        except ImportError:
            pass

    if not hasattr(fix_import_paths, "already_logged"):
        print(
            f"✅ Import paths fixed for app.py. Project root: {project_root}"
        )
        fix_import_paths.already_logged = True


fix_import_paths()

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox

from config.settings import LOG_DIR, SHEET_COLS
from core.config_manager import ConfigManager
from core.engine import ScrapingEngine
from core.sheets import GoogleSheetsManager
from scrapers.browser import BraveDebugManager
from scrapers.scrapling_browser import ScraplingBrowserManager
from scrapers.collectionsetc import CollectionsEtcScraper
from scrapers.harbor_freight import HarborFreightScraper
from scrapers.lakeside import LakesideScraper
from scrapers.menards import MenardsScraper
from scrapers.sportsmans_guide import SportsmansGuideScraper
from scrapers.walmart import WalmartScraper
from scrapers.walmart_scrapling import WalmartScraplingScraper
from scrapers.webstaurant import WebstaurantScraper
from ui.log_color_policy import (
    LogSeverity,
    classify_log_text,
    merge_severity,
    severity_tag,
)
from ui.review_window import ReviewUpdatesWindow
from core.local_ai_config import LocalAIConfig
from ui.local_ai_settings import LocalAISettingsDialog


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
            self.after(0, lambda: self.state("zoomed"))
        except Exception:
            pass

        self.config_manager = ConfigManager()
        self.sheets_manager = GoogleSheetsManager()
        self.debug_bm = BraveDebugManager(port=9222)
        initial_scrapling_headless = bool(
            self.config_manager.get(
                "scrapling_headless",
                self.config_manager.get(
                    "walmart_scrapling_headless",
                    self.config_manager.get("headless", True),
                ),
            )
        )
        self.scrapling_bm = ScraplingBrowserManager(
            project_root=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            headless=initial_scrapling_headless,
            logger_func=self._write_log,
        )
        self.walmart_scraper = WalmartScraplingScraper(
            headless=initial_scrapling_headless,
            logger_func=self._write_log,
            selenium_fallback=None,
        )
        self.scrapers = {
            "WEB": WebstaurantScraper(
                browser_manager=self.scrapling_bm.for_supplier("WEB"),
                use_physical_browser=True,
            ),
            "WAL": self.walmart_scraper,
            "SG": SportsmansGuideScraper(browser_manager=None),
            "HF": HarborFreightScraper(
                browser_manager=self.scrapling_bm.for_supplier("HF")
            ),
            "MN": MenardsScraper(
                browser_manager=self.scrapling_bm.for_supplier("MN")
            ),
            "LS": LakesideScraper(
                browser_manager=self.scrapling_bm.for_supplier("LS")
            ),
            "CE": CollectionsEtcScraper(
                browser_manager=self.scrapling_bm.for_supplier("CE")
            ),
        }
        self.engine = ScrapingEngine(
            self.sheets_manager,
            self.config_manager,
            self.scrapers,
        )

        self.start_row_var = ctk.StringVar(
            value=str(self.config_manager.get("start_row", "2"))
        )
        self.end_row_var = ctk.StringVar(
            value=str(self.config_manager.get("end_row", ""))
        )
        self.schedule_interval = ctk.StringVar(
            value=str(self.config_manager.get("schedule_interval", "120"))
        )
        self.multi_thread_var = ctk.BooleanVar(
            value=self.config_manager.get("multi_thread", False)
        )
        self.review_mode_var = ctk.BooleanVar(
            value=self.config_manager.get("review_mode", True)
        )
        self.enable_ai_var = ctk.BooleanVar(
            value=self.config_manager.get("enable_ai", True)
        )
        self.ai_engine_var = ctk.StringVar(
            value="Regular (Regex)"
        )
        self.headless_var = ctk.BooleanVar(
            value=initial_scrapling_headless
        )
        self.use_vpn_var = ctk.BooleanVar(
            value=self.config_manager.get("use_vpn", True)
        )
        self.sg_debug_var = ctk.BooleanVar(
            value=self.config_manager.get("sg_debug", False)
        )
        self.scrape_web_var = ctk.BooleanVar(
            value=self.config_manager.get("scrape_web", False)
        )
        self.scrape_wal_var = ctk.BooleanVar(
            value=self.config_manager.get("scrape_wal", False)
        )
        self.scrape_sg_var = ctk.BooleanVar(
            value=self.config_manager.get("scrape_sg", True)
        )
        self.scrape_hf_var = ctk.BooleanVar(
            value=self.config_manager.get("scrape_hf", False)
        )
        self.scrape_mn_var = ctk.BooleanVar(
            value=self.config_manager.get("scrape_mn", False)
        )
        self.scrape_ls_var = ctk.BooleanVar(
            value=self.config_manager.get("scrape_ls", True)
        )
        self.scrape_ce_var = ctk.BooleanVar(
            value=self.config_manager.get("scrape_ce", True)
        )

        self.captcha_urls = []
        self.failed_urls = []
        self.is_scheduling = False
        self.countdown_seconds = 0

        self.setup_ui()
        self.after(
            250,
            self._refresh_local_ai_status,
        )
        self.after(100, self._poll_engine_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        atexit.register(self.cleanup_on_exit)

    def save_all_settings(self):
        """Save the current state of all UI settings."""
        try:
            settings = {
                "start_row": self.start_row_var.get(),
                "end_row": self.end_row_var.get(),
                "schedule_interval": self.schedule_interval.get(),
                "multi_thread": self.multi_thread_var.get(),
                "review_mode": self.review_mode_var.get(),
                "enable_ai": self.enable_ai_var.get(),
                "ai_engine": self.ai_engine_var.get(),
                "headless": self.headless_var.get(),
                "scrapling_headless": self.headless_var.get(),
                "use_vpn": self.use_vpn_var.get(),
                "sg_debug": self.sg_debug_var.get(),
                "scrape_web": self.scrape_web_var.get(),
                "scrape_wal": self.scrape_wal_var.get(),
                "scrape_sg": self.scrape_sg_var.get(),
                "scrape_hf": self.scrape_hf_var.get(),
                "scrape_mn": self.scrape_mn_var.get(),
                "scrape_ls": self.scrape_ls_var.get(),
                "scrape_ce": self.scrape_ce_var.get(),
            }
            for key, value in settings.items():
                self.config_manager.save_config(key, value)
        except Exception as exc:
            print(f"Failed to save UI settings: {exc}")

    def on_closing(self):
        self.cleanup_on_exit()
        self.destroy()

    def cleanup_on_exit(self):
        self.save_all_settings()
        try:
            if hasattr(self, "engine"):
                self.engine.save_recovery_data()
                if hasattr(self.engine, "cleanup_vpn"):
                    self.engine.cleanup_vpn()

            if hasattr(self, "walmart_scraper") and self.walmart_scraper:
                self.walmart_scraper.close()
            if hasattr(self, "scrapling_bm") and self.scrapling_bm:
                self.scrapling_bm.close()
            if hasattr(self, "debug_bm") and self.debug_bm:
                self.debug_bm.quit()
        except Exception:
            pass

    @staticmethod
    def validate_number_input(new_value):
        return new_value == "" or new_value.isdigit()

    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar_frame = ctk.CTkFrame(
            self,
            width=260,
            corner_radius=0,
        )
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(4, weight=1)

        self.logo_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="STOCK CHECKER",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.logo_label.grid(row=0, column=0, padx=20, pady=(15, 5))

        self.supplier_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="Target Suppliers:",
            anchor="w",
        )
        self.supplier_label.grid(
            row=1,
            column=0,
            padx=20,
            pady=(5, 0),
            sticky="w",
        )

        self.supplier_frame = ctk.CTkFrame(
            self.sidebar_frame,
            fg_color="transparent",
        )
        self.supplier_frame.grid(
            row=2,
            column=0,
            padx=15,
            pady=0,
            sticky="ew",
        )
        self.supplier_frame.grid_columnconfigure((0, 1), weight=1)

        self.web_check = ctk.CTkCheckBox(
            self.supplier_frame,
            text="Webstaurant",
            variable=self.scrape_web_var,
        )
        self.web_check.grid(
            row=0,
            column=0,
            pady=(5, 0),
            padx=5,
            sticky="w",
        )

        self.ls_check = ctk.CTkCheckBox(
            self.supplier_frame,
            text="Lakeside",
            variable=self.scrape_ls_var,
        )
        self.ls_check.grid(
            row=0,
            column=1,
            pady=(5, 0),
            padx=5,
            sticky="w",
        )

        self.wal_check = ctk.CTkCheckBox(
            self.supplier_frame,
            text="Walmart",
            variable=self.scrape_wal_var,
        )
        self.wal_check.grid(
            row=1,
            column=0,
            pady=(5, 0),
            padx=5,
            sticky="w",
        )

        self.ce_check = ctk.CTkCheckBox(
            self.supplier_frame,
            text="Collections Etc.",
            variable=self.scrape_ce_var,
        )
        self.ce_check.grid(
            row=1,
            column=1,
            pady=(5, 0),
            padx=5,
            sticky="w",
        )

        self.sg_check = ctk.CTkCheckBox(
            self.supplier_frame,
            text="Sportsman's G.",
            variable=self.scrape_sg_var,
        )
        self.sg_check.grid(
            row=2,
            column=0,
            columnspan=2,
            pady=(5, 0),
            padx=5,
            sticky="w",
        )

        self.hf_check = ctk.CTkCheckBox(
            self.supplier_frame,
            text="Harbor Freight",
            variable=self.scrape_hf_var,
        )
        self.hf_check.grid(
            row=3,
            column=0,
            columnspan=2,
            pady=(5, 0),
            padx=5,
            sticky="w",
        )

        self.mn_check = ctk.CTkCheckBox(
            self.supplier_frame,
            text="Menards",
            variable=self.scrape_mn_var,
        )
        self.mn_check.grid(
            row=4,
            column=0,
            columnspan=2,
            pady=(5, 10),
            padx=5,
            sticky="w",
        )

        self.settings_frame = ctk.CTkFrame(
            self.sidebar_frame,
            fg_color="transparent",
        )
        self.settings_frame.grid(
            row=3,
            column=0,
            padx=15,
            pady=0,
            sticky="ew",
        )

        validation_command = (
            self.register(self.validate_number_input),
            "%P",
        )

        ctk.CTkLabel(
            self.settings_frame,
            text="Start Row:",
        ).grid(row=0, column=0, sticky="w")
        self.start_row_entry = ctk.CTkEntry(
            self.settings_frame,
            textvariable=self.start_row_var,
            width=60,
            height=24,
            validate="key",
            validatecommand=validation_command,
        )
        self.start_row_entry.grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(
            self.settings_frame,
            text="End Row:",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.end_row_entry = ctk.CTkEntry(
            self.settings_frame,
            textvariable=self.end_row_var,
            width=60,
            height=24,
            validate="key",
            validatecommand=validation_command,
            placeholder_text="All",
        )
        self.end_row_entry.grid(
            row=1,
            column=1,
            sticky="e",
            pady=(2, 0),
        )

        ctk.CTkLabel(
            self.settings_frame,
            text="Interval (m):",
        ).grid(row=2, column=0, sticky="w", pady=(2, 0))
        self.interval_entry = ctk.CTkEntry(
            self.settings_frame,
            textvariable=self.schedule_interval,
            width=60,
            height=24,
            validate="key",
            validatecommand=validation_command,
        )
        self.interval_entry.grid(
            row=2,
            column=1,
            sticky="e",
            pady=(2, 0),
        )

        self.ai_toggle = ctk.CTkSwitch(
            self.settings_frame,
            text="Local AI Cross-Check Changes",
            variable=self.enable_ai_var,
            progress_color="#1ABC9C",
            switch_width=32,
            switch_height=16,
        )
        self.ai_toggle.grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(8, 0),
        )

        self.local_ai_setup_btn = ctk.CTkButton(
            self.settings_frame,
            text="Configure Local AI",
            command=self.open_local_ai_settings,
            height=26,
            fg_color="#34495E",
            hover_color="#1ABC9C",
        )
        self.local_ai_setup_btn.grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(5, 3),
        )

        self.local_ai_status_var = ctk.StringVar(
            value="Local AI: checking…"
        )
        self.local_ai_status_label = ctk.CTkLabel(
            self.settings_frame,
            textvariable=self.local_ai_status_var,
            justify="left",
            wraplength=210,
            text_color="gray65",
            font=ctk.CTkFont(size=11),
        )
        self.local_ai_status_label.grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 5),
        )

        self.headless_check = ctk.CTkCheckBox(
            self.settings_frame,
            text="Scrapling Browser: Headless",
            variable=self.headless_var,
            command=self._apply_scrapling_mode,
            checkbox_width=16,
            checkbox_height=16,
        )
        self.headless_check.grid(
            row=6,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(5, 0),
        )

        self.vpn_check = ctk.CTkCheckBox(
            self.settings_frame,
            text="Surfshark VPN (SG/HF/CE)",
            variable=self.use_vpn_var,
            checkbox_width=16,
            checkbox_height=16,
            text_color="#1ABC9C",
        )
        self.vpn_check.grid(
            row=7,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(5, 0),
        )

        self.sg_debug_check = ctk.CTkCheckBox(
            self.settings_frame,
            text="SG: Use Chrome Debug",
            variable=self.sg_debug_var,
            checkbox_width=16,
            checkbox_height=16,
            text_color="#3498DB",
        )
        self.sg_debug_check.grid(
            row=8,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(5, 0),
        )

        self.thread_switch = ctk.CTkSwitch(
            self.settings_frame,
            text="Concurrent",
            variable=self.multi_thread_var,
            progress_color="#8E44AD",
            switch_width=32,
            switch_height=16,
        )
        self.thread_switch.grid(
            row=9,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(10, 0),
        )

        self.review_check = ctk.CTkCheckBox(
            self.settings_frame,
            text="Review Mode (Hold Updates)",
            variable=self.review_mode_var,
            checkbox_width=16,
            checkbox_height=16,
            text_color="#F1C40F",
        )
        self.review_check.grid(
            row=10,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(10, 0),
        )

        self.button_frame = ctk.CTkFrame(
            self.sidebar_frame,
            fg_color="transparent",
        )
        self.button_frame.grid(
            row=5,
            column=0,
            padx=15,
            pady=(0, 20),
            sticky="ew",
        )
        self.button_frame.grid_columnconfigure((0, 1), weight=1)

        self.check_btn = ctk.CTkButton(
            self.button_frame,
            text="Start Scraping",
            command=self.start_check,
            fg_color="#2FA572",
            hover_color="#1F7A50",
        )
        self.check_btn.grid(
            row=0,
            column=0,
            columnspan=2,
            pady=(0, 8),
            sticky="ew",
        )

        self.pause_btn = ctk.CTkButton(
            self.button_frame,
            text="Pause",
            command=self.pause_checking,
            state="disabled",
            fg_color="#E67E22",
            hover_color="#D35400",
        )
        self.pause_btn.grid(
            row=1,
            column=0,
            padx=(0, 4),
            pady=(0, 8),
            sticky="ew",
        )

        self.resume_btn = ctk.CTkButton(
            self.button_frame,
            text="Resume",
            command=self.resume_scrape,
            state="disabled",
        )
        self.resume_btn.grid(
            row=1,
            column=1,
            padx=(4, 0),
            pady=(0, 8),
            sticky="ew",
        )

        self.stop_btn = ctk.CTkButton(
            self.button_frame,
            text="Stop",
            command=self.stop_checking,
            state="disabled",
            fg_color="#E74C3C",
            hover_color="#C0392B",
        )
        self.stop_btn.grid(
            row=2,
            column=0,
            padx=(0, 4),
            pady=(0, 8),
            sticky="ew",
        )

        self.retry_btn = ctk.CTkButton(
            self.button_frame,
            text="Retry",
            command=self.retry_failed,
            fg_color="#8E44AD",
            hover_color="#732D91",
        )
        self.retry_btn.grid(
            row=2,
            column=1,
            padx=(4, 0),
            pady=(0, 8),
            sticky="ew",
        )

        self.schedule_btn = ctk.CTkButton(
            self.button_frame,
            text="Schedule",
            command=self.toggle_schedule,
        )
        self.schedule_btn.grid(
            row=3,
            column=0,
            columnspan=2,
            pady=(0, 8),
            sticky="ew",
        )

        self.captcha_btn = ctk.CTkButton(
            self.button_frame,
            text="Solve CAPTCHA",
            command=self.open_browser,
            fg_color="transparent",
            border_width=2,
            text_color=("gray10", "#DCE4EE"),
        )
        self.captcha_btn.grid(
            row=4,
            column=0,
            columnspan=2,
            pady=(0, 8),
            sticky="ew",
        )

        self.review_btn = ctk.CTkButton(
            self.button_frame,
            text="Review Pending Updates",
            command=self.open_review_window,
            fg_color="#F39C12",
            hover_color="#D4AC0D",
            text_color="black",
            font=ctk.CTkFont(weight="bold"),
        )
        self.review_btn.grid(
            row=5,
            column=0,
            columnspan=2,
            pady=(0, 0),
            sticky="ew",
        )

        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=20,
            pady=20,
        )
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(2, weight=1)

        self.stats_frame = ctk.CTkFrame(
            self.main_frame,
            fg_color="transparent",
        )
        self.stats_frame.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 15),
        )

        self.stats_row1 = ctk.CTkFrame(
            self.stats_frame,
            fg_color="transparent",
        )
        self.stats_row1.pack(fill="x", pady=(0, 5))
        self.stats_row1.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.stats_row2 = ctk.CTkFrame(
            self.stats_frame,
            fg_color="transparent",
        )
        self.stats_row2.pack(fill="x")
        self.stats_row2.grid_columnconfigure((0, 1, 2, 3), weight=1)

        def create_stat_card(parent, title, column):
            card = ctk.CTkFrame(parent, corner_radius=10)
            card.grid(row=0, column=column, padx=5, sticky="ew")

            title_label = ctk.CTkLabel(
                card,
                text=title,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="gray60",
            )
            title_label.pack(pady=(10, 0))

            value_label = ctk.CTkLabel(
                card,
                text="0",
                font=ctk.CTkFont(size=24, weight="bold"),
            )
            value_label.pack(pady=(0, 10))
            return value_label

        self.total_val = create_stat_card(
            self.stats_row1,
            "TOTAL LINKS",
            0,
        )
        self.instock_val = create_stat_card(
            self.stats_row1,
            "IN-STOCK",
            1,
        )
        self.oos_val = create_stat_card(
            self.stats_row1,
            "OUT OF STOCK",
            2,
        )
        self.updates_val = create_stat_card(
            self.stats_row1,
            "PRICE UPDATES",
            3,
        )
        self.stock_update_val = create_stat_card(
            self.stats_row2,
            "STOCK UPDATES",
            0,
        )
        self.in2oos_val = create_stat_card(
            self.stats_row2,
            "IN → OOS",
            1,
        )
        self.oos2in_val = create_stat_card(
            self.stats_row2,
            "OOS → IN",
            2,
        )
        self.errors_val = create_stat_card(
            self.stats_row2,
            "ERRORS",
            3,
        )

        self.progress_frame = ctk.CTkFrame(
            self.main_frame,
            fg_color="transparent",
        )
        self.progress_frame.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(0, 15),
        )
        self.progress_frame.grid_columnconfigure(0, weight=1)

        self.progress_info_frame = ctk.CTkFrame(
            self.progress_frame,
            fg_color="transparent",
        )
        self.progress_info_frame.pack(fill="x")

        self.progress_label = ctk.CTkLabel(
            self.progress_info_frame,
            text="Ready",
            font=ctk.CTkFont(size=12),
        )
        self.progress_label.pack(side="left")

        self.countdown_label = ctk.CTkLabel(
            self.progress_info_frame,
            text="Next scrape: N/A",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
        )
        self.countdown_label.pack(side="right")

        self.progress = ctk.CTkProgressBar(
            self.progress_frame,
            height=10,
        )
        self.progress.pack(fill="x", pady=(5, 0))
        self.progress.set(0)

        self.log_frame = ctk.CTkFrame(self.main_frame)
        self.log_frame.grid(
            row=2,
            column=0,
            sticky="nsew",
            padx=5,
            pady=5,
        )

        ctk.CTkLabel(
            self.log_frame,
            text="Live Logs",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(8, 4))

        self.log_text = ctk.CTkTextbox(
            self.log_frame,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="word",
            corner_radius=10,
        )
        self.log_text.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=(0, 8),
        )

        self.log_text.tag_config("error", foreground="#FF6B6B")
        self.log_text.tag_config("oos", foreground="#4DABF7")
        self.log_text.tag_config("price", foreground="#FCC419")
        self.log_text.tag_config("info", foreground="#868E96")
        self.log_text.tag_config("warning", foreground="#F39C12")
        self.log_text.tag_config(
            "hyperlink",
            foreground="#339AF0",
            underline=1,
        )
        self.log_text.tag_bind(
            "hyperlink",
            "<Button-1>",
            self.open_url,
        )
        self.log_text.configure(state="disabled")

        log_button_frame = ctk.CTkFrame(
            self.log_frame,
            fg_color="transparent",
        )
        log_button_frame.pack(
            fill="x",
            padx=10,
            pady=(0, 10),
        )

        self.copy_btn = ctk.CTkButton(
            log_button_frame,
            text=" Copy All Logs",
            command=self.copy_logs_to_clipboard,
            width=180,
            height=35,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#1f6aa5",
        )
        self.copy_btn.pack(side="left", padx=(0, 8))

        self.clear_btn = ctk.CTkButton(
            log_button_frame,
            text=" Clear Logs",
            command=self.clear_logs,
            width=130,
            height=35,
            font=ctk.CTkFont(size=14),
        )
        self.clear_btn.pack(side="left")

    def open_local_ai_settings(self):
        LocalAISettingsDialog(
            self,
            on_saved=self._refresh_local_ai_status,
        )

    def _refresh_local_ai_status(self):
        config = LocalAIConfig.load()
        ready, message = config.readiness()

        if config.enabled and ready:
            status = (
                "Local AI: Ready — "
                f"{config.resolved_model_path.name}"
            )
        elif config.enabled:
            status = f"Local AI: Needs setup — {message}"
        else:
            status = "Local AI: Disabled"

        if hasattr(self, "local_ai_status_var"):
            self.local_ai_status_var.set(status)

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
            self.copy_btn.configure(
                text="✅ Copied to Clipboard!",
                fg_color="green",
            )
            self.after(
                2200,
                lambda: self.copy_btn.configure(
                    text=original_text,
                    fg_color="#1f6aa5",
                ),
            )
            self._write_log(" All logs copied to clipboard", "info")
        except Exception as exc:
            messagebox.showerror(
                "Copy Failed",
                f"Could not copy logs:\n{exc}",
            )

    def clear_logs(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")
        self._write_log(" Log window cleared", "info")

    def _write_log(self, message, tag="info", url=None):
        """
        Write one log message and color the complete row block.

        White: no change.
        Orange: a stock or price change was detected.
        Red: an error, conflict, block, or unverifiable result occurred.
        """
        text = "" if message is None else str(message)

        if not getattr(self, "_ssc_log_colors_ready", False):
            self.log_text.tag_config(
                "ssc_normal",
                foreground="#F5F5F5",
            )
            self.log_text.tag_config(
                "ssc_change",
                foreground="#F39C12",
            )
            self.log_text.tag_config(
                "ssc_error",
                foreground="#FF4D4F",
            )
            self._ssc_log_colors_ready = True
            self._ssc_log_block_start = None
            self._ssc_log_block_severity = LogSeverity.NORMAL
            self._ssc_log_link_counter = 0

        lines = text.splitlines() or [text]
        starts_row = any(
            line.lstrip().casefold().startswith("row:")
            for line in lines
        )
        ends_block = any(
            len(line.strip()) >= 20
            and set(line.strip()) == {"-"}
            for line in lines
        )

        incoming_severity = classify_log_text(text)
        if str(tag).casefold() == "error":
            incoming_severity = LogSeverity.ERROR

        self.log_text.configure(state="normal")
        insert_start = self.log_text.index("end-1c")

        if starts_row:
            self._ssc_log_block_start = insert_start
            self._ssc_log_block_severity = LogSeverity.NORMAL

        self.log_text.insert(tk.END, f"{text}\n")
        insert_end = self.log_text.index("end-1c")

        if self._ssc_log_block_start is not None:
            self._ssc_log_block_severity = merge_severity(
                self._ssc_log_block_severity,
                incoming_severity,
            )
            paint_start = self._ssc_log_block_start
            paint_tag = severity_tag(self._ssc_log_block_severity)
        else:
            paint_start = insert_start
            paint_tag = severity_tag(incoming_severity)

        for color_tag in (
            "ssc_normal",
            "ssc_change",
            "ssc_error",
        ):
            self.log_text.tag_remove(
                color_tag,
                paint_start,
                insert_end,
            )

        self.log_text.tag_add(
            paint_tag,
            paint_start,
            insert_end,
        )

        if url:
            self._ssc_log_link_counter += 1
            link_tag = f"ssc_link_{self._ssc_log_link_counter}"
            self.log_text.tag_config(link_tag, underline=1)

            url_text = str(url)
            offset = text.find(url_text)
            if offset >= 0:
                link_start = f"{insert_start}+{offset}c"
                link_end = f"{link_start}+{len(url_text)}c"
            else:
                link_start = insert_start
                link_end = insert_end

            self.log_text.tag_add(
                link_tag,
                link_start,
                link_end,
            )
            self.log_text.tag_bind(
                link_tag,
                "<Button-1>",
                lambda event, target=url_text: self.open_url(
                    event,
                    target,
                ),
            )

        self.log_text.tag_raise("ssc_normal")
        self.log_text.tag_raise("ssc_change")
        self.log_text.tag_raise("ssc_error")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

        if ends_block:
            self._ssc_log_block_start = None
            self._ssc_log_block_severity = LogSeverity.NORMAL

    def open_url(self, event=None, url=None):
        """Open a clicked product link without adding noise to the log."""
        target = str(url or "").strip()
        if not target:
            return

        try:
            opened = webbrowser.open(target)
            if not opened:
                self._write_log(
                    f"ERROR: Browser could not open this link: {target}",
                    "error",
                )
        except Exception as exc:
            self._write_log(
                f"ERROR: Could not open browser for {target}: {exc}",
                "error",
            )

    def open_review_window(self):
        verified_items = getattr(self.engine, "pending_updates", [])
        unverified_items = []

        if hasattr(self.engine, "validator") and hasattr(
            self.engine.validator,
            "pending_items",
        ):
            unverified_items = self.engine.validator.pending_items

        ReviewUpdatesWindow(
            self,
            verified_items,
            unverified_items,
            self.sheets_manager,
            self.engine,
            self._write_log,
        )

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
        self.instock_val.configure(
            text=str(self.engine.stats["instock"])
        )
        self.oos_val.configure(text=str(self.engine.stats["oos"]))
        self.updates_val.configure(
            text=str(self.engine.stats["updates"])
        )
        self.stock_update_val.configure(
            text=str(self.engine.stats["stock_updates"])
        )
        self.in2oos_val.configure(
            text=str(self.engine.stats["in2oos"])
        )
        self.oos2in_val.configure(
            text=str(self.engine.stats["oos2in"])
        )
        self.errors_val.configure(
            text=str(self.engine.stats["errors"])
        )

        if total > 0:
            percent = processed / total
            self.progress.set(percent)
            self.progress_label.configure(
                text=f"Updating... {int(percent * 100)}%"
            )

    def start_check(self, retry_list=None, is_scheduled=False):
        self.save_all_settings()

        supplier_selected = any(
            variable.get()
            for variable in (
                self.scrape_web_var,
                self.scrape_wal_var,
                self.scrape_sg_var,
                self.scrape_hf_var,
                self.scrape_mn_var,
                self.scrape_ls_var,
                self.scrape_ce_var,
            )
        )
        if not supplier_selected:
            self._write_log(
                "No suppliers selected. Please select at least one supplier.",
                "error",
            )
            return

        self.debug_bm.headless = self.headless_var.get()
        self.engine.multi_tab_enabled = self.multi_thread_var.get()
        self.engine.review_mode = self.review_mode_var.get()

        if self.enable_ai_var.get():
            local_ai_config = LocalAIConfig.load()
            local_ai_config.enabled = True
            local_ai_config.save()

            # Any value other than OFF queues changed rows
            # for the existing Phase 2 workflow. The regex
            # mode also prevents the old AI scraper bypass.
            self.engine.ai_mode = "Regular (Regex)"

            ready, detail = local_ai_config.readiness()
            if ready:
                self._write_log(
                    "Local AI change cross-check enabled: "
                    f"{local_ai_config.resolved_model_path.name}",
                    "info",
                )
            else:
                self._write_log(
                    "Local AI is enabled but needs setup: "
                    f"{detail} Changed rows will remain "
                    "held for review.",
                    "warning",
                )
        else:
            local_ai_config = LocalAIConfig.load()
            local_ai_config.enabled = False
            local_ai_config.save()
            self.engine.ai_mode = "OFF"
            self._write_log(
                "Local AI change cross-check is disabled.",
                "info",
            )

        if self.sg_debug_var.get():
            self.scrapers["SG"].browser_manager = self.debug_bm
        else:
            self.scrapers["SG"].browser_manager = None

        self.engine.use_vpn = self.use_vpn_var.get()

        data = self.sheets_manager.get_all_rows()
        if not data or len(data) < 2:
            self._write_log(
                "Sheet Error: Failed to fetch Google Sheet data "
                "or sheet is empty.",
                "error",
            )
            return

        try:
            start_row_limit = max(
                2,
                int(self.start_row_var.get().strip()),
            )
        except ValueError:
            start_row_limit = 2

        end_row_text = self.end_row_var.get().strip()
        try:
            end_row_limit = (
                int(end_row_text)
                if end_row_text
                else float("inf")
            )
        except ValueError:
            end_row_limit = float("inf")

        rows = data[1:]
        supplier_codes = ("WEB", "WAL", "SG", "HF", "MN", "LS", "CE")
        supplier_urls = {code: [] for code in supplier_codes}
        supplier_rows = {code: [] for code in supplier_codes}
        supplier_row_nums = {code: [] for code in supplier_codes}
        supplier_targets = {code: [] for code in supplier_codes}

        url_col = SHEET_COLS.get("url", 4)
        variation_col = SHEET_COLS.get("variation", 8)

        for index, row in enumerate(rows):
            row_num = index + 2

            if row_num < start_row_limit:
                continue
            if row_num > end_row_limit:
                break
            if len(row) <= url_col or not row[url_col]:
                continue

            url = row[url_col].strip()
            if retry_list and url not in retry_list:
                continue

            target_variation = ""
            if index > 0 and len(rows[index - 1]) > variation_col:
                target_variation = str(
                    rows[index - 1][variation_col]
                ).strip()
            elif len(row) > variation_col:
                target_variation = str(row[variation_col]).strip()

            supplier = None
            lower_url = url.lower()
            if (
                self.scrape_web_var.get()
                and "webstaurantstore.com" in lower_url
            ):
                supplier = "WEB"
            elif self.scrape_wal_var.get() and "walmart.com" in lower_url:
                supplier = "WAL"
            elif (
                self.scrape_sg_var.get()
                and "sportsmansguide.com" in lower_url
            ):
                supplier = "SG"
            elif (
                self.scrape_hf_var.get()
                and "harborfreight.com" in lower_url
            ):
                supplier = "HF"
            elif self.scrape_mn_var.get() and "menards.com" in lower_url:
                supplier = "MN"
            elif self.scrape_ls_var.get() and "lakeside.com" in lower_url:
                supplier = "LS"
            elif (
                self.scrape_ce_var.get()
                and "collectionsetc.com" in lower_url
            ):
                supplier = "CE"

            if supplier is None:
                continue

            supplier_urls[supplier].append(url)
            supplier_rows[supplier].append(row)
            supplier_row_nums[supplier].append(row_num)
            supplier_targets[supplier].append(target_variation)

        total_urls = sum(len(urls) for urls in supplier_urls.values())
        if total_urls == 0:
            self._write_log(
                "No valid URLs found within the selected row range.",
                "error",
            )
            return

        has_pending_updates = bool(
            getattr(self.engine, "pending_updates", [])
        )
        has_pending_validation = bool(
            getattr(self.engine.validator, "pending_items", [])
        )

        if (
            not is_scheduled
            and (has_pending_updates or has_pending_validation)
        ):
            response = messagebox.askyesno(
                "Clear Previous Data?",
                "You have un-pushed updates in the Review Window from a "
                "previous run.\n\nDo you want to clear them out before "
                "starting this fresh run?",
            )
            if response:
                self.engine.pending_updates.clear()
                if hasattr(self.engine.validator, "pending_items"):
                    self.engine.validator.pending_items.clear()
                if hasattr(self.engine.validator, "pass1_data"):
                    self.engine.validator.pass1_data.clear()
                self.engine.save_recovery_data()

        if not is_scheduled:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", tk.END)
            self.log_text.configure(state="disabled")

        self.failed_urls.clear()
        self.captcha_urls.clear()
        self.engine.session_timestamp = None
        self.engine.vpn_connected = False
        self.engine.is_paused = False
        self.engine.stats = {
            "total": total_urls,
            "processed": 0,
            "oos": 0,
            "instock": 0,
            "updates": 0,
            "stock_updates": 0,
            "in2oos": 0,
            "oos2in": 0,
            "errors": 0,
        }
        self.engine.supplier_indexes = {
            supplier: 0 for supplier in supplier_urls
        }

        self.update_stats()
        self.progress.set(0)
        self.disable_main_buttons()
        self._write_log(
            "Review Mode (Hold Updates): "
            f"{'ON' if self.review_mode_var.get() else 'OFF'}",
            "info",
        )

        if self.multi_thread_var.get():
            self._write_log("Initiating Concurrent Engine...", "info")
            self.progress_label.configure(
                text="Updating (Concurrent)..."
            )
            self.engine.start_concurrent_scrape(
                supplier_urls,
                supplier_rows,
                supplier_row_nums,
                supplier_targets,
            )
        else:
            self._write_log("Initiating Sequential Engine...", "info")
            self.progress_label.configure(
                text="Updating (Sequential)..."
            )
            self.engine.start_sequential_scrape(
                supplier_urls,
                supplier_rows,
                supplier_row_nums,
                supplier_targets,
            )

    def disable_main_buttons(self):
        controls = [
            self.check_btn,
            self.thread_switch,
            self.headless_check,
            self.review_check,
            self.vpn_check,
            self.sg_debug_check,
            self.local_ai_setup_btn,
            self.resume_btn,
            self.retry_btn,
            self.captcha_btn,
            self.interval_entry,
            self.ai_toggle,
            self.clear_btn,
            self.end_row_entry,
            self.start_row_entry,
            self.web_check,
            self.wal_check,
            self.sg_check,
            self.hf_check,
            self.mn_check,
            self.ls_check,
            self.ce_check,
        ]
        for control in controls:
            control.configure(state="disabled")

        for control in (self.pause_btn, self.stop_btn):
            control.configure(state="normal")

    def set_paused_buttons(self):
        controls = [
            self.check_btn,
            self.thread_switch,
            self.headless_check,
            self.review_check,
            self.vpn_check,
            self.sg_debug_check,
            self.local_ai_setup_btn,
            self.pause_btn,
            self.interval_entry,
            self.ai_toggle,
            self.clear_btn,
            self.end_row_entry,
            self.start_row_entry,
            self.web_check,
            self.wal_check,
            self.sg_check,
            self.hf_check,
            self.mn_check,
            self.ls_check,
            self.ce_check,
        ]
        for control in controls:
            control.configure(state="disabled")

        for control in (
            self.resume_btn,
            self.stop_btn,
            self.retry_btn,
            self.captcha_btn,
        ):
            control.configure(state="normal")

    def enable_main_buttons(self):
        controls = [
            self.check_btn,
            self.thread_switch,
            self.headless_check,
            self.review_check,
            self.vpn_check,
            self.sg_debug_check,
            self.local_ai_setup_btn,
            self.retry_btn,
            self.captcha_btn,
            self.interval_entry,
            self.ai_toggle,
            self.clear_btn,
            self.end_row_entry,
            self.start_row_entry,
            self.web_check,
            self.wal_check,
            self.sg_check,
            self.hf_check,
            self.mn_check,
            self.ls_check,
            self.ce_check,
        ]
        for control in controls:
            control.configure(state="normal")

        for control in (
            self.pause_btn,
            self.resume_btn,
            self.stop_btn,
        ):
            control.configure(state="disabled")

    def pause_checking(self):
        if self.engine.is_running:
            self.engine.is_paused = True
            self._write_log(
                "Pause requested. Waiting for active threads to finish "
                "current row...",
                "error",
            )
            self.progress_label.configure(text="Pausing...")
            self.set_paused_buttons()

    def handle_captcha_pause(self, captcha_url, supplier):
        self.engine.is_paused = True
        self.captcha_urls.append(captcha_url)
        self._write_log(
            f"[{supplier}] CAPTCHA detected at: {captcha_url}",
            "error",
        )
        self._write_log(
            "All threads pausing. Click 'Solve CAPTCHA' to open the saved "
            "supplier session, close it when finished, then click Resume.",
            "error",
        )
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
        if (
            self.engine.ai_mode != "OFF"
            and self.engine.validator.has_pending()
        ):
            self._write_log(
                "Phase 1 Complete. Waiting 15s before starting Suspicion "
                "Loop (Background Verification)...",
                "warning",
            )
            self.after(15000, self.engine.start_verification_phase)
            return

        self.engine.is_running = False
        self.engine.disconnect_vpn()
        self._write_log("Engine Check Completed.")

        if self.engine.review_mode and self.engine.pending_updates:
            summary = {}
            summary_lines = []
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            summary_lines.append(
                f"=== VERIFIED UPDATES SUMMARY ({timestamp}) ==="
            )

            for item in self.engine.pending_updates:
                supplier = item["supplier"]
                summary[supplier] = summary.get(supplier, 0) + 1
                row = item["row_num"]
                old_stock = item["prev_stock"]
                new_stock = item["stock"]
                old_price = item["prev_price"]
                new_price = item["price"]
                url = item["url"]
                summary_lines.append(
                    f"Row {row:<4} [{supplier}] | "
                    f"Stock: {old_stock} -> {new_stock} | "
                    f"Price: {old_price} -> {new_price} | URL: {url}"
                )

            self._write_log(
                "\n--- FINAL VERIFIED UPDATES ---",
                "warning",
            )
            for supplier, count in summary.items():
                self._write_log(
                    f"[{supplier}]: {count} verified update(s) waiting "
                    "in Review Window.",
                    "price",
                )
            self._write_log(
                "Click 'Review Pending Updates' to push them to your "
                "Google Sheet.",
                "info",
            )
            self._write_log(
                "------------------------------\n",
                "warning",
            )

            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                file_timestamp = datetime.now().strftime(
                    "%Y%m%d_%H%M%S"
                )
                log_file = os.path.join(
                    LOG_DIR,
                    f"verified_updates_{file_timestamp}.txt",
                )
                with open(log_file, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(summary_lines))
                self._write_log(
                    f"Saved master update summary to {log_file}",
                    "info",
                )
            except Exception as exc:
                self._write_log(
                    f"Failed to write summary log file: {exc}",
                    "error",
                )

        self.progress_label.configure(text="Updated")
        self.enable_main_buttons()

        if self.debug_bm:
            self.debug_bm.quit()

    def retry_failed(self):
        if not self.failed_urls:
            self._write_log("No failed URLs to retry.", "info")
            return

        self._write_log(
            f"Retrying {len(self.failed_urls)} failed URLs...",
            "info",
        )
        self.start_check(retry_list=self.failed_urls)

    def _apply_scrapling_mode(self, save=True):
        headless = bool(self.headless_var.get())
        self.scrapling_bm.set_headless(headless)
        self.walmart_scraper.set_headless(headless)
        if save:
            self.config_manager.save_config("scrapling_headless", headless)
            self.config_manager.save_config("headless", headless)
            self._write_log(
                "Scrapling mode changed to "
                + ("Headless (hidden)." if headless else "Physical browser (visible).")
                + " Applies to every supplier except Sportsman's Guide.",
                "info",
            )

    def open_browser(self):
        urls = self.captcha_urls or self.failed_urls
        target_url = urls[-1] if urls else "https://www.walmart.com/"
        lowered = target_url.lower()
        if "sportsmansguide.com" in lowered:
            webbrowser.open(target_url)
            self._write_log(
                "Sportsman's Guide keeps its existing browser path. Complete "
                "the verification there, then click Resume.",
                "warning",
            )
            return
        opened, message = self.scrapling_bm.open_manual_verification(target_url)
        self._write_log(message, "info" if opened else "warning")
        if opened:
            self._write_log(
                "Solve the CAPTCHA or sign in, close the visible Scrapling "
                "window, then click Resume.",
                "warning",
            )

    def toggle_schedule(self):
        if self.is_scheduling:
            self.stop_schedule()
        else:
            self.schedule_scrape()

    def schedule_scrape(self):
        try:
            interval = int(self.schedule_interval.get())
            if interval < 120:
                self._write_log(
                    "Interval must be 120 minutes or more.",
                    "error",
                )
                return
        except ValueError:
            self._write_log(
                "Please enter a valid number of minutes in interval.",
                "error",
            )
            return

        self.is_scheduling = True
        self.countdown_seconds = interval * 60
        self.schedule_btn.configure(
            text="Stop Schedule",
            fg_color="#E74C3C",
            hover_color="#C0392B",
        )
        self.start_check(is_scheduled=True)

        self.countdown_thread = threading.Thread(
            target=self.countdown_worker,
            daemon=True,
        )
        self.countdown_thread.start()
        self._write_log(
            f"Scheduled scraping started with {interval} minute interval.",
            "info",
        )

    def countdown_worker(self):
        while self.is_scheduling and not self.engine.stop_requested:
            if self.countdown_seconds > 0:
                minutes, seconds = divmod(self.countdown_seconds, 60)
                self.after(
                    0,
                    self.countdown_label.configure,
                    {
                        "text": (
                            f"Next scrape: {minutes:02d}:{seconds:02d}"
                        )
                    },
                )

                if self.countdown_seconds == 300:
                    self.engine.queue.put(
                        (
                            "LOG",
                            "5 minutes until next scrape. Save the log if "
                            "needed, as it will be cleared.",
                            "price",
                            None,
                        )
                    )

                self.countdown_seconds -= 1
                time.sleep(1)
            else:
                self.countdown_seconds = (
                    int(self.schedule_interval.get()) * 60
                )
                self.after(0, self.trigger_scheduled_check)

    def trigger_scheduled_check(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")
        self.start_check(is_scheduled=True)

    def stop_schedule(self):
        self.is_scheduling = False
        self.countdown_seconds = 0
        self.countdown_label.configure(text="Next scrape: N/A")
        self.schedule_btn.configure(
            text="Schedule",
            fg_color="#3498DB",
            hover_color="#2980B9",
        )
        self._write_log("Scheduled scraping stopped.", "error")
        self.stop_checking()


if __name__ == "__main__":
    app = StockPriceCheckerApp()
    app.mainloop()
