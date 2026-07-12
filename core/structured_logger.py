"""Thread-safe structured JSONL logging with readable console summaries."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4


@dataclass(slots=True)
class LogEvent:
    run_id: str
    sequence: int
    supplier: str
    event: str
    phase: str = ""
    row: int = 0
    url: str = ""
    verification: str = ""
    observed_stock: str = ""
    sheet_stock: str = ""
    price: str = ""
    quantity: Optional[int] = None
    reason: str = ""
    level: str = "info"
    extra: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "supplier": self.supplier,
            "row": self.row,
            "phase": self.phase,
            "event": self.event,
            "verification": self.verification,
            "observed_stock": self.observed_stock,
            "sheet_stock": self.sheet_stock,
            "price": self.price,
            "quantity": self.quantity,
            "reason": self.reason,
            "level": self.level,
            "url": self.url,
            "extra": dict(self.extra),
        }

    def console_line(self) -> str:
        parts = [f"[{self.supplier or 'APP'}]"]
        if self.row:
            parts.append(f"[Row {self.row}]")
        if self.verification:
            parts.append(f"[{self.verification}]")
        parts.append(self.event)
        details = []
        if self.observed_stock:
            details.append(f"Observed={self.observed_stock}")
        if self.sheet_stock:
            details.append(f"Sheet={self.sheet_stock}")
        if self.price:
            details.append(f"Price={self.price}")
        if self.quantity is not None:
            details.append(f"Qty={self.quantity}")
        if self.reason:
            details.append(f"Reason={self.reason}")
        return " ".join(parts) + (": " + " | ".join(details) if details else "")


class StructuredLogger:
    """Write append-only JSONL events without interleaving threads."""

    def __init__(
        self,
        supplier: str,
        *,
        logs_dir: str | os.PathLike[str] = "logs",
        run_id: Optional[str] = None,
        filename_prefix: str = "structured",
    ) -> None:
        self.supplier = supplier
        self.run_id = run_id or self._new_run_id(supplier)
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        safe_supplier = supplier.lower() if supplier else "app"
        self.path = self.logs_dir / (
            f"{filename_prefix}_{safe_supplier}_{self.run_id}.jsonl"
        )
        self._sequence = 0
        self._lock = threading.RLock()

    @staticmethod
    def _new_run_id(supplier: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = uuid4().hex[:8]
        return f"{supplier.lower() or 'app'}-{stamp}-{suffix}"

    def emit(
        self,
        event: str,
        *,
        phase: str = "",
        row: int = 0,
        url: str = "",
        verification: str = "",
        observed_stock: str = "",
        sheet_stock: str = "",
        price: str = "",
        quantity: Optional[int] = None,
        reason: str = "",
        level: str = "info",
        extra: Optional[Mapping[str, Any]] = None,
    ) -> LogEvent:
        with self._lock:
            self._sequence += 1
            record = LogEvent(
                run_id=self.run_id,
                sequence=self._sequence,
                supplier=self.supplier,
                event=event,
                phase=phase,
                row=int(row or 0),
                url=url,
                verification=verification,
                observed_stock=observed_stock,
                sheet_stock=sheet_stock,
                price=price,
                quantity=quantity,
                reason=reason,
                level=level,
                extra=dict(extra or {}),
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        record.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            return record
