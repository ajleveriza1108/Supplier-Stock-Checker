"""Simple user-facing runtime logs with full diagnostics kept separately.

The engine and scrapers may continue to emit detailed technical messages.
``SimpleRuntimeLogQueue`` keeps those messages out of the normal application
log and writes them to a diagnostic text file instead.

Only explicitly marked row summaries and a few essential run-level messages
are displayed in the normal log.
"""

from __future__ import annotations

import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


SIMPLE_LOG_MARKER = "\x1eSIMPLE-RUNTIME-LOG\x1e"

_SYSTEM_PREFIXES = (
    "Review Mode (Hold Updates):",
    "Initiating Sequential Engine",
    "Initiating Concurrent Engine",
    "Starting Phase 2",
    "Engine Check Completed",
    "No suppliers selected",
    "Sheet Error:",
    "No valid URLs found",
    "Existing updates found.",
    "Connecting to VPN",
    "VPN Failed",
    "Engine run complete.",
    "App closing",
)

_TECHNICAL_PREFIXES = (
    "WAL:",
    "WAL-DC:",
    "SG:",
    "HF:",
    "WEB:",
    "MN:",
    "LS:",
    "CE:",
    "FINAL VERIFIED",
    "FINAL REVIEW",
    "FINAL ERROR",
    "Traceback",
    "Stacktrace",
)


def mark_simple_log(message: str) -> str:
    """Mark a message as safe for the normal user-facing log."""

    return SIMPLE_LOG_MARKER + str(message or "")


def normalize_sheet_stock(value: Any) -> str:
    """Return the two sheet states used by the application.

    An empty stock cell is intentionally treated as ``In Stock``.
    """

    text = str(value or "").strip().lower()
    if text in {
        "oos",
        "out of stock",
        "out-of-stock",
        "sold out",
        "unavailable",
    }:
        return "OOS"
    return "In Stock"


