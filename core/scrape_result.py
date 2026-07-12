from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class StockState(str, Enum):
    IN_STOCK = "IN_STOCK"
    OOS = "OOS"
    LOW_STOCK = "LOW_STOCK"
    LIMITED_STOCK = "LIMITED_STOCK"
    QUANTITY_REMAINING = "QUANTITY_REMAINING"
    UNKNOWN = "UNKNOWN"


@dataclass
class Evidence:
    source: str
    field: str
    value: str
    item_id: Optional[str] = None
    selector: Optional[str] = None
    confidence: float = 0.0


@dataclass
class ScrapeResult:
    supplier: str
    url: str
    row_number: int

    verification: VerificationStatus
    observed_stock: StockState

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

    evidence: list[Evidence] = field(
        default_factory=list
    )