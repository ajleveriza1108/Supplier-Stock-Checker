"""Scrapling-backed Walmart fetcher with selectable hidden/visible browser mode.

The module deliberately reuses :mod:`scrapers.walmart` for exact-item
resolution and structured engine metadata.  Scrapling is responsible only for
loading the dynamic page and supplying isolated snapshots.  The existing
Walmart safety policy remains the authority for stock and Review Mode.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:  # The installer adds Scrapling; keep import failure safe.
    StealthyFetcher = None  # type: ignore[assignment]

from scrapers.walmart import (
    WALMART_SCRAPER_VERSION,
    WalmartScraper,
    _EXACT_SELECTED_OFFER_SCRIPT,
    _ResolvedSnapshot,
)


SCRAPLING_WALMART_VERSION = "2026.07.28.scrapling-all-suppliers-v10.3.0"


class WalmartScraplingScraper:
    """Fetch Walmart with Scrapling and resolve it through the safe v10.2 policy.

    ``headless=True`` runs a hidden browser. ``headless=False`` opens a normal
    physical browser for every Walmart row.  Both modes use the same local
    Scrapling profile so a Walmart sign-in, ZIP code, and manually cleared
    challenge can persist without storing credentials in source code.
    """

    _scrape_lock = threading.RLock()
    _manual_lock = threading.Lock()

    def __init__(
        self,
        *,
        headless: bool = True,
        logger_func: Optional[Callable[..., Any]] = None,
        selenium_fallback: Optional[WalmartScraper] = None,
        config_path: Optional[str | Path] = None,
    ) -> None:
        self.headless = bool(headless)
        self.log = logger_func
        self.selenium_fallback = selenium_fallback
        self.project_root = Path(__file__).resolve().parents[1]
        self.config_path = Path(config_path) if config_path else (
            self.project_root / "config" / "walmart_playwright.json"
        )
        self.config = self._read_config()
        profile_value = str(
            self.config.get(
                "scrapling_profile_directory",
                "browser_profiles/walmart_scrapling",
            )
            or "browser_profiles/walmart_scrapling"
        )
        profile_path = Path(profile_value)
        if not profile_path.is_absolute():
            profile_path = self.project_root / profile_path
        self.profile_dir = profile_path.resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._manual_thread: Optional[threading.Thread] = None
        self._last_url = "https://www.walmart.com/"

    @property
    def mode_label(self) -> str:
        return "Headless (hidden)" if self.headless else "Physical browser (visible)"

    @property
    def is_scrapling_available(self) -> bool:
        return StealthyFetcher is not None

    def set_headless(self, value: bool) -> None:
        """Change the mode used by the next Walmart request."""
        self.headless = bool(value)

    def close(self) -> None:
        """Compatibility hook for application shutdown.

        Scrapling's one-request fetcher closes each browser context itself.
        Persistent login state is kept in ``profile_dir``.
        """

    quit = close

    def scrape(
        self,
        url: str,
        target_variation: str = "",
    ) -> tuple[str, str, str, str, list[dict[str, Any]], list[tuple[str, str, str]]]:
        clean_url = str(url or "").strip()
        requested_item_id = WalmartScraper._extract_item_id(clean_url)
        self._last_url = clean_url or self._last_url

        if not clean_url or "walmart.com" not in clean_url.lower():
            return WalmartScraper._error(
                clean_url,
                "The link is not a valid Walmart product URL.",
            )
        if not requested_item_id:
            return WalmartScraper._error(
                clean_url,
                "The Walmart item ID could not be read from this link.",
            )

        if StealthyFetcher is None:
            return self._fallback_or_error(
                clean_url,
                target_variation,
                "Scrapling is not installed. Run the v10.2 Scrapling installer.",
            )

        with self._scrape_lock:
            try:
                snapshots = self._fetch_snapshots(clean_url, requested_item_id)
                resolved = self._resolve_consensus(snapshots, requested_item_id)
                resolved.evidence = {
                    **dict(resolved.evidence or {}),
                    "browser_engine": "scrapling",
                    "browser_mode": "headless" if self.headless else "visible",
                    "scrapling_version": SCRAPLING_WALMART_VERSION,
                    "walmart_resolver_version": WALMART_SCRAPER_VERSION,
                    "snapshot_count": len(snapshots),
                }
                result = WalmartScraper._resolved_to_result(
                    url=clean_url,
                    target_variation=str(target_variation or "").strip(),
                    resolved=resolved,
                )
                try:
                    result.metadata.update(
                        {
                            "browser_engine": "scrapling",
                            "browser_mode": "headless" if self.headless else "visible",
                            "scrapling_profile_persistent": True,
                            "scrapling_version": SCRAPLING_WALMART_VERSION,
                        }
                    )
                except Exception:
                    pass
                logs = [
                    (
                        f"Walmart checked with Scrapling ({self.mode_label}).",
                        "info",
                        clean_url,
                    )
                ]
                return WalmartScraper._result_to_public_tuple(
                    result,
                    target_variation=str(target_variation or "").strip(),
                    logs=logs,
                )
            except Exception as exc:
                safe_message = self._safe_exception(exc)
                return self._fallback_or_error(
                    clean_url,
                    target_variation,
                    f"Scrapling could not verify the exact Walmart item: {safe_message}",
                )

    def _fetch_snapshots(self, url: str, requested_item_id: str) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        evaluate_script = self._playwright_evaluate_script()
        timeout_ms = self._navigation_timeout_ms()
        settle_seconds = self._settle_timeout_seconds()
        snapshot_gap = self._snapshot_gap_seconds()
        max_snapshots = max(2, min(6, int(self.config.get("scrapling_max_snapshots", 6))))

        def collect(page: Any) -> None:
            try:
                page.wait_for_selector("body", state="attached", timeout=timeout_ms)
            except Exception:
                pass

            deadline = time.monotonic() + settle_seconds
            while time.monotonic() < deadline and len(snapshots) < max_snapshots:
                try:
                    raw = page.evaluate(evaluate_script, requested_item_id)
                    if isinstance(raw, dict):
                        snapshots.append(dict(raw))
                        resolved_now = WalmartScraper._resolve_snapshot(raw, requested_item_id)
                        if resolved_now.stock == "Captcha":
                            break
                        resolved_list = [
                            WalmartScraper._resolve_snapshot(item, requested_item_id)
                            for item in snapshots
                        ]
                        if WalmartScraper._has_two_matching_successes(resolved_list):
                            break
                except Exception as exc:
                    snapshots.append(
                        {
                            "pageUrl": str(getattr(page, "url", url) or url),
                            "pageItemId": WalmartScraper._extract_item_id(
                                str(getattr(page, "url", url) or url)
                            ),
                            "title": "Walmart item",
                            "bodyText": "",
                            "captcha": False,
                            "snapshotError": self._safe_exception(exc),
                        }
                    )
                time.sleep(snapshot_gap)

        fetch_kwargs = {
            "headless": self.headless,
            "user_data_dir": str(self.profile_dir),
            "solve_cloudflare": bool(
                self.config.get("scrapling_solve_cloudflare", True)
            ),
            "block_webrtc": True,
            "hide_canvas": True,
            "allow_webgl": True,
            "disable_resources": False,
            "network_idle": False,
            "load_dom": True,
            "timeout": timeout_ms,
            "wait": 0,
            "google_search": False,
            "locale": str(self.config.get("scrapling_locale", "en-US") or "en-US"),
            "page_action": collect,
        }
        StealthyFetcher.fetch(url, **fetch_kwargs)  # type: ignore[union-attr]

        if not snapshots:
            raise RuntimeError("Walmart returned no readable exact-item snapshots.")
        return snapshots

    def _resolve_consensus(
        self,
        snapshots: list[dict[str, Any]],
        requested_item_id: str,
    ) -> _ResolvedSnapshot:
        resolved_snapshots = [
            WalmartScraper._resolve_snapshot(raw, requested_item_id)
            for raw in snapshots
        ]

        for resolved in resolved_snapshots:
            if resolved.stock == "Captcha":
                resolved.evidence = {
                    **dict(resolved.evidence or {}),
                    "raw_snapshots": snapshots[-3:],
                }
                return resolved

        winner = WalmartScraper._majority_success(resolved_snapshots)
        if winner is not None and (
            WalmartScraper._has_two_matching_successes(resolved_snapshots)
            or len(resolved_snapshots) >= 2
        ):
            winner.evidence = {
                **dict(winner.evidence or {}),
                "consensus": "matching Scrapling exact-item snapshots",
                "raw_snapshots": snapshots[-3:],
            }
            return winner

        last = resolved_snapshots[-1] if resolved_snapshots else None
        reason = WalmartScraper._consensus_failure_reason(resolved_snapshots)
        result = _ResolvedSnapshot(
            status="Error",
            stock="Unable to Verify",
            price="",
            title=last.title if last else "Walmart item",
            item_id=requested_item_id,
            reason=reason,
            evidence={"raw_snapshots": snapshots[-4:]},
        )
        try:
            WalmartScraper(browser_manager=None)._save_debug_evidence(
                requested_item_id,
                self._last_url,
                snapshots,
                reason,
            )
        except Exception:
            pass
        return result

    def open_manual_verification(self, url: str = "") -> tuple[bool, str]:
        """Open the same Scrapling profile visibly for login/CAPTCHA work.

        The call is non-blocking.  The visible browser remains open until the
        user closes it or the configured manual timeout is reached.
        """
        target_url = str(url or self._last_url or "https://www.walmart.com/").strip()
        if StealthyFetcher is None:
            return False, "Scrapling is not installed."
        if self._manual_thread and self._manual_thread.is_alive():
            return False, "A Walmart verification window is already open."
        if not self._manual_lock.acquire(blocking=False):
            return False, "A Walmart verification window is already open."

        self._manual_thread = threading.Thread(
            target=self._manual_worker,
            args=(target_url,),
            daemon=True,
            name="WalmartScraplingManualVerification",
        )
        self._manual_thread.start()
        return True, "Walmart Scrapling verification window opened. Close it when finished."

    def _manual_worker(self, url: str) -> None:
        try:
            timeout_seconds = self._manual_timeout_seconds()

            def hold_window(page: Any) -> None:
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
                user_data_dir=str(self.profile_dir),
                solve_cloudflare=False,
                disable_resources=False,
                network_idle=False,
                load_dom=True,
                timeout=max(60000, int(timeout_seconds * 1000)),
                wait=0,
                google_search=False,
                locale=str(self.config.get("scrapling_locale", "en-US") or "en-US"),
                page_action=hold_window,
            )
        except Exception as exc:
            self._emit_log(
                f"Walmart manual verification window ended: {self._safe_exception(exc)}",
                "warning",
            )
        finally:
            try:
                self._manual_lock.release()
            except RuntimeError:
                pass

    def _fallback_or_error(
        self,
        url: str,
        target_variation: str,
        message: str,
    ) -> tuple[str, str, str, str, list[dict[str, Any]], list[tuple[str, str, str]]]:
        use_fallback = bool(self.config.get("scrapling_fallback_to_selenium", False))
        if use_fallback and self.selenium_fallback is not None:
            self._emit_log(
                f"Scrapling was unavailable; using the protected Selenium fallback. {message}",
                "warning",
            )
            return self.selenium_fallback.scrape(url, target_variation)
        return WalmartScraper._error(url, message)

    def _read_config(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "navigation_timeout_ms": 60000,
            "settle_timeout_ms": 15000,
            "snapshot_gap_ms": 1200,
            "scrapling_max_snapshots": 6,
            "scrapling_profile_directory": "browser_profiles/walmart_scrapling",
            "scrapling_solve_cloudflare": True,
            "scrapling_manual_timeout_seconds": 300,
            "scrapling_fallback_to_selenium": False,
            "scrapling_locale": "en-US",
        }
        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return defaults

    def _navigation_timeout_ms(self) -> int:
        try:
            value = int(self.config.get("navigation_timeout_ms", 60000))
        except (TypeError, ValueError):
            value = 60000
        return max(30000, min(120000, value))

    def _settle_timeout_seconds(self) -> float:
        try:
            value = int(self.config.get("settle_timeout_ms", 15000)) / 1000.0
        except (TypeError, ValueError):
            value = 15.0
        return max(5.0, min(35.0, value))

    def _snapshot_gap_seconds(self) -> float:
        try:
            value = int(self.config.get("snapshot_gap_ms", 1200)) / 1000.0
        except (TypeError, ValueError):
            value = 1.2
        return max(0.6, min(3.0, value))

    def _manual_timeout_seconds(self) -> int:
        try:
            value = int(self.config.get("scrapling_manual_timeout_seconds", 300))
        except (TypeError, ValueError):
            value = 300
        return max(60, min(900, value))

    @staticmethod
    def _playwright_evaluate_script() -> str:
        body = _EXACT_SELECTED_OFFER_SCRIPT.replace(
            "const requestedItemId = String(arguments[0] || '');",
            "const requestedItemId = String(requestedItemIdArg || '');",
            1,
        )
        return "(requestedItemIdArg) => {\n" + body + "\n}"

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
