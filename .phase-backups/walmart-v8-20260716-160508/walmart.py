"""Walmart scraper compatibility bridge.

The actual Walmart extraction is implemented in Node.js with Playwright under
``scrapers/walmart_runtime``. This Python class preserves the interface used by
the existing Supplier Stock Checker engine.

Legacy Walmart policy and double-check modules are intentionally not used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


WALMART_SCRAPER_VERSION = "2026.07.16.scratch-v1"


class WalmartScraper:
    """Run the exact-item Walmart Playwright extractor."""

    def __init__(
        self,
        browser_manager: Any = None,
        logger_func: Any = None,
        **_: Any,
    ) -> None:
        # Kept only for compatibility with ui/app.py. Walmart no longer
        # attaches to the user's signed-in Brave/Chrome profile.
        self.browser_manager = browser_manager
        self.log = logger_func
        self.project_root = Path(__file__).resolve().parents[1]
        self.runtime_dir = Path(__file__).resolve().parent / "walmart_runtime"
        self.runner = self.runtime_dir / "walmart_scrape.mjs"
        self.config_path = (
            self.project_root / "config" / "walmart_playwright.json"
        )

    def scrape(
        self,
        url: str,
        target_variation: str = "",
    ) -> tuple[
        str,
        str,
        str,
        str,
        list[dict[str, Any]],
        list[tuple[str, str, str]],
    ]:
        """Return the existing engine's six-value scraper tuple."""

        logs: list[tuple[str, str, str]] = []

        if not self.runner.is_file():
            return self._error(
                url,
                "Walmart runtime is missing. Reinstall the Walmart scratch rewrite.",
            )

        node = self._find_node()
        if not node:
            return self._error(
                url,
                "Node.js was not found. Install Node.js LTS, then run the Walmart setup script.",
            )

        request = {
            "url": str(url or "").strip(),
            "targetVariation": str(target_variation or "").strip(),
            "configPath": str(self.config_path),
            "projectRoot": str(self.project_root),
        }

        timeout_seconds = self._timeout_seconds()

        creation_flags = 0
        startup_info = None
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            completed = subprocess.run(
                [node, str(self.runner)],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                cwd=str(self.project_root),
                creationflags=creation_flags,
                startupinfo=startup_info,
                env={
                    **os.environ,
                    "NODE_NO_WARNINGS": "1",
                },
            )
        except subprocess.TimeoutExpired:
            return self._error(
                url,
                f"Walmart check exceeded {timeout_seconds} seconds.",
            )
        except OSError as exc:
            return self._error(
                url,
                f"Could not start the Walmart browser runtime: {exc}",
            )

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        if completed.returncode != 0:
            detail = stderr or stdout or "The Walmart runtime closed unexpectedly."
            return self._error(url, detail[:700])

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            detail = stderr or stdout or "The Walmart runtime returned unreadable data."
            return self._error(url, detail[:700])

        if not isinstance(payload, dict):
            return self._error(
                url,
                "The Walmart runtime returned an invalid result.",
            )

        status = str(payload.get("status") or "Error")
        title = str(payload.get("title") or "Walmart item")
        price = str(payload.get("price") or "")
        stock = str(payload.get("stock") or "")
        item_id = str(payload.get("itemId") or "")
        evidence = payload.get("evidence") or {}
        reason = str(payload.get("reason") or "").strip()

        if status != "Success":
            return self._error(
                url,
                reason or stock or "Walmart could not verify the exact item.",
                title=title,
                evidence=evidence,
            )

        if not stock:
            return self._error(
                url,
                "Walmart returned no stock state for the exact item.",
                title=title,
                evidence=evidence,
            )

        variants = [
            {
                "label": target_variation or "Exact linked item",
                "price": price,
                "stock": stock,
                "item_id": item_id,
                "source": "walmart_playwright_exact_item",
                "evidence": evidence,
            }
        ]

        logs.append(
            (
                (
                    "Walmart exact-item extractor: "
                    f"item {item_id or 'unknown'} | "
                    f"price {price or 'not shown'} | stock {stock}."
                ),
                "info",
                url,
            )
        )

        if reason:
            logs.append((reason, "info", url))

        return "Success", price, stock, title, variants, logs

    def _timeout_seconds(self) -> int:
        default = 90
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            value = int(data.get("process_timeout_seconds", default))
            return max(30, min(300, value))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _find_node() -> str | None:
        from shutil import which

        return which("node.exe") or which("node")

    @staticmethod
    def _error(
        url: str,
        message: str,
        *,
        title: str = "Walmart item",
        evidence: Any = None,
    ) -> tuple[
        str,
        str,
        str,
        str,
        list[dict[str, Any]],
        list[tuple[str, str, str]],
    ]:
        safe_message = str(message or "Unable to verify Walmart item.").strip()
        variants = [
            {
                "label": "Exact linked item",
                "price": "",
                "stock": "Unable to Verify",
                "source": "walmart_playwright_exact_item",
                "evidence": evidence or {},
                "reason": safe_message,
            }
        ]
        return (
            "Error",
            "",
            "Unable to Verify",
            title,
            variants,
            [(safe_message, "error", url)],
        )
