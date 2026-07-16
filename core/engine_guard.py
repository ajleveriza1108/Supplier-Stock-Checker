"""Safety gate between legacy scraper tuples and the existing engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from core.scrape_result import ScrapeResult, VerificationStatus
from core.scraper_diagnostics import read_structured_result
from core.structured_logger import StructuredLogger


@dataclass(frozen=True, slots=True)
class EngineDecision:
    accept: bool
    price: str
    stock: str
    status: str
    message: str
    tag: str = "info"
    count_as_error: bool = False


class EngineResultGuard:
    """Prevent partial or inconsistent results from entering update logic."""

    REVIEW_STATUS_NAMES = {
        "review",
        "partial",
        "conflict",
        "unknown",
        "blocked",
    }

    def __init__(self, queue=None) -> None:
        self.queue = queue
        self.logger = StructuredLogger("ENGINE")

    def evaluate(
        self,
        *,
        supplier: str,
        url: str,
        row_num: int,
        price: str,
        stock: str,
        status: str,
        title: str,
        variants: Iterable[dict[str, Any]],
        is_verify: bool,
    ) -> EngineDecision:
        normalized_status = str(status or "").strip().lower()
        structured = read_structured_result(variants)

        if structured is not None:
            structured.row_number = int(row_num or 0)

            return self._evaluate_structured(
                structured=structured,
                supplier=supplier,
                url=url,
                row_num=row_num,
                title=title,
                is_verify=is_verify,
            )

        # Other suppliers keep their existing legacy behavior. Walmart is
        # intentionally strict: a Success result without structured
        # verification metadata is not allowed to update the sheet.
        if supplier == "WAL" and normalized_status == "success":
            message = (
                f"[WAL][Row {row_num}][UNKNOWN] No sheet update: "
                "Walmart result is missing structured verification metadata."
            )

            self._emit(
                supplier=supplier,
                row_num=row_num,
                url=url,
                verification="UNKNOWN",
                event="legacy_result_rejected",
                reason=message,
                level="warning",
            )

            if is_verify:
                return EngineDecision(
                    accept=True,
                    price="",
                    stock="UNKNOWN",
                    status="Error",
                    message=message,
                    tag="warning",
                )

            return EngineDecision(
                accept=False,
                price="",
                stock="UNKNOWN",
                status="Review",
                message=message,
                tag="warning",
            )

        if normalized_status in self.REVIEW_STATUS_NAMES:
            message = (
                f"[{supplier}][Row {row_num}]"
                f"[{normalized_status.upper()}] "
                "No sheet update: scraper requested manual review."
            )

            self._emit(
                supplier=supplier,
                row_num=row_num,
                url=url,
                verification=normalized_status.upper(),
                event="review_result_rejected",
                reason=message,
                level="warning",
            )

            if is_verify:
                return EngineDecision(
                    accept=True,
                    price="",
                    stock="UNKNOWN",
                    status="Error",
                    message=message,
                    tag="warning",
                )

            return EngineDecision(
                accept=False,
                price="",
                stock="UNKNOWN",
                status="Review",
                message=message,
                tag="warning",
            )

        return EngineDecision(
            accept=True,
            price=price,
            stock=stock,
            status=status,
            message="Legacy result accepted.",
        )

    def _evaluate_structured(
        self,
        *,
        structured: ScrapeResult,
        supplier: str,
        url: str,
        row_num: int,
        title: str,
        is_verify: bool,
    ) -> EngineDecision:
        verification = structured.verification

        stock_only_mode = bool(
            structured.metadata.get("walmart_stock_only_mode")
            or structured.metadata.get("stock_only_mode")
        )

        event_extra = {
            "title": title,
            "is_verify": bool(is_verify),
            "item_id": structured.item_id,
            "seller": structured.seller,
            "policy_reason": structured.policy_reason,
            "stock_only_mode": stock_only_mode,
        }

        if not structured.is_safe_for_sheet:
            reason = (
                structured.error_message
                or structured.policy_reason
                or f"Verification is {verification.value}."
            )

            message = (
                f"[{supplier}][Row {row_num}][{verification.value}] "
                f"No sheet update: {reason}"
            )

            self._emit(
                supplier=supplier,
                row_num=row_num,
                url=url,
                verification=verification.value,
                event="structured_result_rejected",
                observed_stock=structured.observed_stock.value,
                sheet_stock=structured.sheet_stock or "",
                price=ScrapeResult.format_price(structured.price),
                quantity=structured.quantity,
                reason=reason,
                level=(
                    "error"
                    if verification
                    in {
                        VerificationStatus.ERROR,
                        VerificationStatus.BLOCKED,
                    }
                    else "warning"
                ),
                extra=event_extra,
            )

            phase2_error = bool(is_verify)

            return EngineDecision(
                accept=phase2_error,
                price="",
                stock=(
                    "Captcha/Blocked"
                    if verification == VerificationStatus.BLOCKED
                    else "UNKNOWN"
                ),
                status=(
                    "Error"
                    if (
                        phase2_error
                        or verification
                        in {
                            VerificationStatus.ERROR,
                            VerificationStatus.BLOCKED,
                        }
                    )
                    else "Review"
                ),
                message=message,
                tag=(
                    "error"
                    if verification
                    in {
                        VerificationStatus.ERROR,
                        VerificationStatus.BLOCKED,
                    }
                    else "warning"
                ),
                count_as_error=(
                    verification == VerificationStatus.ERROR
                    and not phase2_error
                ),
            )

        final_price = ""

        if (
            structured.sheet_stock == "In Stock"
            and not stock_only_mode
        ):
            final_price = ScrapeResult.format_price(
                structured.price
            )

        display_price = (
            "[manual review only]"
            if (
                stock_only_mode
                and structured.sheet_stock == "In Stock"
            )
            else final_price or "[blank]"
        )

        message = (
            f"[{supplier}][Row {row_num}][VERIFIED] "
            f"Observed={structured.observed_stock.value} | "
            f"Sheet={structured.sheet_stock} | "
            f"Price={display_price} | "
            f"Reason={structured.policy_reason}"
        )

        self._emit(
            supplier=supplier,
            row_num=row_num,
            url=url,
            verification=verification.value,
            event="structured_result_accepted",
            observed_stock=structured.observed_stock.value,
            sheet_stock=structured.sheet_stock or "",
            price=final_price,
            quantity=structured.quantity,
            reason=structured.policy_reason,
            level="info",
            extra=event_extra,
        )

        return EngineDecision(
            accept=True,
            price=final_price,
            stock=structured.sheet_stock or "",
            status="Success",
            message=message,
            tag="info",
        )

    def _emit(
        self,
        *,
        supplier: str,
        row_num: int,
        url: str,
        verification: str,
        event: str,
        observed_stock: str = "",
        sheet_stock: str = "",
        price: str = "",
        quantity: Optional[int] = None,
        reason: str = "",
        level: str = "info",
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            self.logger.emit(
                event,
                phase="engine_gate",
                row=row_num,
                url=url,
                verification=verification,
                observed_stock=observed_stock,
                sheet_stock=sheet_stock,
                price=price,
                quantity=quantity,
                reason=reason,
                level=level,
                extra={
                    "supplier": supplier,
                    **(extra or {}),
                },
            )
        except OSError:
            # Logging must never disable the safety gate.
            pass