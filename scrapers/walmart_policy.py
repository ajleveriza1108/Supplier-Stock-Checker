"""Walmart-specific verification policy.

Extraction and business policy are deliberately separate.  The scraper
records JSON and rendered-page observations; this module decides whether
those observations agree strongly enough to permit a sheet update.
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
    """Resolve JSON and primary rendered-page evidence."""

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
        result.metadata["json_reason"] = json_observation.reason
        result.metadata["visual_reason"] = visual_observation.reason
        result.metadata["json_text_excerpt"] = json_observation.text[:1200]
        result.metadata["visual_text_excerpt"] = visual_observation.text[:1200]

        if blocked_reason:
            result.verification = VerificationStatus.BLOCKED
            result.error_message = blocked_reason
            result.policy_reason = "Blocked pages cannot update the sheet."
            return apply_stock_policy(result)

        if error_message:
            result.verification = VerificationStatus.ERROR
            result.error_message = error_message
            result.policy_reason = "Scrape error cannot update the sheet."
            return apply_stock_policy(result)

        if (
            json_observation.details.get("internal_conflict")
            or visual_observation.details.get("internal_conflict")
        ):
            result.verification = VerificationStatus.CONFLICT
            result.error_message = (
                "One evidence source contained contradictory stock signals."
            )
            result.policy_reason = result.error_message
            return apply_stock_policy(result)

        json_ok = json_observation.conclusive
        visual_ok = visual_observation.conclusive

        if json_ok and visual_ok:
            if not self._stocks_compatible(
                json_observation,
                visual_observation,
            ):
                result.verification = VerificationStatus.CONFLICT
                result.observed_stock = StockState.UNKNOWN
                result.error_message = (
                    "Exact-item JSON and the primary rendered product area "
                    f"disagree: {json_observation.stock.value} versus "
                    f"{visual_observation.stock.value}."
                )
                result.policy_reason = result.error_message
                return apply_stock_policy(result)

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
                    visual_observation.price
                    or json_observation.price
                )
                result.error_message = (
                    "Normal In Stock requires a price from both exact-item "
                    "JSON and the primary rendered product area."
                )
                result.policy_reason = result.error_message
                return apply_stock_policy(result)

            if self._price_conflict(json_observation, visual_observation):
                result.verification = VerificationStatus.CONFLICT
                result.observed_stock = resolved_stock
                result.error_message = (
                    "Exact-item JSON and the primary rendered product area "
                    f"show different prices: ${json_observation.price:.2f} "
                    f"versus ${visual_observation.price:.2f}."
                )
                result.policy_reason = result.error_message
                return apply_stock_policy(result)

            result.verification = VerificationStatus.VERIFIED
            result.observed_stock = resolved_stock
            result.price = (
                visual_observation.price
                or json_observation.price
            )
            result.quantity = (
                visual_observation.quantity
                if visual_observation.quantity is not None
                else json_observation.quantity
            )
            result.seller = (
                visual_observation.seller
                or json_observation.seller
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
    def _stocks_compatible(
        json_observation: WalmartObservation,
        visual_observation: WalmartObservation,
    ) -> bool:
        first = json_observation.stock
        second = visual_observation.stock

        if first == second:
            return True

        # Normal availability and granular low-inventory states are
        # compatible because the rendered page often exposes only an enabled
        # Add to cart control while JSON carries the exact quantity warning.
        if first in AVAILABLE_STATES and second in AVAILABLE_STATES:
            return True

        # A granular JSON low-inventory state and strong product-level visual
        # OOS evidence both map to the user's OOS business rule.  This is only
        # accepted in this direction: JSON OOS plus an enabled rendered CTA
        # remains a real conflict.
        if first in LOW_INVENTORY_STATES and second == StockState.OOS:
            return visual_observation.details.get("oos_scope") in {
                "selected_option",
                "product",
                "all_fulfillment",
            }

        return False

    @staticmethod
    def classify_rendered_signals(
        signals: dict[str, Any],
    ) -> tuple[StockState, Optional[int], str, bool, str]:
        """Classify independent rendered-page signals.

        Returns ``(stock, quantity, reason, internal_conflict, oos_scope)``.
        Fulfillment-specific messages such as ``Delivery: Not available`` do
        not make the whole item OOS when an enabled product Add to cart
        control or another available method exists.
        """

        if not signals.get("regionFound"):
            return (
                StockState.UNKNOWN,
                None,
                str(signals.get("reason") or "Primary product region not found."),
                False,
                "",
            )

        enabled_cta = bool(signals.get("enabledCta"))
        disabled_cta = bool(signals.get("disabledCta"))
        selected_option_oos = bool(signals.get("selectedOptionOos"))
        product_oos = bool(signals.get("productOos"))
        inventory_text = str(signals.get("inventoryText") or "")
        fulfillment = signals.get("fulfillment") or {}

        states = []
        if isinstance(fulfillment, dict):
            for value in fulfillment.values():
                if isinstance(value, dict):
                    state = str(value.get("state") or "UNKNOWN").upper()
                else:
                    state = str(value or "UNKNOWN").upper()
                if state in {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"}:
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
                reason = f"Enabled primary Add to cart; only {quantity} remaining."
            elif stock == StockState.LOW_STOCK:
                reason = "Enabled primary Add to cart; page reports Low Stock."
            elif stock == StockState.LIMITED_STOCK:
                reason = "Enabled primary Add to cart; page reports Limited Stock."
            else:
                reason = "Enabled primary product Add to cart control."
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
                "The primary product purchase area explicitly reports Out of stock.",
                False,
                "product",
            )

        # All-method OOS is intentionally strict.  One unavailable method is
        # normal on Walmart pages and must not override shipping or another
        # available method.  Require at least two known methods, no available
        # method, and every known method unavailable.
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
        states = {first.stock, second.stock}
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
        return abs(first.price - second.price) > Decimal("0.01")


def parse_inventory_detail(
    text: str,
    *,
    quantity: Optional[int] = None,
    available: bool = True,
) -> tuple[StockState, Optional[int]]:
    """Return the most specific available inventory state in text."""

    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()

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

    return (
        (StockState.IN_STOCK, quantity)
        if available
        else (StockState.UNKNOWN, quantity)
    )
