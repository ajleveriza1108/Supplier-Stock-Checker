"""Local AI setup dialog for the Supplier Stock Checker."""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable

import customtkinter as ctk

from core.local_ai_config import (
    LLAMA_CPP_RELEASES_PAGE,
    RECOMMENDED_MODEL_DOWNLOAD,
    RECOMMENDED_MODEL_NAME,
    LocalAIConfig,
)
from core.local_ai_verifier import LocalAIChangeVerifier


class LocalAISettingsDialog(ctk.CTkToplevel):
    """Configure a local GGUF model and llama-server.exe."""

    def __init__(
        self,
        master,
        *,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.on_saved = on_saved
        self.config_data = LocalAIConfig.load()

        self.title("Local AI Change Cross-Check")
        self.geometry("760x620")
        self.minsize(700, 560)
        self.transient(master)
        self.grab_set()

        self.enabled_var = ctk.BooleanVar(value=self.config_data.enabled)
        self.model_var = ctk.StringVar(value=self.config_data.model_path)
        self.server_var = ctk.StringVar(value=self.config_data.server_path)
        self.port_var = ctk.StringVar(value=str(self.config_data.port))
        self.context_var = ctk.StringVar(
            value=str(self.config_data.context_size)
        )
        self.gpu_layers_var = ctk.StringVar(
            value=str(self.config_data.gpu_layers)
        )
        self.confidence_var = ctk.StringVar(
            value=f"{self.config_data.confidence_threshold:.2f}"
        )
        self.status_var = ctk.StringVar(value="Checking configuration…")

        self._build_ui()
        self.after(150, self._refresh_status)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Local AI Change Cross-Check",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=24, pady=(20, 4), sticky="w")

        container = ctk.CTkScrollableFrame(self)
        container.grid(row=1, column=0, padx=20, pady=12, sticky="nsew")
        container.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            container,
            text=(
                "The supplier scraper still checks every product. "
                "Local AI runs only after the sheet and scraper show a "
                "change. It can confirm, reject, or mark the result "
                "inconclusive."
            ),
            justify="left",
            wraplength=675,
            text_color="gray70",
        ).grid(row=0, column=0, padx=12, pady=(12, 10), sticky="w")

        ctk.CTkSwitch(
            container,
            text="Enable local AI for detected changes",
            variable=self.enabled_var,
        ).grid(row=1, column=0, padx=12, pady=8, sticky="w")

        model = self._section(container, row=2, title="1. GGUF model")
        model.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(
            model,
            textvariable=self.model_var,
        ).grid(row=0, column=0, padx=(10, 6), pady=10, sticky="ew")
        ctk.CTkButton(
            model,
            text="Choose GGUF",
            width=120,
            command=self._choose_model,
        ).grid(row=0, column=1, padx=(0, 10), pady=10)

        model_actions = ctk.CTkFrame(model, fg_color="transparent")
        model_actions.grid(
            row=1,
            column=0,
            columnspan=2,
            padx=10,
            pady=(0, 10),
            sticky="ew",
        )
        ctk.CTkButton(
            model_actions,
            text=f"Download recommended {RECOMMENDED_MODEL_NAME}",
            command=lambda: webbrowser.open(RECOMMENDED_MODEL_DOWNLOAD),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            model_actions,
            text="About 2.5 GB",
            text_color="gray65",
        ).pack(side="left")

        server = self._section(
            container,
            row=3,
            title="2. llama.cpp server",
        )
        server.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(
            server,
            textvariable=self.server_var,
        ).grid(row=0, column=0, padx=(10, 6), pady=10, sticky="ew")
        ctk.CTkButton(
            server,
            text="Choose Server",
            width=120,
            command=self._choose_server,
        ).grid(row=0, column=1, padx=(0, 10), pady=10)
        ctk.CTkButton(
            server,
            text="Open llama.cpp downloads",
            command=lambda: webbrowser.open(LLAMA_CPP_RELEASES_PAGE),
        ).grid(
            row=1,
            column=0,
            columnspan=2,
            padx=10,
            pady=(0, 10),
            sticky="w",
        )

        advanced = self._section(container, row=4, title="3. Performance")
        for column, (label, variable) in enumerate(
            (
                ("Port", self.port_var),
                ("Context", self.context_var),
                ("GPU layers", self.gpu_layers_var),
                ("Confidence", self.confidence_var),
            )
        ):
            advanced.grid_columnconfigure(column, weight=1)
            box = ctk.CTkFrame(advanced, fg_color="transparent")
            box.grid(row=0, column=column, padx=8, pady=10, sticky="ew")
            ctk.CTkLabel(box, text=label).pack(anchor="w")
            ctk.CTkEntry(
                box,
                textvariable=variable,
                width=120,
            ).pack(fill="x")

        status = self._section(container, row=5, title="Status")
        ctk.CTkLabel(
            status,
            textvariable=self.status_var,
            justify="left",
            wraplength=640,
        ).grid(row=0, column=0, padx=10, pady=10, sticky="w")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=2, column=0, padx=20, pady=(0, 18), sticky="ew")

        self.test_button = ctk.CTkButton(
            buttons,
            text="Save and Test",
            command=self._save_and_test,
        )
        self.test_button.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            buttons,
            text="Save",
            command=self._save,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            buttons,
            text="Close",
            fg_color="transparent",
            border_width=1,
            command=self.destroy,
        ).pack(side="right")

    @staticmethod
    def _section(parent, *, row: int, title: str):
        outer = ctk.CTkFrame(parent)
        outer.grid(row=row, column=0, padx=8, pady=8, sticky="ew")
        outer.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            outer,
            text=title,
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=(8, 0), sticky="w")

        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew")
        return body

    def _choose_model(self) -> None:
        current = Path(self.model_var.get()).expanduser()
        filename = filedialog.askopenfilename(
            parent=self,
            title="Choose a GGUF model",
            initialdir=str(
                current.parent if current.parent.exists() else Path.cwd()
            ),
            filetypes=[
                ("GGUF models", "*.gguf"),
                ("All files", "*.*"),
            ],
        )
        if filename:
            self.model_var.set(filename)
            self._refresh_status()

    def _choose_server(self) -> None:
        current = Path(self.server_var.get()).expanduser()
        filename = filedialog.askopenfilename(
            parent=self,
            title="Choose llama-server.exe",
            initialdir=str(
                current.parent if current.parent.exists() else Path.cwd()
            ),
            filetypes=[
                ("llama-server.exe", "llama-server.exe"),
                ("Executable files", "*.exe"),
                ("All files", "*.*"),
            ],
        )
        if filename:
            self.server_var.set(filename)
            self._refresh_status()

    def _collect(self) -> LocalAIConfig:
        try:
            port = int(self.port_var.get().strip())
            context = int(self.context_var.get().strip())
            gpu_layers = int(self.gpu_layers_var.get().strip())
            confidence = float(self.confidence_var.get().strip())
        except ValueError as exc:
            raise ValueError(
                "Performance settings must contain valid numbers."
            ) from exc

        return LocalAIConfig(
            enabled=bool(self.enabled_var.get()),
            model_path=self.model_var.get().strip(),
            server_path=self.server_var.get().strip(),
            port=port,
            context_size=context,
            gpu_layers=gpu_layers,
            confidence_threshold=confidence,
            auto_start_server=True,
            verify_price_changes=True,
        )

    def _save(self, *, show_message: bool = True) -> bool:
        try:
            config = self._collect()
            config.save()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Local AI Setup", str(exc), parent=self)
            return False

        self.config_data = config
        self._refresh_status()

        if self.on_saved:
            self.on_saved()

        if show_message:
            messagebox.showinfo(
                "Local AI Setup",
                "Settings saved.",
                parent=self,
            )
        return True

    def _save_and_test(self) -> None:
        if not self._save(show_message=False):
            return

        ready, message = self.config_data.readiness()
        if not ready:
            messagebox.showwarning(
                "Local AI Setup",
                message,
                parent=self,
            )
            return

        self.test_button.configure(state="disabled", text="Testing…")
        self.status_var.set(
            "Starting the local model and testing its JSON response…"
        )

        def worker() -> None:
            verifier = LocalAIChangeVerifier(self.config_data)
            success, detail = verifier.test()
            self.after(0, lambda: self._test_finished(success, detail))

        threading.Thread(target=worker, daemon=True).start()

    def _test_finished(self, success: bool, detail: str) -> None:
        self.test_button.configure(state="normal", text="Save and Test")
        self.status_var.set(detail)

        if success:
            messagebox.showinfo("Local AI Setup", detail, parent=self)
        else:
            messagebox.showwarning("Local AI Setup", detail, parent=self)

    def _refresh_status(self) -> None:
        try:
            config = self._collect()
            ready, message = config.readiness()
        except ValueError as exc:
            ready, message = False, str(exc)

        prefix = "Ready" if ready else "Needs setup"
        enabled = "Enabled" if self.enabled_var.get() else "Disabled"
        self.status_var.set(f"{prefix} • {enabled}\n{message}")