def _first_variant(
    variants: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    for variant in variants or []:
        if isinstance(variant, Mapping):
            return variant
    return {}


def _metadata_value(
    variant: Mapping[str, Any],
    key: str,
    default: Any = "",
) -> Any:
    value = variant.get(key, default)
    if value not in (None, ""):
        return value

    payload = variant.get("_structured_result")
    if isinstance(payload, Mapping):
        aliases = {
            "_verification": "verification",
            "_observed_stock": "observed_stock",
            "_sheet_stock": "sheet_stock",
            "_quantity": "quantity",
            "_policy_reason": "policy_reason",
            "_error_message": "error_message",
        }
        payload_key = aliases.get(key)
        if payload_key:
            return payload.get(payload_key, default)

    return default


def _canonical_observed_stock(value: Any) -> str:
    text = str(value or "").strip().upper().replace("-", "_")
    text = re.sub(r"\s+", "_", text)

    if re.search(r"\bONLY_\d+_", text):
        return "QUANTITY_REMAINING"
    if "QUANTITY_REMAINING" in text or "ITEM_REMAINING" in text:
        return "QUANTITY_REMAINING"
    if "LIMITED_STOCK" in text or "LOW_STOCK" in text:
        return "LIMITED_STOCK"
    if text in {"OOS", "OUT_OF_STOCK", "SOLD_OUT", "UNAVAILABLE"}:
        return "OOS"
    if text in {"IN_STOCK", "INSTOCK", "AVAILABLE"}:
        return "IN_STOCK"
    return "UNKNOWN"


def _quantity_from_text(value: Any) -> Optional[int]:
    match = re.search(
        r"\bonly\s+(\d+)\s+(?:remaining|left|in stock)\b",
        str(value or ""),
        re.I,
    )
    return int(match.group(1)) if match else None


def display_link_status(
    observed_stock: Any,
    quantity: Optional[int] = None,
) -> str:
    canonical = _canonical_observed_stock(observed_stock)

    if canonical == "QUANTITY_REMAINING":
        resolved_quantity = quantity
        if resolved_quantity is None:
            resolved_quantity = _quantity_from_text(observed_stock)
        if resolved_quantity is not None:
            return f"Only {resolved_quantity} Remaining"
        return "Item Remaining"

    if canonical == "LIMITED_STOCK":
        return "Limited Stock"
    if canonical == "OOS":
        return "OOS"
    if canonical == "IN_STOCK":
        return "In Stock"
    return "Unable to Verify"


def effective_link_sheet_status(
    observed_stock: Any,
    explicit_sheet_stock: Any = "",
) -> Optional[str]:
    explicit = str(explicit_sheet_stock or "").strip()
    if explicit:
        return normalize_sheet_stock(explicit)

    canonical = _canonical_observed_stock(observed_stock)
    if canonical in {
        "OOS",
        "LIMITED_STOCK",
        "QUANTITY_REMAINING",
    }:
        return "OOS"
    if canonical == "IN_STOCK":
        return "In Stock"
    return None


def _price_decimal(value: Any) -> Optional[Decimal]:
    text = str(value or "").strip()
    if not text or text.upper() in {
        "N/A",
        "NONE",
        "SEE MEMBER PRICE IN CHECKOUT",
    }:
        return None

    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned:
        return None

    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _display_price(value: Any) -> str:
    number = _price_decimal(value)
    if number is None:
        return str(value or "").strip() or "[blank]"
    return f"${number:.2f}"


def _verification_label(status: Any, verification: Any) -> str:
    verification_text = str(verification or "").strip().upper()
    if verification_text:
        return verification_text

    status_text = str(status or "").strip().upper()
    if status_text == "SUCCESS":
        return "VERIFIED"
    if status_text == "REVIEW":
        return "REVIEW"
    if status_text == "ERROR":
        return "ERROR"
    return status_text or "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RuntimeRowSummary:
    row_number: int
    url: str
    sheet_status: str
    link_status: str
    comparison: str
    notice: str

    def render(self) -> str:
        return "\n".join(
            (
                f"Row: {self.row_number}",
                f"Link: {self.url}",
                f"Sheet Status: {self.sheet_status}",
                f"Link Status: {self.link_status}",
                f"Comparison: {self.comparison}",
                f"NOTICE: {self.notice}",
                "-" * 72,
            )
        )


def build_runtime_row_summary(
    item: tuple,
    sheet_cols: Mapping[str, int],
    *,
    pending_review: bool = False,
) -> RuntimeRowSummary:
    """Build the concise normal-log summary for one scraper result."""

    if len(item) < 10:
        raise ValueError(
            "A scraper result item must contain at least 10 values."
        )

    (
        _supplier,
        url,
        price,
        stock,
        status,
        _title,
        row,
        row_number,
        variants,
        _is_verify,
    ) = item[:10]

    stock_col = int(sheet_cols.get("stock_status", 2))
    price_col = int(sheet_cols.get("price", 15))

    previous_stock_raw = (
        row[stock_col]
        if isinstance(row, (list, tuple)) and len(row) > stock_col
        else ""
    )
    previous_price_raw = (
        row[price_col]
        if isinstance(row, (list, tuple)) and len(row) > price_col
        else ""
    )

    sheet_status = normalize_sheet_stock(previous_stock_raw)
    variant = _first_variant(variants)

    verification = _verification_label(
        status,
        _metadata_value(variant, "_verification"),
    )
    observed_stock = _metadata_value(
        variant,
        "_observed_stock",
        stock,
    )
    explicit_sheet_stock = _metadata_value(
        variant,
        "_sheet_stock",
        "",
    )

    quantity_raw = _metadata_value(variant, "_quantity", None)
    try:
        quantity = (
            int(quantity_raw)
            if quantity_raw not in (None, "")
            else _quantity_from_text(observed_stock)
        )
    except (TypeError, ValueError):
        quantity = _quantity_from_text(observed_stock)

    link_status = display_link_status(observed_stock, quantity)
    effective_status = effective_link_sheet_status(
        observed_stock,
        explicit_sheet_stock,
    )

    verified = (
        str(status or "").strip().lower() == "success"
        and verification == "VERIFIED"
        and effective_status is not None
    )

    if not verified:
        reason = (
            str(_metadata_value(variant, "_policy_reason", "") or "")
            or str(_metadata_value(variant, "_error_message", "") or "")
        )
        reason_first_line = reason.splitlines()[0].strip() if reason else ""
        compact_reason = (
            f"{verification}: {reason_first_line}"
            if reason_first_line
            else verification
        )
        return RuntimeRowSummary(
            row_number=int(row_number or 0),
            url=str(url or ""),
            sheet_status=sheet_status,
            link_status=(
                f"{link_status} — Unverified"
                if link_status != "Unable to Verify"
                else f"Unable to Verify ({verification})"
            ),
            comparison="Not Compared",
            notice=f"NO SHEET CHANGE — {compact_reason}",
        )

    same_stock = sheet_status == effective_status
    comparison = "Same" if same_stock else "Different"

    if link_status in {"Limited Stock", "Item Remaining"} or (
        link_status.startswith("Only ")
        and link_status.endswith(" Remaining")
    ):
        comparison += " — link is treated as OOS"

    previous_price = _price_decimal(previous_price_raw)
    new_price = _price_decimal(price)

    stock_changed = not same_stock
    price_changed = (
        effective_status == "In Stock"
        and new_price is not None
        and new_price != previous_price
    )

    changes: list[str] = []
    if stock_changed:
        changes.append(
            f"Stock {sheet_status} → {effective_status}"
        )
    if price_changed:
        changes.append(
            f"Price {_display_price(previous_price_raw)} "
            f"→ {_display_price(price)}"
        )

    if changes:
        notice = "CHANGE DETECTED — " + "; ".join(changes)
        if pending_review:
            notice += " — HELD FOR REVIEW"
    else:
        notice = "NO CHANGE"

    return RuntimeRowSummary(
        row_number=int(row_number or 0),
        url=str(url or ""),
        sheet_status=sheet_status,
        link_status=link_status,
        comparison=comparison,
        notice=notice,
    )


def format_runtime_row(
    item: tuple,
    sheet_cols: Mapping[str, int],
    *,
    pending_review: bool = False,
) -> str:
    return build_runtime_row_summary(
        item,
        sheet_cols,
        pending_review=pending_review,
    ).render()


class SimpleRuntimeLogQueue(queue.Queue):
    """Queue that separates normal runtime logs from diagnostics."""

    def __init__(
        self,
        maxsize: int = 0,
        *,
        diagnostics_dir: str | Path = "logs/diagnostics",
    ) -> None:
        super().__init__(maxsize=maxsize)
        self.diagnostics_dir = Path(diagnostics_dir)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.diagnostic_path = (
            self.diagnostics_dir
            / f"runtime_diagnostics_{stamp}.log"
        )
        self._diagnostic_lock = threading.RLock()

    @staticmethod
    def _is_log_event(item: Any) -> bool:
        return (
            isinstance(item, tuple)
            and len(item) >= 3
            and item[0] == "LOG"
        )

    @staticmethod
    def _allow_system_message(message: str) -> bool:
        return any(
            message.startswith(prefix)
            for prefix in _SYSTEM_PREFIXES
        )

    @staticmethod
    def _looks_technical(message: str) -> bool:
        stripped = message.lstrip()
        if any(
            stripped.startswith(prefix)
            for prefix in _TECHNICAL_PREFIXES
        ):
            return True
        lowered = stripped.lower()
        return any(
            token in lowered
            for token in (
                "confidence=",
                "verification=",
                "observed=",
                "policy reason",
                "stacktrace:",
                "chromedriver!",
                "webdriverexception",
                "processing row",
                "strike 1",
                "strike 2",
                "strike 3",
                "held for user review",
                "change detected. ai verification",
                "row acknowledgement",
            )
        )

    def write_diagnostic(
        self,
        message: Any,
        tag: str = "info",
        url: Optional[str] = None,
    ) -> None:
        text = str(message or "")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

        with self._diagnostic_lock:
            with self.diagnostic_path.open(
                "a",
                encoding="utf-8",
            ) as handle:
                handle.write(
                    f"[{timestamp}] [{str(tag).upper()}]"
                    + (f" [URL: {url}]" if url else "")
                    + "\n"
                    + text
                    + "\n"
                    + ("-" * 96)
                    + "\n"
                )

    def _filter_log_event(self, item: tuple) -> Optional[tuple]:
        message = str(item[1] if len(item) > 1 else "")
        tag = str(item[2] if len(item) > 2 else "info")
        url = item[3] if len(item) > 3 else None

        if message.startswith(SIMPLE_LOG_MARKER):
            clean = message[len(SIMPLE_LOG_MARKER) :]
            return ("LOG", clean, tag, url)

        self.write_diagnostic(message, tag, url)

        if self._allow_system_message(message):
            if message.startswith("Engine Check Completed"):
                clean = message
            else:
                clean = message.splitlines()[0].strip()
            return ("LOG", clean, tag, url)

        if (
            tag.lower() == "error"
            and not url
            and not message.lstrip().startswith("Row ")
            and not self._looks_technical(message)
        ):
            clean = message.splitlines()[0].strip()
            return ("LOG", clean, tag, None)

        return None

    def put(
        self,
        item: Any,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        if self._is_log_event(item):
            item = self._filter_log_event(item)
            if item is None:
                return

        super().put(item, block=block, timeout=timeout)
