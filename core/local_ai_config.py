"""Configuration for the optional local AI change verifier."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CONFIG_VERSION = 1
RECOMMENDED_MODEL_NAME = "Qwen3-4B-Q4_K_M.gguf"
RECOMMENDED_MODEL_PAGE = (
    "https://huggingface.co/Qwen/Qwen3-4B-GGUF/blob/main/"
    "Qwen3-4B-Q4_K_M.gguf"
)
RECOMMENDED_MODEL_DOWNLOAD = (
    "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/"
    "Qwen3-4B-Q4_K_M.gguf?download=true"
)
LLAMA_CPP_RELEASES_PAGE = "https://github.com/ggml-org/llama.cpp/releases"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def config_path() -> Path:
    return project_root() / "config" / "local_ai_verifier.json"


def _resolve_saved_path(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return Path()

    candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
    if candidate.is_absolute():
        return candidate

    return project_root() / candidate


def _portable_path(value: str | Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root() / path

    try:
        return str(path.resolve().relative_to(project_root().resolve()))
    except (OSError, ValueError):
        return str(path)


@dataclass(slots=True)
class LocalAIConfig:
    """Saved settings for llama.cpp and the selected GGUF model."""

    version: int = CONFIG_VERSION
    enabled: bool = False
    model_path: str = ""
    server_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8099
    context_size: int = 4096
    threads: int = 0
    gpu_layers: int = 99
    timeout_seconds: int = 120
    confidence_threshold: float = 0.72
    auto_start_server: bool = True
    verify_price_changes: bool = True

    @classmethod
    def load(cls) -> "LocalAIConfig":
        path = config_path()
        if not path.exists():
            config = cls()
            config.apply_discovery()
            return config

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

        allowed = set(cls.__dataclass_fields__)
        values = {key: value for key, value in data.items() if key in allowed}

        try:
            config = cls(**values)
        except (TypeError, ValueError):
            config = cls()

        config.apply_discovery(only_missing=True)
        return config

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        data = asdict(self)
        data["version"] = CONFIG_VERSION
        data["model_path"] = (
            _portable_path(self.model_path) if self.model_path else ""
        )
        data["server_path"] = (
            _portable_path(self.server_path) if self.server_path else ""
        )

        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)

    @property
    def resolved_model_path(self) -> Path:
        return _resolve_saved_path(self.model_path)

    @property
    def resolved_server_path(self) -> Path:
        path = _resolve_saved_path(self.server_path)
        if path.is_file():
            return path

        discovered = shutil.which("llama-server") or shutil.which(
            "llama-server.exe"
        )
        return Path(discovered) if discovered else Path()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{int(self.port)}"

    def apply_discovery(self, *, only_missing: bool = False) -> None:
        if not only_missing or not self.model_path:
            model = self.discover_model()
            if model:
                self.model_path = _portable_path(model)

        if not only_missing or not self.server_path:
            server = self.discover_server()
            if server:
                self.server_path = _portable_path(server)

    @staticmethod
    def discover_model() -> Path:
        root = project_root()
        preferred = [
            root / "models" / "local-ai" / RECOMMENDED_MODEL_NAME,
            root / "models" / RECOMMENDED_MODEL_NAME,
            root / RECOMMENDED_MODEL_NAME,
        ]
        for path in preferred:
            if path.is_file():
                return path

        for folder in (
            root / "models",
            root / "app" / "models",
            root / "llm-models",
        ):
            if not folder.is_dir():
                continue

            candidates = sorted(
                folder.rglob("*.gguf"),
                key=lambda item: (
                    "qwen3-4b" not in item.name.lower(),
                    "q4_k_m" not in item.name.lower(),
                    item.name.lower(),
                ),
            )
            if candidates:
                return candidates[0]

        return Path()

    @staticmethod
    def discover_server() -> Path:
        discovered = shutil.which("llama-server") or shutil.which(
            "llama-server.exe"
        )
        if discovered:
            return Path(discovered)

        root = project_root()
        for path in (
            root / "tools" / "llama.cpp" / "llama-server.exe",
            root / "tools" / "llama.cpp" / "llama-server",
            root / "llama.cpp" / "llama-server.exe",
            root / "llama.cpp" / "llama-server",
            root / "llama-server.exe",
            root / "llama-server",
        ):
            if path.is_file():
                return path

        return Path()

    def readiness(self) -> tuple[bool, str]:
        model = self.resolved_model_path
        server = self.resolved_server_path

        if not model.is_file():
            return False, "Choose a downloaded GGUF model."

        if model.suffix.lower() != ".gguf":
            return False, "The selected model must be a .gguf file."

        if not server.is_file():
            return False, "Choose llama-server.exe from llama.cpp."

        if not 1024 <= int(self.port) <= 65535:
            return False, "Choose a port from 1024 to 65535."

        if int(self.context_size) < 2048:
            return False, "Context size must be at least 2048."

        if not 0.50 <= float(self.confidence_threshold) <= 0.99:
            return False, "Confidence threshold must be from 0.50 to 0.99."

        return True, "Ready"

    def as_public_status(self) -> dict[str, Any]:
        ready, message = self.readiness()
        return {
            "enabled": bool(self.enabled),
            "ready": ready,
            "message": message,
            "model": (
                self.resolved_model_path.name
                if self.resolved_model_path.is_file()
                else ""
            ),
            "server": (
                self.resolved_server_path.name
                if self.resolved_server_path.is_file()
                else ""
            ),
            "base_url": self.base_url,
        }
