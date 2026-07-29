"""Shared Scrapling browser layer for every supplier except Sportsman's Guide.

The existing supplier parsers were written around a small Selenium-style API.
This module supplies a conservative read-only compatibility driver backed by
Scrapling.  Every ``get`` call opens an isolated Scrapling page, captures the
fully rendered HTML, then closes the browser context.  No DOM from a previous
supplier row can survive into the next row.

Supported suppliers: WEB, WAL, HF, MN, LS, CE.
Sportsman's Guide is intentionally excluded and keeps its existing browser path.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

try:
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import NoSuchElementException
except ImportError:  # pragma: no cover - Selenium remains a project dependency.
    class By:  # type: ignore[no-redef]
        CSS_SELECTOR = "css selector"
        TAG_NAME = "tag name"
        ID = "id"
        NAME = "name"
        CLASS_NAME = "class name"
        XPATH = "xpath"

    class NoSuchElementException(LookupError):
        pass

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:  # Installer adds Scrapling; keep the project import-safe.
    StealthyFetcher = None  # type: ignore[assignment]


SCRAPLING_SHARED_BROWSER_VERSION = "2026.07.28.scrapling-all-suppliers-v10.3.0"
SUPPORTED_SUPPLIERS = frozenset({"WEB", "WAL", "HF", "MN", "LS", "CE"})
EXCLUDED_SUPPLIERS = frozenset({"SG"})

_CAPTCHA_MARKERS = (
    "captcha",
    "are you a human",
    "verify you are human",
    "verify that you are human",
    "pardon our interruption",
    "additional security check",
    "security check is required",
    "press & hold",
    "attention required",
    "just a moment",
    "access denied",
    "cf-chl-",
    "px-captcha",
    "datadome",
)


class ScraplingElement:
    """Small Selenium WebElement-compatible view over a BeautifulSoup tag."""

    def __init__(self, tag: Tag):
        self._tag = tag

    @property
    def tag_name(self) -> str:
        return str(self._tag.name or "")

    @property
    def text(self) -> str:
        return self._tag.get_text(" ", strip=True)

    def get_attribute(self, name: str) -> Optional[str]:
        key = str(name or "")
        lowered = key.lower()
        if lowered in {"textcontent", "innertext"}:
            return self.text
        if lowered == "innerhtml":
            return self._tag.decode_contents()
        if lowered == "outerhtml":
            return str(self._tag)
        value = self._tag.attrs.get(key)
        if value is None:
            value = self._tag.attrs.get(lowered)
        if value is None:
            return None
        if isinstance(value, (list, tuple, set)):
            return " ".join(str(item) for item in value)
        if value is True:
            return "true"
        return str(value)

    def get_dom_attribute(self, name: str) -> Optional[str]:
        return self.get_attribute(name)

    def is_displayed(self) -> bool:
        style = (self.get_attribute("style") or "").replace(" ", "").lower()
        hidden = self._tag.has_attr("hidden")
        aria_hidden = (self.get_attribute("aria-hidden") or "").lower() == "true"
        return not hidden and not aria_hidden and "display:none" not in style and "visibility:hidden" not in style

    def is_enabled(self) -> bool:
        disabled = self._tag.has_attr("disabled")
        aria_disabled = (self.get_attribute("aria-disabled") or "").lower() == "true"
        classes = (self.get_attribute("class") or "").lower()
        return not disabled and not aria_disabled and "disabled" not in classes

    def is_selected(self) -> bool:
        return self._tag.has_attr("selected") or (self.get_attribute("aria-selected") or "").lower() == "true"

    def click(self) -> None:
        # Snapshot elements are read-only.  For Select compatibility, mark an
        # option selected locally so parser code can continue deterministically.
        if self.tag_name.lower() == "option":
            self._tag.attrs["selected"] = "selected"

    def find_elements(self, by: str = By.CSS_SELECTOR, value: str = "") -> list["ScraplingElement"]:
        return [ScraplingElement(tag) for tag in _select_tags(self._tag, by, value)]

    def find_element(self, by: str = By.CSS_SELECTOR, value: str = "") -> "ScraplingElement":
        items = self.find_elements(by, value)
        if not items:
            raise NoSuchElementException(f"Element not found: {by}={value}")
        return items[0]


class ScraplingSnapshotDriver:
    """Read-only Selenium-style driver backed by one isolated Scrapling fetch."""

    is_scrapling_snapshot = True

    def __init__(self, manager: "ScraplingBrowserManager", supplier_code: str):
        self.manager = manager
        self.supplier_code = supplier_code.upper().strip()
        self.page_source = ""
        self.title = ""
        self.current_url = ""
        self.blocked_reason = ""
        self.fetch_error = ""
        self.status_code: Optional[int] = None
        self._timeout_ms = 60000
        self._soup = BeautifulSoup("<html><body></body></html>", "html.parser")

    def set_page_load_timeout(self, seconds: float) -> None:
        try:
            self._timeout_ms = max(10000, min(180000, int(float(seconds) * 1000)))
        except (TypeError, ValueError):
            self._timeout_ms = 60000

    def get(self, url: str) -> None:
        snapshot = self.manager.fetch_snapshot(
            url,
            supplier_code=self.supplier_code,
            timeout_ms=self._timeout_ms,
        )
        self.page_source = str(snapshot.get("html") or "")
        self.title = str(snapshot.get("title") or "")
        self.current_url = str(snapshot.get("url") or url)
        self.blocked_reason = str(snapshot.get("blocked_reason") or "")
        self.fetch_error = str(snapshot.get("error") or "")
        self.status_code = snapshot.get("status_code")
        self._soup = BeautifulSoup(self.page_source or "<html><body></body></html>", "html.parser")

    def refresh(self) -> None:
        if self.current_url:
            self.get(self.current_url)

    def find_elements(self, by: str = By.CSS_SELECTOR, value: str = "") -> list[ScraplingElement]:
        return [ScraplingElement(tag) for tag in _select_tags(self._soup, by, value)]

    def find_element(self, by: str = By.CSS_SELECTOR, value: str = "") -> ScraplingElement:
        items = self.find_elements(by, value)
        if not items:
            raise NoSuchElementException(f"Element not found: {by}={value}")
        return items[0]

    def execute_script(self, script: str, *args: Any) -> Any:
        source = str(script or "")
        compact = " ".join(source.split()).lower()

        if "arguments[0]" in compact and args:
            element = args[0]
            if isinstance(element, ScraplingElement):
                if "getattribute('data-value')" in compact or 'getattribute("data-value")' in compact:
                    return element.get_attribute("data-value") or element.text
                if "getattribute('aria-label')" in compact or 'getattribute("aria-label")' in compact:
                    return element.get_attribute("aria-label") or element.text
                if "innertext" in compact or "textcontent" in compact:
                    return element.text
                if "dispatchEvent" in source or ".click()" in source:
                    element.click()
                    return None

        if "document.readystate" in compact:
            return "complete"
        if "document.title" in compact and "document.body" not in compact:
            return self.title
        if "document.body" in compact and "innertext" in compact:
            body = self._soup.body or self._soup
            body_text = body.get_text(" ", strip=True)
            # Menards waits for one of several phrases and expects a boolean.
            includes = re.findall(r"txt\.includes\(['\"]([^'\"]+)['\"]\)", source, flags=re.I)
            title_includes = re.findall(r"title\.includes\(['\"]([^'\"]+)['\"]\)", source, flags=re.I)
            if includes or title_includes:
                body_lower = body_text.lower()
                title_lower = self.title.lower()
                return any(item.lower() in body_lower for item in includes) or any(
                    item.lower() in title_lower for item in title_includes
                )
            return body_text
        if "queryselector" in compact:
            selector_match = re.search(r"querySelector\((['\"])(.*?)\1\)", source, flags=re.S)
            if selector_match:
                selector = selector_match.group(2)
                tag = self._soup.select_one(selector)
                return tag.get_text(" ", strip=True) if tag else None
        if "window.scrollto" in compact or "window.stop" in compact:
            return None
        return None

    def execute_cdp_cmd(self, _name: str, _params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {}

    def get_cookies(self) -> list[dict[str, Any]]:
        return []

    def add_cookie(self, _cookie: dict[str, Any]) -> None:
        return None

    def quit(self) -> None:
        return None

    close = quit


class ScopedScraplingBrowserManager:
    """Supplier-bound view returned to an existing supplier scraper."""

    def __init__(self, parent: "ScraplingBrowserManager", supplier_code: str):
        code = supplier_code.upper().strip()
        if code not in SUPPORTED_SUPPLIERS:
            raise ValueError(f"Scrapling is not enabled for supplier {code!r}.")
        self.parent = parent
        self.supplier_code = code

    @property
    def headless(self) -> bool:
        return self.parent.headless

    def get_driver(self) -> ScraplingSnapshotDriver:
        return ScraplingSnapshotDriver(self.parent, self.supplier_code)

    def set_headless(self, value: bool) -> None:
        self.parent.set_headless(value)

    def quit(self) -> None:
        return None


class ScraplingBrowserManager:
    """One global toggle and isolated profiles for all non-SG suppliers."""

    _manual_lock = threading.Lock()

    def __init__(
        self,
        *,
        project_root: str | Path,
        headless: bool = True,
        logger_func: Optional[Callable[..., Any]] = None,
        config_path: Optional[str | Path] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.headless = bool(headless)
        self.log = logger_func
        self.config_path = Path(config_path) if config_path else (
            self.project_root / "config" / "scrapling_suppliers.json"
        )
        self.config = self._read_config()
        self._manual_thread: Optional[threading.Thread] = None
        self._supplier_locks = {code: threading.RLock() for code in SUPPORTED_SUPPLIERS}

    @property
    def is_available(self) -> bool:
        return StealthyFetcher is not None

    @property
    def mode_label(self) -> str:
        return "Headless (hidden)" if self.headless else "Physical browser (visible)"

    def for_supplier(self, supplier_code: str) -> ScopedScraplingBrowserManager:
        return ScopedScraplingBrowserManager(self, supplier_code)

    def set_headless(self, value: bool) -> None:
        self.headless = bool(value)

    def close(self) -> None:
        return None

    quit = close

    def profile_directory(self, supplier_code: str) -> Path:
        code = supplier_code.upper().strip()
        if code == "WAL":
            relative = "browser_profiles/walmart_scrapling"
        else:
            relative = f"browser_profiles/scrapling/{code.lower()}"
        configured = self.config.get("profiles", {}).get(code, relative)
        path = Path(str(configured))
        if not path.is_absolute():
            path = self.project_root / path
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def fetch_snapshot(
        self,
        url: str,
        *,
        supplier_code: str,
        timeout_ms: int = 60000,
    ) -> dict[str, Any]:
        code = supplier_code.upper().strip()
        if code not in SUPPORTED_SUPPLIERS:
            raise ValueError(f"Supplier {code} is excluded from Scrapling.")
        if StealthyFetcher is None:
            raise RuntimeError("Scrapling is not installed.")

        captured: dict[str, Any] = {
            "url": url,
            "title": "",
            "html": "",
            "blocked_reason": "",
            "error": "",
            "status_code": None,
            "supplier": code,
            "engine": "scrapling",
            "mode": "headless" if self.headless else "visible",
            "version": SCRAPLING_SHARED_BROWSER_VERSION,
        }

        def capture(page: Any) -> None:
            try:
                page.wait_for_selector("body", state="attached", timeout=timeout_ms)
            except Exception:
                pass
            try:
                page.evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight, 900));")
                time.sleep(float(self.config.get("scroll_settle_seconds", 0.8)))
                page.evaluate("window.scrollTo(0, 0);")
            except Exception:
                pass

            if not self.headless:
                self._wait_for_manual_challenge_clearance(page, timeout_ms)

            captured["url"] = str(getattr(page, "url", url) or url)
            try:
                captured["title"] = str(page.title() or "")
            except Exception:
                captured["title"] = ""
            try:
                captured["html"] = str(page.content() or "")
            except Exception as exc:
                captured["error"] = self._safe_exception(exc)
            captured["blocked_reason"] = _detect_blocked_reason(
                captured.get("title", ""), captured.get("html", "")
            )

        supplier_lock = self._supplier_locks[code]
        supplier_lock.acquire()
        try:
            try:
                response = StealthyFetcher.fetch(  # type: ignore[union-attr]
                    url,
                    headless=self.headless,
                    user_data_dir=str(self.profile_directory(code)),
                    solve_cloudflare=bool(self.config.get("solve_cloudflare", True)),
                    block_webrtc=True,
                    hide_canvas=True,
                    allow_webgl=True,
                    disable_resources=False,
                    network_idle=False,
                    load_dom=True,
                    timeout=max(30000, min(180000, int(timeout_ms))),
                    wait=int(self.config.get("wait_ms", 0)),
                    google_search=False,
                    locale=str(self.config.get("locale", "en-US") or "en-US"),
                    page_action=capture,
                )
                captured["status_code"] = getattr(response, "status", None) or getattr(response, "status_code", None)
                if not captured["html"]:
                    captured["html"] = _response_html(response)
                if not captured["url"]:
                    captured["url"] = str(getattr(response, "url", url) or url)
                if not captured["title"]:
                    soup = BeautifulSoup(captured["html"], "html.parser")
                    captured["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
                if not captured["blocked_reason"]:
                    captured["blocked_reason"] = _detect_blocked_reason(
                        captured["title"], captured["html"]
                    )
            except Exception as exc:
                captured["error"] = self._safe_exception(exc)
                if not captured["blocked_reason"]:
                    captured["blocked_reason"] = _detect_blocked_reason("", str(exc))
                self._emit_log(
                    f"{code}: Scrapling fetch failed: {captured['error']}",
                    "warning",
                )

        finally:
            supplier_lock.release()

        if captured["blocked_reason"]:
            self._emit_log(
                f"{code}: Scrapling detected verification/CAPTCHA; no supplier result should be trusted.",
                "warning",
            )
        return captured

    def open_manual_verification(
        self,
        url: str,
        supplier_code: Optional[str] = None,
    ) -> tuple[bool, str]:
        if StealthyFetcher is None:
            return False, "Scrapling is not installed."
        code = (supplier_code or _supplier_code_from_url(url)).upper().strip()
        if code == "SG":
            return False, "Sportsman's Guide is excluded from Scrapling."
        if code not in SUPPORTED_SUPPLIERS:
            return False, "The supplier could not be mapped to a Scrapling profile."
        if self._manual_thread and self._manual_thread.is_alive():
            return False, "A Scrapling verification window is already open."
        if not self._manual_lock.acquire(blocking=False):
            return False, "A Scrapling verification window is already open."

        self._manual_thread = threading.Thread(
            target=self._manual_worker,
            args=(url, code),
            daemon=True,
            name=f"ScraplingManualVerification-{code}",
        )
        self._manual_thread.start()
        return True, f"{code} verification window opened. Close it when finished."

    def _manual_worker(self, url: str, code: str) -> None:
        try:
            timeout_seconds = int(self.config.get("manual_timeout_seconds", 300))
            timeout_seconds = max(60, min(900, timeout_seconds))

            def hold(page: Any) -> None:
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    try:
                        if page.is_closed():
                            break
                    except Exception:
                        break
                    time.sleep(1.0)

            StealthyFetcher.fetch(  # type: ignore[union-attr]
                url,
                headless=False,
                user_data_dir=str(self.profile_directory(code)),
                solve_cloudflare=False,
                disable_resources=False,
                network_idle=False,
                load_dom=True,
                timeout=max(60000, timeout_seconds * 1000),
                wait=0,
                google_search=False,
                locale=str(self.config.get("locale", "en-US") or "en-US"),
                page_action=hold,
            )
        except Exception as exc:
            self._emit_log(
                f"{code} manual verification ended: {self._safe_exception(exc)}",
                "warning",
            )
        finally:
            try:
                self._manual_lock.release()
            except RuntimeError:
                pass

    def _wait_for_manual_challenge_clearance(self, page: Any, timeout_ms: int) -> None:
        deadline = time.monotonic() + min(300.0, max(10.0, timeout_ms / 1000.0))
        while time.monotonic() < deadline:
            try:
                title = str(page.title() or "")
                html = str(page.content() or "")
            except Exception:
                return
            if not _detect_blocked_reason(title, html):
                return
            time.sleep(1.0)

    def _read_config(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "locale": "en-US",
            "solve_cloudflare": True,
            "manual_timeout_seconds": 300,
            "wait_ms": 0,
            "scroll_settle_seconds": 0.8,
            "profiles": {},
        }
        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return defaults

    @staticmethod
    def _safe_exception(exc: BaseException) -> str:
        text = " ".join(str(exc or "").split())
        return text[:500] or exc.__class__.__name__

    def _emit_log(self, message: str, severity: str = "info") -> None:
        if not callable(self.log):
            return
        try:
            self.log(message, severity)
        except TypeError:
            try:
                self.log(message)
            except Exception:
                pass
        except Exception:
            pass


def _select_tags(root: Any, by: str, value: str) -> list[Tag]:
    selector_type = str(by or "").lower()
    query = str(value or "")
    if not query:
        return []
    try:
        if selector_type == str(By.CSS_SELECTOR).lower() or "css" in selector_type:
            return list(root.select(query))
        if selector_type == str(By.TAG_NAME).lower() or "tag" in selector_type:
            return list(root.find_all(query))
        if selector_type == str(By.ID).lower() or selector_type == "id":
            item = root.find(id=query)
            return [item] if isinstance(item, Tag) else []
        if selector_type == str(By.NAME).lower() or selector_type == "name":
            return list(root.find_all(attrs={"name": query}))
        if selector_type == str(By.CLASS_NAME).lower() or "class" in selector_type:
            return list(root.find_all(class_=query))
        if selector_type == str(By.XPATH).lower() or "xpath" in selector_type:
            # Existing non-SG scrapers do not depend on complex XPath.  Support
            # the common //tag form and fail closed for anything else.
            match = re.fullmatch(r"//([A-Za-z0-9_-]+)", query.strip())
            return list(root.find_all(match.group(1))) if match else []
    except Exception:
        return []
    return []


def _detect_blocked_reason(title: str, html: str) -> str:
    combined = f"{title}\n{html}".lower()
    for marker in _CAPTCHA_MARKERS:
        if marker in combined:
            return marker
    return ""


def _response_html(response: Any) -> str:
    for attr in ("html", "body", "text", "content"):
        value = getattr(response, attr, None)
        if value:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return str(value)
    return str(response or "")


def _supplier_code_from_url(url: str) -> str:
    host = (urlparse(str(url or "")).hostname or "").lower()
    if "walmart.com" in host:
        return "WAL"
    if "webstaurantstore.com" in host:
        return "WEB"
    if "harborfreight.com" in host:
        return "HF"
    if "menards.com" in host:
        return "MN"
    if "lakeside.com" in host:
        return "LS"
    if "collectionsetc.com" in host:
        return "CE"
    if "sportsmansguide.com" in host:
        return "SG"
    return ""
