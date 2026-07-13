"""Conservative Walmart verification and stock-policy resolution.

This module deliberately prefers manual review over a guessed Walmart update.

Rules:
- Exact-item JSON and the rendered primary buy box must both be conclusive.
- The raw stock states must agree exactly.
- Low Stock, Limited Stock, and Only N Remaining are preserved as raw states;
  the application-wide stock policy maps all three to sheet OOS.
- Fulfillment-only OOS text never overrides an enabled primary Add to cart.
- A normal In Stock result requires matching prices from both sources.
- No visual or JSON source may override the other.
"""

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


@dataclass(slots=True)
class WalmartObservation:
    """One independently collected Walmart evidence source."""

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
                confidence=float(self.confidence or 0.0),
                details={
                    "reason": self.reason,
                    "exact_item_match": bool(self.exact_item_match),
                    "title_match": bool(self.title_match),
                    **dict(self.details or {}),
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
                    confidence=float(self.confidence or 0.0),
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
                    confidence=float(self.confidence or 0.0),
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
                    confidence=float(self.confidence or 0.0),
                )
            )

        return output


class WalmartPolicy:
    """Resolve exact-item JSON and the primary rendered purchase block."""

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
                "walmart_policy": "conservative-consensus-v1",
                "manual_review_walmart_price_changes": True,
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

        if json_observation.details.get("internal_conflict"):
            return self._conflict(
                result,
                "The exact-item Walmart JSON contains contradictory stock signals.",
            )

        if visual_observation.details.get("internal_conflict"):
            return self._conflict(
                result,
                "The primary rendered Walmart purchase block contains "
                "contradictory stock signals.",
            )

        if not json_observation.exact_item_match:
            return self._partial(
                result,
                json_observation=json_observation,
                visual_observation=visual_observation,
                reason=(
                    "Exact-item Walmart JSON could not be tied to the requested "
                    f"item ID {item_id}."
                ),
            )

        if not visual_observation.title_match:
            return self._partial(
                result,
                json_observation=json_observation,
                visual_observation=visual_observation,
                reason=(
                    "The rendered Walmart evidence could not be tied safely "
                    "to the requested primary product."
                ),
            )

        if not json_observation.conclusive or not visual_observation.conclusive:
            missing = []
            if not json_observation.conclusive:
                missing.append("exact-item JSON")
            if not visual_observation.conclusive:
                missing.append("primary rendered buy box")
            return self._partial(
                result,
                json_observation=json_observation,
                visual_observation=visual_observation,
                reason=(
                    "Walmart requires two independent conclusive sources; "
                    + " and ".join(missing)
                    + " was inconclusive."
                ),
            )

        if json_observation.stock != visual_observation.stock:
            return self._conflict(
                result,
                (
                    "Exact-item JSON and the primary rendered buy box disagree: "
                    f"{json_observation.stock.value} versus "
                    f"{visual_observation.stock.value}."
                ),
            )

        stock = json_observation.stock
        result.observed_stock = stock
        result.quantity = self._agreed_quantity(
            json_observation,
            visual_observation,
        )
        result.seller = visual_observation.seller or json_observation.seller

        if stock == StockState.IN_STOCK:
            if json_observation.price is None or visual_observation.price is None:
                return self._partial(
                    result,
                    json_observation=json_observation,
                    visual_observation=visual_observation,
                    reason=(
                        "Normal Walmart In Stock requires a price from both "
                        "exact-item JSON and the primary rendered buy box."
                    ),
                    observed_stock=stock,
                )

            if not self._prices_equal(
                json_observation.price,
                visual_observation.price,
            ):
                return self._conflict(
                    result,
                    (
                        "Exact-item JSON and the primary rendered buy box "
                        f"show different prices: ${json_observation.price:.2f} "
                        f"versus ${visual_observation.price:.2f}."
                    ),
                    observed_stock=stock,
                )

            result.price = visual_observation.price
            result.metadata.update(
                {
                    "walmart_price_sources_agree": True,
                    "walmart_verified_price": f"{result.price:.2f}",
                    "walmart_price_change_requires_review": True,
                }
            )

        else:
            # OOS and every low-inventory state intentionally carry no sheet
            # price after apply_stock_policy().
            result.price = None
            result.metadata["walmart_price_sources_agree"] = None

        result.verification = VerificationStatus.VERIFIED
        result.policy_reason = (
            "Exact-item Walmart JSON and the primary rendered buy box agree "
            f"on {stock.value}."
        )
        return apply_stock_policy(result)

    @staticmethod
    def _prices_equal(first: Decimal, second: Decimal) -> bool:
        return abs(first - second) <= Decimal("0.01")

    @staticmethod
    def _agreed_quantity(
        first: WalmartObservation,
        second: WalmartObservation,
    ) -> Optional[int]:
        if first.quantity is not None and second.quantity is not None:
            return first.quantity if first.quantity == second.quantity else None
        return (
            first.quantity
            if first.quantity is not None
            else second.quantity
        )

    @staticmethod
    def _blocked(result: ScrapeResult, reason: str) -> ScrapeResult:
        result.verification = VerificationStatus.BLOCKED
        result.error_message = reason
        result.policy_reason = "Blocked Walmart pages cannot update the sheet."
        return apply_stock_policy(result)

    @staticmethod
    def _error(result: ScrapeResult, reason: str) -> ScrapeResult:
        result.verification = VerificationStatus.ERROR
        result.error_message = reason
        result.policy_reason = "Walmart scrape errors cannot update the sheet."
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
    def _partial(
        result: ScrapeResult,
        *,
        json_observation: WalmartObservation,
        visual_observation: WalmartObservation,
        reason: str,
        observed_stock: StockState = StockState.UNKNOWN,
    ) -> ScrapeResult:
        result.verification = VerificationStatus.PARTIAL
        result.observed_stock = observed_stock
        result.price = visual_observation.price or json_observation.price
        result.quantity = (
            visual_observation.quantity
            if visual_observation.quantity is not None
            else json_observation.quantity
        )
        result.seller = visual_observation.seller or json_observation.seller
        result.error_message = reason
        result.policy_reason = reason
        return apply_stock_policy(result)

    @staticmethod
    def classify_rendered_signals(
        signals: dict[str, Any],
    ) -> tuple[StockState, Optional[int], str, bool, str]:
        """Classify only the requested item's primary rendered purchase block."""

        if not signals.get("regionFound"):
            return (
                StockState.UNKNOWN,
                None,
                str(
                    signals.get("reason")
                    or "Primary Walmart purchase region was not found."
                ),
                False,
                "",
            )

        if not signals.get("exactItemAnchor"):
            return (
                StockState.UNKNOWN,
                None,
                "Rendered purchase evidence was not anchored to the requested item.",
                False,
                "",
            )

        enabled_cta = bool(signals.get("enabledCta"))
        disabled_cta = bool(signals.get("disabledCta"))
        selected_option_oos = bool(signals.get("selectedOptionOos"))
        product_oos = bool(signals.get("productOos"))
        inventory_text = str(signals.get("inventoryText") or "")
        fulfillment = signals.get("fulfillment") or {}

        fulfillment_states: list[str] = []
        if isinstance(fulfillment, dict):
            for value in fulfillment.values():
                if isinstance(value, dict):
                    state = str(value.get("state") or "UNKNOWN").upper()
                else:
                    state = str(value or "UNKNOWN").upper()
                if state in {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"}:
                    fulfillment_states.append(state)

        available_methods = fulfillment_states.count("AVAILABLE")
        unavailable_methods = fulfillment_states.count("UNAVAILABLE")

        if enabled_cta:
            if selected_option_oos or product_oos:
                return (
                    StockState.UNKNOWN,
                    None,
                    (
                        "The primary Add to cart control is enabled, but the "
                        "same primary purchase block also reports product OOS."
                    ),
                    True,
                    "product",
                )

            stock, quantity = parse_inventory_detail(
                inventory_text,
                available=True,
            )

            if stock == StockState.QUANTITY_REMAINING:
                reason = f"Primary buy box reports only {quantity} remaining."
            elif stock == StockState.LOW_STOCK:
                reason = "Primary buy box reports Low Stock."
            elif stock == StockState.LIMITED_STOCK:
                reason = "Primary buy box reports Limited Stock."
            else:
                reason = "Enabled primary Add to cart control."

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
                "The primary product purchase block explicitly reports OOS.",
                False,
                "product",
            )

        if (
            len(fulfillment_states) >= 2
            and unavailable_methods == len(fulfillment_states)
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
                    "independent product-level OOS evidence."
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

        if unavailable_methods:
            return (
                StockState.UNKNOWN,
                None,
                (
                    "One fulfillment method is unavailable, but that alone "
                    "does not prove whole-product OOS."
                ),
                False,
                "",
            )

        return (
            StockState.UNKNOWN,
            None,
            "Primary rendered Walmart evidence was inconclusive.",
            False,
            "",
        )


def parse_inventory_detail(
    text: str,
    *,
    quantity: Optional[int] = None,
    available: bool = True,
) -> tuple[StockState, Optional[int]]:
    """Return the most specific selected-item inventory state."""

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
        return StockState.QUANTITY_REMAINING, int(match.group(1))

    if "low stock" in normalized:
        return StockState.LOW_STOCK, quantity

    if "limited stock" in normalized:
        return StockState.LIMITED_STOCK, quantity

    if available:
        return StockState.IN_STOCK, quantity

    return StockState.UNKNOWN, quantity
