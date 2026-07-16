"""Manage an optional local llama.cpp server process."""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from core.local_ai_config import LocalAIConfig, project_root


class LocalAIServerError(RuntimeError):
    """Raised when the local AI server cannot be used safely."""


class LocalAIServerManager:
    """Start, reuse, health-check, and stop llama-server."""

    def __init__(self, config: LocalAIConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._log_stream = None
        self._lock = threading.RLock()
        self._owns_process = False
        atexit.register(self.stop)

    @property
    def base_url(self) -> str:
        return self.config.base_url

    def is_running(self) -> bool:
        return self._health_request(timeout=1.5)

    def ensure_running(self) -> None:
        with self._lock:
            if self.is_running():
                return

            ready, message = self.config.readiness()
            if not ready:
                raise LocalAIServerError(message)

            if not self.config.auto_start_server:
                raise LocalAIServerError(
                    f"No local AI server is responding at {self.base_url}."
                )

            self._start_process()

            deadline = time.monotonic() + 45.0
            while time.monotonic() < deadline:
                if self._process is not None and self._process.poll() is not None:
                    raise LocalAIServerError(
                        "llama-server closed before the model became ready. "
                        "Open logs/local_ai_server.log for details."
                    )

                if self.is_running():
                    return

                time.sleep(0.5)

            self.stop()
            raise LocalAIServerError(
                "The local model did not become ready. Check the selected "
                "model, llama.cpp build, and available memory."
            )

    def _start_process(self) -> None:
        server = self.config.resolved_server_path
        model = self.config.resolved_model_path

        log_dir = project_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "local_ai_server.log"

        threads = int(self.config.threads)
        if threads <= 0:
            threads = max(1, (os.cpu_count() or 4) - 1)

        command = [
            str(server),
            "-m",
            str(model),
            "--host",
            self.config.host,
            "--port",
            str(int(self.config.port)),
            "-c",
            str(int(self.config.context_size)),
            "-t",
            str(threads),
            "-ngl",
            str(max(0, int(self.config.gpu_layers))),
        ]

        creation_flags = 0
        startup_info = None
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        self._log_stream = log_path.open("a", encoding="utf-8")

        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(project_root()),
                stdout=self._log_stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                creationflags=creation_flags,
                startupinfo=startup_info,
            )
        except OSError as exc:
            self._close_log()
            raise LocalAIServerError(
                f"Could not start llama-server: {exc}"
            ) from exc

        self._owns_process = True

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None

            if process and self._owns_process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass

            self._owns_process = False
            self._close_log()

    def _close_log(self) -> None:
        stream = self._log_stream
        self._log_stream = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def model_id(self) -> str:
        try:
            payload = self.get_json("/v1/models", timeout=5)
            models = payload.get("data") or []
            if models and isinstance(models[0], dict):
                value = str(models[0].get("id") or "").strip()
                if value:
                    return value
        except LocalAIServerError:
            pass

        return self.config.resolved_model_path.stem or "local-model"

    def get_json(self, endpoint: str, *, timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + endpoint,
            headers={"Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError) as exc:
            raise LocalAIServerError(str(exc)) from exc

        return self._decode_json(raw)

    def post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LocalAIServerError(
                f"Local AI request failed ({exc.code}): {detail[:300]}"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise LocalAIServerError(str(exc)) from exc

        return self._decode_json(raw)

    @staticmethod
    def _decode_json(raw: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LocalAIServerError(
                "The local AI server returned an unreadable response."
            ) from exc

        if not isinstance(data, dict):
            raise LocalAIServerError(
                "The local AI server returned an invalid response."
            )

        return data

    def _health_request(self, *, timeout: float) -> bool:
        for endpoint in ("/health", "/v1/models"):
            try:
                self.get_json(endpoint, timeout=timeout)
                return True
            except LocalAIServerError:
                continue
        return False
