"""Regression tests for the dedicated Brave window focus policy."""

from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scrapers.browser import BraveDebugManager


class _FakeDriver:
    def __init__(self) -> None:
        self.window_handles = ["original"]
        self.current_window_handle = "original"
        self.commands: list[tuple[str, dict]] = []
        self.used_window_open = False

    def execute_cdp_cmd(self, method: str, params: dict):
        self.commands.append((method, params))
        if method == "Target.createTarget":
            self.window_handles.append("CDwindow-target-123")
            return {"targetId": "target-123"}
        if method == "Browser.getWindowForTarget":
            return {
                "windowId": 7,
                "bounds": {"windowState": "normal"},
            }
        if method == "Browser.setWindowBounds":
            return {}
        raise AssertionError(f"Unexpected CDP command: {method}")

    def execute_script(self, _script: str) -> None:
        self.used_window_open = True


class BraveMinimizedBackgroundTests(unittest.TestCase):
    def make_manager(self, driver: _FakeDriver) -> BraveDebugManager:
        manager = BraveDebugManager.__new__(BraveDebugManager)
        manager.port = 9222
        manager.headless = False
        manager.driver = driver
        manager.browser_process = None
        manager._browser_pid = None
        manager._minimize_stop = threading.Event()
        manager._minimize_thread = None
        manager._native_minimize_browser_window = lambda: False
        manager.lock = threading.RLock()
        manager.operation_lock = threading.RLock()
        manager.get_driver = lambda: driver
        return manager

    def test_new_tab_uses_background_cdp_target(self):
        driver = _FakeDriver()
        manager = self.make_manager(driver)

        handle = manager.get_new_tab()

        self.assertEqual(handle, "CDwindow-target-123")
        self.assertFalse(driver.used_window_open)
        self.assertIn(
            (
                "Target.createTarget",
                {
                    "url": "about:blank",
                    "background": True,
                },
            ),
            driver.commands,
        )

    def test_keep_browser_minimized_reapplies_window_state(self):
        driver = _FakeDriver()
        manager = self.make_manager(driver)

        self.assertTrue(manager.keep_browser_minimized(driver))
        self.assertIn(
            (
                "Browser.setWindowBounds",
                {
                    "windowId": 7,
                    "bounds": {
                        "windowState": "minimized",
                    },
                },
            ),
            driver.commands,
        )

    def test_browser_launch_starts_minimized(self):
        manager = BraveDebugManager.__new__(BraveDebugManager)
        manager.port = 9222
        manager.headless = False
        manager.browser_process = None
        manager.user_data_dir = Path("C:/Temp/BraveDebugProfile")
        manager._is_debugger_running = lambda: False
        manager._wait_for_debugger = lambda: None
        process = Mock()
        process.pid = 12345
        process.poll.return_value = None
        manager._start_minimize_watchdog = lambda: None

        with (
            patch(
                "scrapers.browser.ENFORCED_BRAVE_PATH",
                Path(sys.executable),
            ),
            patch(
                "scrapers.browser.subprocess.Popen",
                return_value=process,
            ) as popen,
            patch("builtins.print"),
        ):
            manager._launch_browser_process()

        command = popen.call_args.args[0]
        self.assertIn("--start-minimized", command)
        self.assertNotIn("--start-maximized", command)
        self.assertEqual(
            popen.call_args.kwargs["stdout"],
            subprocess.DEVNULL,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
