"""
This file holds all the static configuration variables for the Stock Price Checker.
Changing colors, adding a new supplier, or updating the Google Sheet URL 
can all be done directly from here.
"""
import sys
import os

# --- Project Root (for relative paths) ---
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Google Sheets Configuration ---
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1KflGzPLIszDAxyiaoMJBOkmI_7JAmow2fFB2s_-H56c/edit?usp=sharing"
SHEET_NAME = "breathingspace"
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets', 
    'https://www.googleapis.com/auth/drive'
]
SERVICE_ACCOUNT_FILE = "service_account.json"

# --- Supplier Configuration ---
# FIXED: Added "LS" (Lakeside) to maintain consistency
SUPPLIER_ORDER = ["WEB", "WAL", "SG", "CE", "HF", "MN", "LS"]

SUPPLIER_COLORS = {
    'WAL': '#ffd966',
    'WEB': '#8fce00',
    'SG': '#f6b26b',
    'CE': '#c27ba0',
    'HF': '#e06666',
    'MN': '#93c47d',
    'LS': '#76a5af'
}

# --- UI Theme Configuration ---
THEME_COLORS = {
    'bg': '#0f0f0f',
    'fg': '#e2e2e2',
    'entry_bg': '#1a1a1a',
    'entry_fg': '#ffffff',
    'log_bg': '#141414',
    'log_fg': '#cccccc',
    'button_bg': '#242424',
    'button_fg': '#ffffff',
    'button_active_bg': '#333333',
    'button_active_fg': '#ffffff',
    'disabled_fg': '#4a4a4a',
    'accent': '#ffffff'
}

# --- CENTRALIZED BROWSER PATHS ---
BRAVE_PATHS = {
    "win32": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "darwin": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "linux": "/usr/bin/brave-browser"
}
CURRENT_BRAVE_PATH = BRAVE_PATHS.get(sys.platform, BRAVE_PATHS["win32"])

BRAVE_DEBUG_USER_DATA_DIR = os.path.join(PROJECT_ROOT, "browser_profiles", "brave_debug")
BRAVE_UC_USER_DATA_DIR = os.path.join(PROJECT_ROOT, "browser_profiles", "brave_uc")

# --- CENTRALIZED VPN CONFIGURATION ---
OPENVPN_EXE_PATH = r"C:\Program Files\OpenVPN\bin\openvpn.exe" if sys.platform == "win32" else "/usr/sbin/openvpn"
VPN_DIR = os.path.join(PROJECT_ROOT, "vpn")
VPN_CONFIG_PATH = os.path.join(VPN_DIR, "surfshark.ovpn")
VPN_AUTH_PATH = os.path.join(VPN_DIR, "auth.txt")
VPN_LOG_PATH = os.path.join(VPN_DIR, "vpn_log.txt")

# --- Decoupled Spreadsheet Columns ---
SHEET_COLS = {
    "status_note": 0,
    "date_updated": 1,
    "stock_status": 2,
    "url": 4,
    "variation": 8,
    "price": 15
}

# --- Anti-Bot Configuration ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15"
]

PROXY = None

# --- LOG CONFIGURATION: Separate file per supplier ---
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

# DYNAMIC LOG FIX: These are now base paths without extensions. The engine will append the timestamp and .txt
SUPPLIER_LOG_FILES = {
    "SG": os.path.join(LOG_DIR, "sg_changes"),
    "WAL": os.path.join(LOG_DIR, "walmart_changes"),
    "HF": os.path.join(LOG_DIR, "harborfreight_changes"),
    "WEB": os.path.join(LOG_DIR, "webstaurant_changes"),
    "MN": os.path.join(LOG_DIR, "menards_changes"),
    "LS": os.path.join(LOG_DIR, "lakeside_changes"),
    "CE": os.path.join(LOG_DIR, "collectionsetc_changes")
}

# --- Self-awareness: Ensure required folders exist ---
os.makedirs(BRAVE_DEBUG_USER_DATA_DIR, exist_ok=True)
os.makedirs(BRAVE_UC_USER_DATA_DIR, exist_ok=True)
os.makedirs(VPN_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ============================================================================
# NEW: SPORTSMANS GUIDE BROWSER MODE SETTING
# ============================================================================

# SG_USE_PHYSICAL_BROWSER:
#   True  → Use physical browser via browser_manager (remote debugging port 9222)
#           Faster, fewer CAPTCHA triggers, better for production
#   False → Use Selenium headless Chrome (launches its own instance)
#           Self-contained, no external browser needed, slower
SG_USE_PHYSICAL_BROWSER = True