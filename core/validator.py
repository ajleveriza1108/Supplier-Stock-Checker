from __future__ import annotations

import re
import threading
from typing import Optional

try:
    from core.smart_tools import OllamaAI
except ImportError:
    from smart_tools import OllamaAI


class SuspicionValidator:
    def __init__(self) -> None:
        self.pending_items: list[dict] = []
        self.pass1_data: dict[int, dict] = {}
        self.last_reason = ""
        self._lock = threading.RLock()

    def flag(self, item_dict: dict) -> None:
        with self._lock:
            self.pending_items.append(item_dict)

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self.pending_items)

    def pop_pending(self) -> list[dict]:
        with self._lock:
            items = list(self.pending_items)
            self.pending_items.clear()
            return items

    @staticmethod
    def _normalise_price(
        value: object,
    ) -> str:
        text = (
            str(value or "")
            .strip()
            .upper()
            .replace(",", "")
        )

        if text in {
            "",
            "N/A",
            "NA",
            "NONE",
            "NULL",
        }:
            return ""

        match = re.search(
            r"-?\d+(?:\.\d{1,2})?",
            text,
        )

        return (
            match.group(0)
            if match
            else text
        )

    @staticmethod
    def _normalise_stock(
        value: object,
    ) -> str:
        text = re.sub(
            r"\s+",
            " ",
            str(value or "").strip().upper(),
        )

        aliases = {
            "INSTOCK": "IN STOCK",
            "AVAILABLE": "IN STOCK",
            "OUT OF STOCK": "OOS",
            "UNAVAILABLE": "OOS",
        }

        return aliases.get(text, text)

    def verify(
        self,
        row_num: int,
        url: str,
        price: str,
        stock: str,
        ai_mode: str,
        supplier: str = "",
    ) -> Optional[bool]:
        try:
            with self._lock:
                phase1 = self.pass1_data.get(
                    row_num
                )

            if not phase1:
                self.last_reason = (
                    "Phase 1 data is missing."
                )
                return None

            phase1_price = (
                self._normalise_price(
                    phase1.get("price")
                )
            )
            phase2_price = (
                self._normalise_price(price)
            )

            phase1_stock = (
                self._normalise_stock(
                    phase1.get("stock")
                )
            )
            phase2_stock = (
                self._normalise_stock(stock)
            )

            if (
                phase1_price != phase2_price
                or phase1_stock != phase2_stock
            ):
                self.last_reason = (
                    "Phase 1 and Phase 2 disagree: "
                    f"price {phase1_price!r} "
                    f"-> {phase2_price!r}, "
                    f"stock {phase1_stock!r} "
                    f"-> {phase2_stock!r}."
                )
                return False

            mode = (
                str(ai_mode or "")
                .strip()
                .lower()
            )

            if mode in {
                "",
                "off",
                "regular",
                "regular (regex)",
                "regex",
            }:
                self.last_reason = (
                    "Phase 1 and Phase 2 agree."
                )
                return True

            decision = OllamaAI(
                model_string=ai_mode
            ).verify_scrape(
                url,
                price,
                stock,
                supplier=supplier,
            )

            if decision is True:
                self.last_reason = (
                    "Phase 1, Phase 2, "
                    "and AI agree."
                )
            elif decision is False:
                self.last_reason = (
                    "AI rejected the result."
                )
            else:
                self.last_reason = (
                    "AI verification "
                    "was inconclusive."
                )

            return decision

        except Exception as exc:
            self.last_reason = (
                "Validator error: "
                f"{type(exc).__name__}: {exc}"
            )
            return None
