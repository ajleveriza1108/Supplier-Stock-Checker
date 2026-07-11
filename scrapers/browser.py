import os
import sys
import time
import socket
import subprocess
import threading
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from selenium.common.exceptions import WebDriverException, TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

try:
    from scrapling import StealthyFetcher
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False

# Windows-specific enforced paths
ENFORCED_BRAVE_PATH = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
ENFORCED_USER_DATA_DIR = r"C:\Temp\BraveDebugProfile"


class ScraplingManager:
    """
    Manages the Scrapling StealthyFetcher (Playwright-based).
    Shared instance to conserve RAM and bypass protections like PerimeterX.
    """
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.fetcher = None
        self.lock = threading.Lock()

    def get_fetcher(self):
        with self.lock:
            if self.fetcher is None:
                if not HAS_SCRAPLING:
                    print("❌ Scrapling not installed. Run dependencies.py.")
                    return None
                print("⚡ Launching Scrapling StealthyFetcher (Playwright)...")
                self.fetcher = StealthyFetcher(headless=self.headless)
            return self.fetcher

    def restart_browser(self):
        with self.lock:
            self.quit()
            print("⚡ Restarting Scrapling StealthyFetcher...")
            if HAS_SCRAPLING:
                self.fetcher = StealthyFetcher(headless=self.headless)

    def quit(self):
        self.fetcher = None


class BraveDebugManager:
    """
    Manages Brave Browser with remote debugging on Windows.
    Automatically launches Brave if not already running.
    """
    def __init__(self, port: int = 9222, headless: bool = False):
        self.port = port
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None
        self.browser_process: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        
        self.user_data_dir = ENFORCED_USER_DATA_DIR
        os.makedirs(self.user_data_dir, exist_ok=True)

    def _is_debugger_running(self) -> bool:
        """Check if Brave is already listening on debug port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(('127.0.0.1', self.port))
                return True
            except (ConnectionRefusedError, socket.timeout, OSError):
                return False

    def _launch_browser_process(self):
        """Launch Brave with remote debugging if not already running."""
        if self._is_debugger_running():
            print(f"✅ Brave Debugger already running on port {self.port}")
            return

        print(f"⚡ Launching Brave Browser in debug mode (port {self.port})...")

        cmd = [
            ENFORCED_BRAVE_PATH,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-notifications",
            "--disable-extensions"
        ]

        if self.headless:
            cmd.extend(["--headless=new", "--disable-gpu"])
        else:
            cmd.extend(["--start-maximized"])

        try:
            creation_flags = subprocess.CREATE_NO_WINDOW
            self.browser_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags
            )
            time.sleep(4)
            print("✅ Brave launched successfully in debug mode.")
        except FileNotFoundError:
            print(f"❌ Brave not found at: {ENFORCED_BRAVE_PATH}")
        except Exception as e:
            print(f"❌ Failed to launch Brave: {e}")

    def get_driver(self) -> webdriver.Chrome:
        """Return Selenium driver attached to Brave debug session."""
        with self.lock:
            if self.driver:
                try:
                    self.driver.current_url
                    return self.driver
                except WebDriverException:
                    self.driver = None

            self._launch_browser_process()

            options = Options()
            options.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.port}")
            options.add_argument("--disable-blink-features=AutomationControlled")

            try:
                os.environ['WDM_LOG'] = '0'
                
                # THE TRUE FIX: Tell the manager to track Brave's actual version dynamically, not Chrome's.
                service = Service(ChromeDriverManager().install())
                
                self.driver = webdriver.Chrome(service=service, options=options)
                self.driver.set_page_load_timeout(45)
                print("✅ Selenium successfully attached to Brave.")
                return self.driver
            except Exception as e:
                print(f"❌ Failed to attach Selenium to Brave: {e}")
                raise e

    def wait_for_js_interactive(self, timeout: int = 8):
        try:
            driver = self.get_driver()
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
            )
        except (TimeoutException, WebDriverException):
            pass

    def get_new_tab(self) -> str:
        driver = self.get_driver()
        driver.execute_script("window.open('');")
        return driver.window_handles[-1]

    def switch_to_tab(self, handle: str):
        driver = self.get_driver()
        if handle in driver.window_handles:
            driver.switch_to.window(handle)

    def close_tab(self, handle: str):
        driver = self.get_driver()
        if handle in driver.window_handles:
            driver.switch_to.window(handle)
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])

    def restart_browser(self):
        with self.lock:
            self.quit_driver_only()
            if self.browser_process:
                try:
                    self.browser_process.terminate()
                    self.browser_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.browser_process.kill()
                self.browser_process = None
            time.sleep(2)
            self._launch_browser_process()

    def quit_driver_only(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def quit(self):
        self.quit_driver_only()
        if self.browser_process:
            try:
                self.browser_process.terminate()
            except Exception:
                pass
            self.browser_process = None