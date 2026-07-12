"""Structured scraper result models shared by suppliers and the engine.

The legacy application still expects scraper tuples.  These models preserve
the full observation, verification, and business-policy history so that
ambiguous results cannot silently become sheet updates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Optional


class VerificationStatus(str, Enum):
    """How confidently the application verified a scrape result."""

    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class StockState(str, Enum):
    """Raw inventory state observed before business rules are applied."""

    IN_STOCK = "IN_STOCK"
    OOS = "OOS"
    LOW_STOCK = "LOW_STOCK"
    LIMITED_STOCK = "LIMITED_STOCK"
    QUANTITY_REMAINING = "QUANTITY_REMAINING"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class Evidence:
    """One field-level observation from JSON, DOM, browser, or policy."""

    source: str
    field: str
    value: str
    item_id: Optional[str] = None
    selector: Optional[str] = None
    confidence: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        return cls(
            source=str(data.get("source", "")),
            field=str(data.get("field", "")),
            value=str(data.get("value", "")),
            item_id=(
                str(data["item_id"])
                if data.get("item_id") is not None
                else None
            ),
            selector=(
                str(data["selector"])
                if data.get("selector") is not None
                else None
            ),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            details=dict(data.get("details") or {}),
        )


@dataclass(slots=True)
class ScrapeResult:
    """Complete supplier result before it is converted to the legacy tuple."""

    supplier: str
    url: str
    verification: VerificationStatus
    observed_stock: StockState

    row_number: int = 0
    price: Optional[Decimal] = None
    quantity: Optional[int] = None

    title: str = ""
    item_id: str = ""
    seller: str = ""
    variant: str = ""
    final_url: str = ""

    sheet_stock: Optional[str] = None
    policy_reason: str = ""
    error_message: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    observed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def add_evidence(
        self,
        *,
        source: str,
        field_name: str,
        value: Any,
        confidence: float = 0.0,
        item_id: Optional[str] = None,
        selector: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.evidence.append(
            Evidence(
                source=source,
                field=field_name,
                value=str(value),
                item_id=item_id,
                selector=selector,
                confidence=float(confidence),
                details=dict(details or {}),
            )
        )

    @property
    def is_verified(self) -> bool:
        return self.verification == VerificationStatus.VERIFIED

    @property
    def is_safe_for_sheet(self) -> bool:
        if not self.is_verified:
            return False
        if self.sheet_stock not in {"In Stock", "OOS"}:
            return False
        if self.sheet_stock == "In Stock" and self.price is None:
            return False
        return True

    @staticmethod
    def parse_price(value: Any) -> Optional[Decimal]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, Decimal):
            return value.quantize(Decimal("0.01"))
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        match = __import__("re").search(r"-?\d+(?:\.\d{1,2})?", text)
        if not match:
            return None
        try:
            amount = Decimal(match.group(0)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None
        return amount if amount >= 0 else None

    @staticmethod
    def format_price(value: Optional[Decimal]) -> str:
        if value is None:
            return ""
        return f"${value.quantize(Decimal('0.01'))}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplier": self.supplier,
            "url": self.url,
            "row_number": self.row_number,
            "verification": self.verification.value,
            "observed_stock": self.observed_stock.value,
            "price": (
                str(self.price.quantize(Decimal("0.01")))
                if self.price is not None
                else None
            ),
            "quantity": self.quantity,
            "title": self.title,
            "item_id": self.item_id,
            "seller": self.seller,
            "variant": self.variant,
            "final_url": self.final_url,
            "sheet_stock": self.sheet_stock,
            "policy_reason": self.policy_reason,
            "error_message": self.error_message,
            "evidence": [item.to_dict() for item in self.evidence],
            "metadata": dict(self.metadata),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScrapeResult":
        price = cls.parse_price(data.get("price"))
        quantity = data.get("quantity")
        try:
            parsed_quantity = int(quantity) if quantity is not None else None
        except (TypeError, ValueError):
            parsed_quantity = None

        return cls(
            supplier=str(data.get("supplier", "")),
            url=str(data.get("url", "")),
            row_number=int(data.get("row_number", 0) or 0),
            verification=VerificationStatus(
                str(data.get("verification", VerificationStatus.UNKNOWN.value))
            ),
            observed_stock=StockState(
                str(data.get("observed_stock", StockState.UNKNOWN.value))
            ),
            price=price,
            quantity=parsed_quantity,
            title=str(data.get("title", "")),
            item_id=str(data.get("item_id", "")),
            seller=str(data.get("seller", "")),
            variant=str(data.get("variant", "")),
            final_url=str(data.get("final_url", "")),
            sheet_stock=(
                str(data["sheet_stock"])
                if data.get("sheet_stock") is not None
                else None
            ),
            policy_reason=str(data.get("policy_reason", "")),
            error_message=str(data.get("error_message", "")),
            evidence=[
                Evidence.from_dict(item)
                for item in (data.get("evidence") or [])
                if isinstance(item, Mapping)
            ],
            metadata=dict(data.get("metadata") or {}),
            observed_at=str(data.get("observed_at", "")),
        )
