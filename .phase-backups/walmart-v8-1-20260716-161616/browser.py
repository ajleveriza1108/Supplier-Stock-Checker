from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

try:
    from scrapling import StealthyFetcher

    HAS_SCRAPLING = True
except ImportError:
    StealthyFetcher = None
    HAS_SCRAPLING = False


ENFORCED_BRAVE_PATH = Path(
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
)
ENFORCED_USER_DATA_DIR = Path(r"C:\Temp\BraveDebugProfile")

CHROME_FOR_TESTING_METADATA_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "known-good-versions-with-downloads.json"
)

APP_CACHE_DIR = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "SupplierStockChecker"
)
DRIVER_CACHE_DIR = APP_CACHE_DIR / "drivers"


class ScraplingManager:
    """
    Manages one shared Scrapling StealthyFetcher instance.
    """

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self.fetcher = None
        self.lock = threading.RLock()

    def get_fetcher(self):
        with self.lock:
            if self.fetcher is None:
                if not HAS_SCRAPLING:
                    print(
                        "❌ Scrapling is not installed. "
                        "Run the dependency installer.",
                        flush=True,
                    )
                    return None

                print(
                    "⚡ Launching Scrapling StealthyFetcher...",
                    flush=True,
                )
                self.fetcher = StealthyFetcher(
                    headless=self.headless
                )

            return self.fetcher

    def restart_browser(self) -> None:
        with self.lock:
            self.quit()
            print(
                "⚡ Restarting Scrapling StealthyFetcher...",
                flush=True,
            )

            if HAS_SCRAPLING:
                self.fetcher = StealthyFetcher(
                    headless=self.headless
                )

    def quit(self) -> None:
        with self.lock:
            self.fetcher = None


