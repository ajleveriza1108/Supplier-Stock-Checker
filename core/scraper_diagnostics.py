"""Compatibility and diagnostic helpers for structured scraper results."""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from core.result_policy import evaluate_update_safety
from core.scrape_result import ScrapeResult, VerificationStatus


REVIEW_STATUSES = {
    VerificationStatus.PARTIAL,
    VerificationStatus.CONFLICT,
    VerificationStatus.UNKNOWN,
}


def evidence_summary(result: ScrapeResult, limit: int = 6) -> str:
    items = []
    for evidence in result.evidence[: max(1, int(limit))]:
        text = f"{evidence.source}.{evidence.field}={evidence.value}"
        if evidence.confidence:
            text += f" ({evidence.confidence:.2f})"
        items.append(text)
    return "; ".join(items) if items else "No field evidence recorded."


def structured_metadata(result: ScrapeResult) -> dict[str, Any]:
    return {
        "_verification": result.verification.value,
        "_observed_stock": result.observed_stock.value,
        "_sheet_stock": result.sheet_stock,
        "_policy_reason": result.policy_reason,
        "_error_message": result.error_message,
        "_quantity": result.quantity,
        "_item_id": result.item_id,
        "_seller": result.seller,
        "_final_url": result.final_url,
        "_evidence_summary": evidence_summary(result),
        "_structured_result": result.to_dict(),
    }


def result_to_legacy_tuple(
    result: ScrapeResult,
    *,
    target_variation: Optional[str] = None,
    logs: Optional[list[tuple[str, str, str]]] = None,
):
    """Convert a structured result to the application's six-value tuple.

    VERIFIED and policy-safe results return ``Success``.  Partial,
    conflicting, and unknown results return ``Review`` and intentionally
    expose neither a price nor a sheet stock value.  Blocked and error
    results return ``Error``.
    """

    output_logs = list(logs or [])
    safety = evaluate_update_safety(result)
    metadata = structured_metadata(result)

    variant = {
        "label": target_variation or result.variant or "Default",
        "price": (
            ScrapeResult.format_price(result.price)
            if safety.allowed and safety.sheet_stock == "In Stock"
            else ""
        ),
        "stock": safety.sheet_stock if safety.allowed else "UNKNOWN",
        **metadata,
    }
    variants = [variant]

    if safety.allowed:
        status = "Success"
        price = (
            ScrapeResult.format_price(safety.price)
            if safety.sheet_stock == "In Stock"
            else ""
        )
        stock = safety.sheet_stock or ""
        output_logs.append(
            (
                (
                    f"FINAL VERIFIED -> Observed: "
                    f"{result.observed_stock.value} | "
                    f"Sheet: {stock} | "
                    f"Price: {price or '[blank]'} | "
                    f"Reason: {safety.reason}"
                ),
                "oos" if stock == "OOS" else "price",
                result.url,
            )
        )
        return status, price, stock, result.title, variants, output_logs

    if result.verification in REVIEW_STATUSES:
        reason = result.policy_reason or result.error_message or safety.reason
        output_logs.append(
            (
                (
                    f"FINAL REVIEW -> Verification: "
                    f"{result.verification.value} | "
                    f"Observed: {result.observed_stock.value} | "
                    f"No sheet update | Reason: {reason}"
                ),
                "warning",
                result.url,
            )
        )
        return (
            "Review",
            "",
            "UNKNOWN",
            result.title,
            variants,
            output_logs,
        )

    stock = (
        "Captcha/Blocked"
        if result.verification == VerificationStatus.BLOCKED
        else "UNKNOWN"
    )
    reason = result.error_message or result.policy_reason or safety.reason
    output_logs.append(
        (
            (
                f"FINAL ERROR -> Verification: "
                f"{result.verification.value} | "
                f"Reason: {reason}"
            ),
            "error",
            result.url,
        )
    )
    return "Error", "", stock, result.title, variants, output_logs


def read_structured_result(variants: Iterable[dict[str, Any]]) -> Optional[ScrapeResult]:
    """Recover a ScrapeResult from a legacy variants payload."""

    for variant in variants or []:
        if not isinstance(variant, dict):
            continue
        payload = variant.get("_structured_result")
        if isinstance(payload, dict):
            try:
                return ScrapeResult.from_dict(payload)
            except (TypeError, ValueError, KeyError):
                continue
        if isinstance(payload, str):
            try:
                decoded = json.loads(payload)
                if isinstance(decoded, dict):
                    return ScrapeResult.from_dict(decoded)
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                continue
    return None
