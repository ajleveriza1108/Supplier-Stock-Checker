# Stock & Price Checker Dashboard

An advanced, multi-threaded, self-healing e-commerce web scraping automation tool. This application monitors product prices and stock availability across multiple retail suppliers, intelligently verifies changes using a "Suspicion Loop" paired with Local AI, and seamlessly synchronizes the final, user-approved data with Google Sheets.

---

## 🌟 Key Features

### 1. Multi-Supplier Architecture
- Supports automated scraping for: **Walmart**, **Sportsman's Guide**, **Harbor Freight**, **WebstaurantStore**, **Menards**, **Lakeside**, and **Collections Etc.**
- Modular scraper design allowing easy additions of new suppliers.

### 2. Advanced Anti-Bot Evasion & Aggressive Extraction
- **Scrapling / Playwright Integration:** Bypasses PerimeterX (HUMAN) and Cloudflare seamlessly without the need for manual `time.sleep()` delays. Used primarily for Walmart.
- **Brave Remote Debugging:** Hooks into a live Brave browser session (Port 9222) to bypass browser-fingerprinting protections.
- **Aggressive Price Hunting:** Bypasses UI text like "See Member Price in Cart" by ripping prices directly from hidden JSON-LD and Meta Schema tags (highly effective for Sportsman's Guide).

### 3. Suspicion Loop & AI Verification (Two-Phase Check)
- **Phase 1 (Regex/DOM):** Detects an initial change in stock (e.g., In-Stock → OOS) or price. Flags the item as "Suspicious" and places it in a holding queue.
- **Phase 2 (Background Verification):** Retries the scrape 15 seconds later. If the Regex confirms the change again, it calls upon local LLMs (via **Ollama**) such as *Qwen2.5* or *DeepSeek-R1* to read the raw HTML text and definitively confirm the scraper didn't hallucinate. Features "Limbo-Bug" protection so items remain visible in the UI during this background check.

### 4. Tabbed Review Command Center (Dry-Run)
- Protects the live Google Sheet from bad data. Verified updates are held in a **Review Window**.
- **Supplier Tabs:** Updates are cleanly compartmentalized by supplier (e.g., WAL, MN, SG) using a tabbed interface.
- **Executive Control:** You can manually edit the Date, Stock Status, and Price in the UI before clicking "Push Selected to Sheet".
- **Live Unlock Monitor:** The Push button securely unlocks the exact millisecond background active threads finish processing.

### 5. Bulletproof Crash Recovery
- **State Persistence:** The engine actively saves a snapshot of the *Verified*, *Unverified*, and *Historical Phase 1* queues to `config/crash_recovery.json`.
- If the app crashes, your computer restarts, or you accidentally close the window, relaunching the app will instantly restore your pending updates back to the Review Window so no API calls or AI processing time is wasted.

### 6. Dynamic AI Prompting (`ai_prompts.json`)
- The AI's logic is fully configurable without touching Python code.
- Update overarching rules (e.g., "For Walmart, ignore 3rd party sellers") directly in `config/ai_prompts.json` and the AI adapts immediately on the next scrape.

### 7. Integrated VPN Management
- Automatically hooks into the OpenVPN CLI to route traffic through **Surfshark**.
- Auto-rotates `.ovpn` configurations and uses "Smart Polling" to verify the exit node IP before proceeding with the scrape.

### 8. Self-Bootstrapping Environment
- The `main.py` entry point acts as an **Auto-VENV Launcher**. It detects if a virtual environment exists, builds it if missing, silently installs/upgrades dependencies via `dependencies.py` (including Playwright browsers), and securely launches the Tkinter application.

---

## 📂 Project Structure

```text
stock_checker/
│
├── .venv_[HOSTNAME]/               # Auto-generated isolated Python environment
├── browser_profiles/               # Sandbox data for persistent logins/cookies
├── config/
│   ├── settings.py                 # Centralized configuration, paths, and UI theme colors
│   ├── ai_prompts.json             # Dynamic supplier-specific AI rules
│   └── crash_recovery.json         # Auto-save state cache (Generated dynamically)
├── core/
│   ├── base_scraper.py             # Abstract class standardizing all scrapers
│   ├── config_manager.py           # Handles reading/writing app_config.json
│   ├── engine.py                   # The ScrapingEngine (Threading, Queues, SQLite, Crash Recovery)
│   ├── sheets.py                   # Google Sheets API wrapper with exponential backoff
│   ├── validator.py                # The SuspicionValidator (Phase 2 cross-referencing logic)
│   └── vpn_manager.py              # Surfshark OpenVPN CLI wrapper
├── logs/                           # Auto-generated localized logs (e.g., walmart_changes_[TIMESTAMP].txt)
├── scrapers/
│   ├── browser.py                  # ScraplingManager & BraveDebugManager
│   ├── ce_price_parser.py          # Shopify JSON & DOM Parser for Collections Etc.
│   ├── collectionsetc.py           # Collections Etc. scraper
│   ├── harbor_freight.py           # Harbor Freight scraper
│   ├── lakeside.py                 # Lakeside scraper
│   ├── menards.py                  # Menards scraper
│   ├── sg_price_parser.py          # Price/Stock evaluation logic for Sportsman's Guide
│   ├── sportsmans_guide.py         # Sportsman's Guide scraper (Aggressive JSON-LD hunter)
│   ├── walmart.py                  # Scrapling-powered Walmart Scraper
│   ├── walmart_doublecheck.py      # Walmart DoubleCheck wrapper
│   └── webstaurant.py              # WebstaurantStore scraper
├── ui/
│   ├── app.py                      # Main customtkinter GUI Dashboard
│   └── review_window.py            # Tabbed UI for reviewing and pushing updates
├── vpn/                            # Directory containing auth.txt and .ovpn configurations
│
├── Launch Stock Checker.bat        # Windows Batch script to request Admin & launch main.py
├── main.py                         # Environment bootstrapper
├── dependencies.py                 # Package manager and environment stabilization
├── smart_tools.py                  # Ollama AI matcher, Prompts loader, and Captcha solvers
├── requirements.txt                # Required PyPI packages
├── scraper_cache.db                # Local SQLite database for offline state tracking
└── service_account.json            # Google Service Account Credentials