class BraveDebugManager:
    """
    Launches or attaches to a dedicated Brave debugging profile.

    ChromeDriver is resolved from Chrome for Testing metadata using the
    Chromium version reported by Brave's DevTools endpoint.
    """

    def __init__(
        self,
        port: int = 9222,
        headless: bool = False,
    ) -> None:
        self.port = port
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None
        self.browser_process: Optional[subprocess.Popen] = None

        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()

        self.user_data_dir = ENFORCED_USER_DATA_DIR
        self.user_data_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        DRIVER_CACHE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

    @property
    def debugger_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _is_debugger_running(self) -> bool:
        with socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        ) as connection:
            connection.settimeout(0.5)

            try:
                connection.connect(
                    ("127.0.0.1", self.port)
                )
                return True
            except (
                ConnectionRefusedError,
                socket.timeout,
                OSError,
            ):
                return False

    def _wait_for_debugger(
        self,
        timeout: float = 15.0,
    ) -> None:
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._is_debugger_running():
                try:
                    self._get_debugger_metadata(
                        timeout=2.0
                    )
                    return
                except Exception:
                    pass

            time.sleep(0.25)

        raise TimeoutError(
            "Brave started, but its remote-debugging "
            f"endpoint did not become ready on port {self.port}."
        )

    def _launch_browser_process(self) -> None:
        if self._is_debugger_running():
            print(
                f"✅ Brave Debugger already running "
                f"on port {self.port}",
                flush=True,
            )
            return

        if not ENFORCED_BRAVE_PATH.is_file():
            raise FileNotFoundError(
                f"Brave was not found at: "
                f"{ENFORCED_BRAVE_PATH}"
            )

        print(
            f"⚡ Launching Brave in debug mode "
            f"on port {self.port}...",
            flush=True,
        )

        command = [
            str(ENFORCED_BRAVE_PATH),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-notifications",
            "--disable-extensions",
        ]

        if self.headless:
            command.extend(
                [
                    "--headless=new",
                    "--disable-gpu",
                ]
            )
        else:
            command.append("--start-maximized")

        creation_flags = getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        )

        self.browser_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )

        self._wait_for_debugger()

        print(
            "✅ Brave launched successfully in debug mode.",
            flush=True,
        )

    def _get_debugger_metadata(
        self,
        timeout: float = 5.0,
    ) -> dict:
        request = urllib.request.Request(
            f"{self.debugger_url}/json/version",
            headers={
                "User-Agent": "SupplierStockChecker/1.0"
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            payload = response.read()

        metadata = json.loads(
            payload.decode("utf-8")
        )

        if not metadata.get("Browser"):
            raise RuntimeError(
                "The Brave debugger did not report "
                "a browser version."
            )

        return metadata

    @staticmethod
    def _parse_browser_version(
        metadata: dict,
    ) -> str:
        browser_text = str(
            metadata.get("Browser", "")
        ).strip()

        if "/" not in browser_text:
            raise RuntimeError(
                "Unexpected debugger Browser value: "
                f"{browser_text!r}"
            )

        version = browser_text.split(
            "/",
            1,
        )[1].strip()

        parts = version.split(".")

        if len(parts) < 4 or not all(
            part.isdigit()
            for part in parts[:4]
        ):
            raise RuntimeError(
                "Unexpected Brave Chromium version: "
                f"{version!r}"
            )

        return ".".join(parts[:4])

    @staticmethod
    def _version_tuple(
        version: str,
    ) -> tuple[int, ...]:
        try:
            return tuple(
                int(part)
                for part in version.split(".")
            )
        except ValueError:
            return tuple()

    @staticmethod
    def _driver_platform() -> str:
        if os.name != "nt":
            raise RuntimeError(
                "This Brave manager currently supports "
                "Windows only."
            )

        machine = platform.machine().lower()

        if "64" in machine:
            return "win64"

        return "win32"

    @staticmethod
    def _download_json(
        url: str,
        timeout: float = 20.0,
    ) -> dict:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "SupplierStockChecker/1.0"
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            payload = response.read()

        return json.loads(
            payload.decode("utf-8")
        )

    def _select_driver_download(
        self,
        browser_version: str,
    ) -> tuple[str, str]:
        print(
            "🔎 Resolving a matching ChromeDriver "
            f"for Brave Chromium {browser_version}...",
            flush=True,
        )

        metadata = self._download_json(
            CHROME_FOR_TESTING_METADATA_URL
        )

        versions = metadata.get(
            "versions",
            [],
        )

        platform_name = self._driver_platform()
        build_prefix = (
            ".".join(
                browser_version.split(".")[:3]
            )
            + "."
        )
        major_prefix = (
            browser_version.split(".")[0]
            + "."
        )

        exact_candidates = [
            item
            for item in versions
            if item.get("version") == browser_version
        ]

        build_candidates = [
            item
            for item in versions
            if str(
                item.get("version", "")
            ).startswith(build_prefix)
        ]

        milestone_candidates = [
            item
            for item in versions
            if str(
                item.get("version", "")
            ).startswith(major_prefix)
        ]

        candidates = (
            exact_candidates
            or build_candidates
            or milestone_candidates
        )

        candidates = sorted(
            candidates,
            key=lambda item: self._version_tuple(
                str(item.get("version", ""))
            ),
            reverse=True,
        )

        for candidate in candidates:
            downloads = (
                candidate
                .get("downloads", {})
                .get("chromedriver", [])
            )

            for download in downloads:
                if (
                    download.get("platform")
                    == platform_name
                    and download.get("url")
                ):
                    selected_version = str(
                        candidate["version"]
                    )

                    if not selected_version.startswith(
                        build_prefix
                    ):
                        print(
                            "⚠️ No exact build match was listed; "
                            f"using milestone-compatible driver "
                            f"{selected_version}.",
                            flush=True,
                        )

                    return (
                        selected_version,
                        str(download["url"]),
                    )

        raise RuntimeError(
            "Chrome for Testing did not provide a "
            f"{platform_name} ChromeDriver for "
            f"Brave Chromium {browser_version}."
        )

    @staticmethod
    def _driver_executable_name() -> str:
        return (
            "chromedriver.exe"
            if os.name == "nt"
            else "chromedriver"
        )

    def _download_driver(
        self,
        driver_version: str,
        download_url: str,
    ) -> Path:
        destination_dir = (
            DRIVER_CACHE_DIR
            / driver_version
            / self._driver_platform()
        )
        destination_path = (
            destination_dir
            / self._driver_executable_name()
        )

        if destination_path.is_file():
            print(
                f"✅ Using cached ChromeDriver "
                f"{driver_version}",
                flush=True,
            )
            return destination_path

        print(
            f"⬇️ Downloading ChromeDriver "
            f"{driver_version}...",
            flush=True,
        )

        destination_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        with tempfile.TemporaryDirectory(
            prefix="ssc_chromedriver_"
        ) as temporary_directory:
            temporary_path = Path(
                temporary_directory
            )
            archive_path = (
                temporary_path
                / "chromedriver.zip"
            )

            request = urllib.request.Request(
                download_url,
                headers={
                    "User-Agent": (
                        "SupplierStockChecker/1.0"
                    )
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=45,
            ) as response:
                with archive_path.open("wb") as archive:
                    shutil.copyfileobj(
                        response,
                        archive,
                    )

            with zipfile.ZipFile(
                archive_path
            ) as archive:
                archive.extractall(
                    temporary_path / "extracted"
                )

            matches = list(
                (
                    temporary_path
                    / "extracted"
                ).rglob(
                    self._driver_executable_name()
                )
            )

            if not matches:
                raise RuntimeError(
                    "The downloaded driver archive did "
                    "not contain chromedriver."
                )

            shutil.copy2(
                matches[0],
                destination_path,
            )

        print(
            f"✅ ChromeDriver cached at: "
            f"{destination_path}",
            flush=True,
        )

        return destination_path

    def _resolve_driver_path(
        self,
        browser_version: str,
    ) -> Path:
        version_file = (
            DRIVER_CACHE_DIR
            / f"brave-{browser_version}.txt"
        )

        if version_file.is_file():
            cached_path = Path(
                version_file.read_text(
                    encoding="utf-8"
                ).strip()
            )

            if cached_path.is_file():
                print(
                    f"✅ Using cached ChromeDriver: "
                    f"{cached_path}",
                    flush=True,
                )
                return cached_path

        driver_version, download_url = (
            self._select_driver_download(
                browser_version
            )
        )

        driver_path = self._download_driver(
            driver_version,
            download_url,
        )

        version_file.write_text(
            str(driver_path),
            encoding="utf-8",
        )

        return driver_path

    @staticmethod
    def _describe_driver(
        driver_path: Path,
    ) -> str:
        try:
            completed = subprocess.run(
                [
                    str(driver_path),
                    "--version",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            output = (
                completed.stdout
                or completed.stderr
                or ""
            ).strip()

            return output or str(driver_path)
        except Exception:
            return str(driver_path)

    def get_driver(self) -> webdriver.Chrome:
        """
        Return a Selenium driver attached to the Brave debug session.
        """
        with self.lock:
            if self.driver is not None:
                try:
                    _ = self.driver.current_url
                    return self.driver
                except WebDriverException:
                    self.driver = None

            self._launch_browser_process()

            debugger_metadata = (
                self._get_debugger_metadata()
            )
            browser_version = (
                self._parse_browser_version(
                    debugger_metadata
                )
            )

            print(
                f"✅ Brave reports Chromium "
                f"{browser_version}",
                flush=True,
            )

            driver_path = (
                self._resolve_driver_path(
                    browser_version
                )
            )

            print(
                "✅ Driver selected: "
                f"{self._describe_driver(driver_path)}",
                flush=True,
            )

            options = Options()
            options.binary_location = str(
                ENFORCED_BRAVE_PATH
            )
            options.add_experimental_option(
                "debuggerAddress",
                f"127.0.0.1:{self.port}",
            )
            options.add_argument(
                "--disable-blink-features="
                "AutomationControlled"
            )

            service = Service(
                executable_path=str(driver_path)
            )

            print(
                "🔗 Starting ChromeDriver and "
                "attaching Selenium to Brave...",
                flush=True,
            )

            try:
                self.driver = webdriver.Chrome(
                    service=service,
                    options=options,
                )
                self.driver.set_page_load_timeout(
                    45
                )

                print(
                    "✅ Selenium successfully "
                    "attached to Brave.",
                    flush=True,
                )

                return self.driver

            except Exception as exc:
                self.driver = None

                raise RuntimeError(
                    "Selenium could not attach to Brave. "
                    f"Brave Chromium: {browser_version}. "
                    f"Driver: {driver_path}. "
                    f"Original error: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    def wait_for_js_interactive(
        self,
        timeout: int = 8,
    ) -> None:
        try:
            driver = self.get_driver()

            WebDriverWait(
                driver,
                timeout,
            ).until(
                lambda current_driver: (
                    current_driver.execute_script(
                        "return document.readyState"
                    )
                    in {
                        "interactive",
                        "complete",
                    }
                )
            )
        except (
            TimeoutException,
            WebDriverException,
        ):
            pass

    def get_new_tab(self) -> str:
        with self.operation_lock:
            driver = self.get_driver()
            before = set(driver.window_handles)

            driver.execute_script(
                "window.open('about:blank', '_blank');"
            )

            WebDriverWait(
                driver,
                10,
            ).until(
                lambda current_driver: (
                    len(current_driver.window_handles)
                    > len(before)
                )
            )

            new_handles = [
                handle
                for handle in driver.window_handles
                if handle not in before
            ]

            if not new_handles:
                raise WebDriverException(
                    "Brave did not create a new tab."
                )

            return new_handles[-1]

    def switch_to_tab(
        self,
        handle: str,
    ) -> None:
        with self.operation_lock:
            driver = self.get_driver()

            if handle not in driver.window_handles:
                raise WebDriverException(
                    "The requested Brave tab "
                    "no longer exists."
                )

            driver.switch_to.window(handle)

    def close_tab(
        self,
        handle: str,
    ) -> None:
        with self.operation_lock:
            driver = self.get_driver()
            handles = driver.window_handles

            if handle not in handles:
                return

            if len(handles) == 1:
                driver.get("about:blank")
                return

            original_handle = (
                driver.current_window_handle
            )

            driver.switch_to.window(handle)
            driver.close()

            remaining_handles = (
                driver.window_handles
            )

            if not remaining_handles:
                return

            if (
                original_handle
                in remaining_handles
            ):
                driver.switch_to.window(
                    original_handle
                )
            else:
                driver.switch_to.window(
                    remaining_handles[0]
                )

    def restart_browser(self) -> None:
        with self.lock:
            self.quit_driver_only()

            if self.browser_process is not None:
                try:
                    self.browser_process.terminate()
                    self.browser_process.wait(
                        timeout=3
                    )
                except subprocess.TimeoutExpired:
                    self.browser_process.kill()
                finally:
                    self.browser_process = None

            time.sleep(1)
            self._launch_browser_process()

    def quit_driver_only(self) -> None:
        with self.lock:
            if self.driver is not None:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                finally:
                    self.driver = None

    def quit(self) -> None:
        with self.lock:
            self.quit_driver_only()

            if self.browser_process is not None:
                try:
                    self.browser_process.terminate()
                except Exception:
                    pass
                finally:
                    self.browser_process = None
