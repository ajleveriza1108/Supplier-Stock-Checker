# scrapers/walmart_policy.py

"""Walmart-specific verification and stock-policy resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from core.result_policy import apply_stock_policy
from core.scrape_result import Evidence, ScrapeResult, StockState, VerificationStatus


LOW_INVENTORY_STATES = {
    StockState.LOW_STOCK,
    StockState.LIMITED_STOCK,
    StockState.QUANTITY_REMAINING,
}

AVAILABLE_STATES = {
    StockState.IN_STOCK,
    *LOW_INVENTORY_STATES,
}

STRONG_VISUAL_OOS_SCOPES = {
    "selected_option",
    "product",
    "all_fulfillment",
}


@dataclass(slots=True)
class WalmartObservation:
    source: str
    stock: StockState = StockState.UNKNOWN
    price: Optional[Decimal] = None
    quantity: Optional[int] = None
    confidence: float = 0.0
    exact_item_match: bool = False
    title_match: bool = False
    seller: str = ""
    text: str = ""
    reason: str = ""
    selector: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def conclusive(self) -> bool:
        return self.stock != StockState.UNKNOWN

    @property
    def is_available(self) -> bool:
        return self.stock in AVAILABLE_STATES

    def evidence(self, item_id: str) -> list[Evidence]:
        output = [
            Evidence(
                source=self.source,
                field="stock",
                value=self.stock.value,
                item_id=item_id or None,
                selector=self.selector or None,
                confidence=self.confidence,
                details={
                    "reason": self.reason,
                    "exact_item_match": self.exact_item_match,
                    "title_match": self.title_match,
                    **self.details,
                },
            )
        ]

        if self.price is not None:
            output.append(
                Evidence(
                    source=self.source,
                    field="price",
                    value=f"{self.price:.2f}",
                    item_id=item_id or None,
                    selector=self.selector or None,
                    confidence=self.confidence,
                )
            )

        if self.quantity is not None:
            output.append(
                Evidence(
                    source=self.source,
                    field="quantity",
                    value=str(self.quantity),
                    item_id=item_id or None,
                    selector=self.selector or None,
                    confidence=self.confidence,
                )
            )

        if self.seller:
            output.append(
                Evidence(
                    source=self.source,
                    field="seller",
                    value=self.seller,
                    item_id=item_id or None,
                    selector=self.selector or None,
                    confidence=self.confidence,
                )
            )

        return output


class WalmartPolicy:
    """Resolve exact-item JSON and the rendered primary buy box."""

    def resolve(
        self,
        *,
        url: str,
        item_id: str,
        title: str,
        final_url: str,
        target_variation: str,
        json_observation: WalmartObservation,
        visual_observation: WalmartObservation,
        blocked_reason: str = "",
        error_message: str = "",
    ) -> ScrapeResult:
        result = ScrapeResult(
            supplier="WAL",
            url=url,
            verification=VerificationStatus.UNKNOWN,
            observed_stock=StockState.UNKNOWN,
            title=title,
            item_id=item_id,
            variant=target_variation,
            final_url=final_url,
        )

        result.evidence.extend(json_observation.evidence(item_id))
        result.evidence.extend(visual_observation.evidence(item_id))

        result.metadata.update(
            {
                "json_reason": json_observation.reason,
                "visual_reason": visual_observation.reason,
                "json_text_excerpt": json_observation.text[:1200],
                "visual_text_excerpt": visual_observation.text[:1200],
            }
        )

        if blocked_reason:
            return self._blocked(result, blocked_reason)

        if error_message:
            return self._error(result, error_message)

        visual_internal_conflict = bool(
            visual_observation.details.get("internal_conflict")
        )
        json_internal_conflict = bool(
            json_observation.details.get("internal_conflict")
        )

        if visual_internal_conflict:
            return self._conflict(
                result,
                "The primary rendered product area contains contradictory "
                "stock signals.",
            )

        override_reason = self._strong_visual_override_reason(
            item_id=item_id,
            final_url=final_url,
            json_observation=json_observation,
            visual_observation=visual_observation,
        )

        if json_internal_conflict:
            if override_reason:
                return self._resolve_from_strong_visual(
                    result=result,
                    visual_observation=visual_observation,
                    reason=override_reason,
                )

            return self._conflict(
                result,
                "The exact-item JSON contains contradictory stock signals, "
                "and the rendered product area is not strong enough to "
                "resolve them safely.",
            )

        json_ok = json_observation.conclusive
        visual_ok = visual_observation.conclusive

        if json_ok and visual_ok:
            if not self._stocks_compatible(
                json_observation,
                visual_observation,
            ):
                if override_reason:
                    return self._resolve_from_strong_visual(
                        result=result,
                        visual_observation=visual_observation,
                        reason=override_reason,
                    )

                return self._conflict(
                    result,
                    "Exact-item JSON and the primary rendered product area "
                    f"disagree: {json_observation.stock.value} versus "
                    f"{visual_observation.stock.value}.",
                )

            resolved_stock = self._specific_stock(
                json_observation,
                visual_observation,
            )

            if (
                resolved_stock == StockState.IN_STOCK
                and (
                    json_observation.price is None
                    or visual_observation.price is None
                )
            ):
                result.verification = VerificationStatus.PARTIAL
                result.observed_stock = resolved_stock
                result.price = (
                    visual_observation.price or json_observation.price
                )
                result.error_message = (
                    "Normal In Stock requires a price from both exact-item "
                    "JSON and the primary rendered product area."
                )
                result.policy_reason = result.error_message
                return apply_stock_policy(result)

            if self._price_conflict(
                json_observation,
                visual_observation,
            ):
                return self._conflict(
                    result,
                    "Exact-item JSON and the primary rendered product area "
                    f"show different prices: ${json_observation.price:.2f} "
                    f"versus ${visual_observation.price:.2f}.",
                    observed_stock=resolved_stock,
                )

            result.verification = VerificationStatus.VERIFIED
            result.observed_stock = resolved_stock
            result.price = (
                visual_observation.price or json_observation.price
            )
            result.quantity = (
                visual_observation.quantity
                if visual_observation.quantity is not None
                else json_observation.quantity
            )
            result.seller = (
                visual_observation.seller or json_observation.seller
            )
            result.policy_reason = (
                "Exact-item JSON and primary rendered product evidence agree."
            )
            return apply_stock_policy(result)

        if json_ok:
            result.verification = VerificationStatus.PARTIAL
            result.observed_stock = json_observation.stock
            result.price = json_observation.price
            result.quantity = json_observation.quantity
            result.seller = json_observation.seller
            result.policy_reason = (
                "Exact-item JSON was found, but the primary rendered "
                "product evidence could not independently confirm it."
            )
            return apply_stock_policy(result)

        if visual_ok:
            result.verification = VerificationStatus.PARTIAL
            result.observed_stock = visual_observation.stock
            result.price = visual_observation.price
            result.quantity = visual_observation.quantity
            result.seller = visual_observation.seller
            result.policy_reason = (
                "Primary rendered product evidence was found, but exact-item "
                "JSON could not independently confirm it."
            )
            return apply_stock_policy(result)

        result.verification = VerificationStatus.UNKNOWN
        result.policy_reason = (
            "Neither exact-item JSON nor primary rendered product evidence "
            "produced a conclusive result."
        )
        return apply_stock_policy(result)

    @staticmethod
    def _blocked(
        result: ScrapeResult,
        reason: str,
    ) -> ScrapeResult:
        result.verification = VerificationStatus.BLOCKED
        result.error_message = reason
        result.policy_reason = "Blocked pages cannot update the sheet."
        return apply_stock_policy(result)

    @staticmethod
    def _error(
        result: ScrapeResult,
        reason: str,
    ) -> ScrapeResult:
        result.verification = VerificationStatus.ERROR
        result.error_message = reason
        result.policy_reason = "Scrape error cannot update the sheet."
        return apply_stock_policy(result)

    @staticmethod
    def _conflict(
        result: ScrapeResult,
        reason: str,
        *,
        observed_stock: StockState = StockState.UNKNOWN,
    ) -> ScrapeResult:
        result.verification = VerificationStatus.CONFLICT
        result.observed_stock = observed_stock
        result.error_message = reason
        result.policy_reason = reason
        return apply_stock_policy(result)

    @staticmethod
    def _strong_visual_override_reason(
        *,
        item_id: str,
        final_url: str,
        json_observation: WalmartObservation,
        visual_observation: WalmartObservation,
    ) -> str:
        """
        Allow the rendered primary buy box to resolve stale Walmart JSON.

        The override remains intentionally strict. It requires:

        - A conclusive visual result.
        - No contradictory visual signals.
        - The primary product region to be found.
        - High-confidence visual evidence.
        - The final URL to contain the requested Walmart item ID.
        - An enabled Add to cart button and price for available products.
        - Product-level OOS evidence for unavailable products.
        """

        if not visual_observation.conclusive:
            return ""

        if visual_observation.details.get("internal_conflict"):
            return ""

        if not visual_observation.title_match:
            return ""

        if float(visual_observation.confidence or 0.0) < 0.90:
            return ""

        final_url_text = str(final_url or "")

        if item_id and not re.search(
            rf"/{re.escape(str(item_id))}(?:[/?#]|$)",
            final_url_text,
        ):
            return ""

        json_conflicted = bool(
            json_observation.details.get("internal_conflict")
        )

        json_stock = json_observation.stock
        visual_stock = visual_observation.stock

        if visual_stock in AVAILABLE_STATES:
            if not visual_observation.details.get("enabled_cta"):
                return ""

            if visual_observation.price is None:
                return ""

            if json_conflicted or json_stock == StockState.OOS:
                return (
                    "Strong primary rendered buy-box evidence overrides "
                    "stale or contradictory exact-item JSON: an enabled "
                    "Add to cart control and a primary price are present."
                )

            return ""

        if visual_stock == StockState.OOS:
            if visual_observation.details.get("enabled_cta"):
                return ""

            oos_scope = str(
                visual_observation.details.get("oos_scope") or ""
            )

            if oos_scope not in STRONG_VISUAL_OOS_SCOPES:
                return ""

            if json_conflicted or json_stock in AVAILABLE_STATES:
                return (
                    "Strong product-level rendered OOS evidence overrides "
                    "stale or contradictory exact-item JSON."
                )

        return ""

    @staticmethod
    def _resolve_from_strong_visual(
        *,
        result: ScrapeResult,
        visual_observation: WalmartObservation,
        reason: str,
    ) -> ScrapeResult:
        result.verification = VerificationStatus.VERIFIED
        result.observed_stock = visual_observation.stock
        result.price = visual_observation.price
        result.quantity = visual_observation.quantity
        result.seller = visual_observation.seller
        result.policy_reason = reason

        result.metadata.update(
            {
                "resolved_by_visual_override": True,
                "visual_override_reason": reason,
                "visual_override_scope": str(
                    visual_observation.details.get("oos_scope") or ""
                ),
            }
        )

        return apply_stock_policy(result)

    @staticmethod
    def _stocks_compatible(
        json_observation: WalmartObservation,
        visual_observation: WalmartObservation,
    ) -> bool:
        first = json_observation.stock
        second = visual_observation.stock

        if first == second:
            return True

        if first in AVAILABLE_STATES and second in AVAILABLE_STATES:
            return True

        if (
            first in LOW_INVENTORY_STATES
            and second == StockState.OOS
        ):
            return str(
                visual_observation.details.get("oos_scope") or ""
            ) in STRONG_VISUAL_OOS_SCOPES

        return False

    @staticmethod
    def classify_rendered_signals(
        signals: dict[str, Any],
    ) -> tuple[StockState, Optional[int], str, bool, str]:
        """Classify stock evidence from the primary product area."""

        if not signals.get("regionFound"):
            return (
                StockState.UNKNOWN,
                None,
                str(
                    signals.get("reason")
                    or "Primary product region not found."
                ),
                False,
                "",
            )

        enabled_cta = bool(signals.get("enabledCta"))
        disabled_cta = bool(signals.get("disabledCta"))
        selected_option_oos = bool(
            signals.get("selectedOptionOos")
        )
        product_oos = bool(signals.get("productOos"))
        inventory_text = str(
            signals.get("inventoryText") or ""
        )
        fulfillment = signals.get("fulfillment") or {}

        states: list[str] = []

        if isinstance(fulfillment, dict):
            for value in fulfillment.values():
                if isinstance(value, dict):
                    state = str(
                        value.get("state") or "UNKNOWN"
                    ).upper()
                else:
                    state = str(
                        value or "UNKNOWN"
                    ).upper()

                if state in {
                    "AVAILABLE",
                    "UNAVAILABLE",
                    "UNKNOWN",
                }:
                    states.append(state)

        available_methods = states.count("AVAILABLE")
        unavailable_methods = states.count("UNAVAILABLE")

        if enabled_cta:
            if selected_option_oos:
                return (
                    StockState.UNKNOWN,
                    None,
                    (
                        "The primary Add to cart control is enabled, but the "
                        "page also says the selected option is out of stock."
                    ),
                    True,
                    "selected_option",
                )

            stock, quantity = parse_inventory_detail(
                inventory_text,
                available=True,
            )

            if stock == StockState.QUANTITY_REMAINING:
                reason = (
                    f"Enabled primary Add to cart; only "
                    f"{quantity} remaining."
                )
            elif stock == StockState.LOW_STOCK:
                reason = (
                    "Enabled primary Add to cart; page reports Low Stock."
                )
            elif stock == StockState.LIMITED_STOCK:
                reason = (
                    "Enabled primary Add to cart; page reports "
                    "Limited Stock."
                )
            else:
                reason = (
                    "Enabled primary product Add to cart control."
                )

            return stock, quantity, reason, False, ""

        if selected_option_oos:
            return (
                StockState.OOS,
                None,
                "The selected product option is explicitly out of stock.",
                False,
                "selected_option",
            )

        if product_oos:
            return (
                StockState.OOS,
                None,
                (
                    "The primary product purchase area explicitly reports "
                    "Out of stock."
                ),
                False,
                "product",
            )

        if (
            len(states) >= 2
            and unavailable_methods == len(states)
            and available_methods == 0
        ):
            return (
                StockState.OOS,
                None,
                "All detected primary fulfillment methods are unavailable.",
                False,
                "all_fulfillment",
            )

        if disabled_cta:
            return (
                StockState.UNKNOWN,
                None,
                (
                    "The primary Add to cart control is disabled without "
                    "enough product-level OOS evidence."
                ),
                False,
                "",
            )

        if available_methods:
            return (
                StockState.UNKNOWN,
                None,
                (
                    "A fulfillment method appears available, but no enabled "
                    "primary Add to cart control was found."
                ),
                False,
                "",
            )

        return (
            StockState.UNKNOWN,
            None,
            "Rendered primary product evidence was inconclusive.",
            False,
            "",
        )

    @staticmethod
    def _specific_stock(
        first: WalmartObservation,
        second: WalmartObservation,
    ) -> StockState:
        priority = (
            StockState.QUANTITY_REMAINING,
            StockState.LOW_STOCK,
            StockState.LIMITED_STOCK,
            StockState.IN_STOCK,
            StockState.OOS,
        )

        states = {
            first.stock,
            second.stock,
        }

        for stock in priority:
            if stock in states:
                return stock

        return StockState.UNKNOWN

    @staticmethod
    def _price_conflict(
        first: WalmartObservation,
        second: WalmartObservation,
    ) -> bool:
        if not first.is_available or not second.is_available:
            return False

        if first.price is None or second.price is None:
            return False

        return abs(
            first.price - second.price
        ) > Decimal("0.01")


def parse_inventory_detail(
    text: str,
    *,
    quantity: Optional[int] = None,
    available: bool = True,
) -> tuple[StockState, Optional[int]]:
    """Return the most specific available inventory state in text."""

    normalized = re.sub(
        r"\s+",
        " ",
        str(text or ""),
    ).strip().lower()

    if quantity is not None and 0 < quantity <= 10:
        return StockState.QUANTITY_REMAINING, quantity

    match = re.search(
        r"\bonly\s+(\d+)\s+(?:left|remaining|in stock)\b",
        normalized,
    )

    if match:
        return (
            StockState.QUANTITY_REMAINING,
            int(match.group(1)),
        )

    if "low stock" in normalized:
        return StockState.LOW_STOCK, quantity

    if "limited stock" in normalized:
        return StockState.LIMITED_STOCK, quantity

    if available:
        return StockState.IN_STOCK, quantity

    return StockState.UNKNOWN, quantity