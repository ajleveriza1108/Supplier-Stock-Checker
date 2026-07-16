"""Local AI cross-check for supplier changes.

The verifier is deliberately a second opinion. It never replaces supplier
scrapers and never auto-approves when evidence is missing or ambiguous.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from core.local_ai_config import LocalAIConfig, project_root
from core.local_ai_server import LocalAIServerError, LocalAIServerManager


SYSTEM_PROMPT = """You are a conservative e-commerce change verifier.

You receive the previous Google Sheet values, a supplier scraper's changed
result, the selected variation, structured scraper evidence, and visible page
text. Decide whether the detected CHANGE is supported by the evidence.

Rules:
1. Evaluate only the requested product, selected variation, primary offer, and
   intended seller. Ignore recommendations, sponsored products, other colors,
   other sizes, other conditions, and other sellers.
2. Low Stock, Limited Stock, Backorder, and Only N Remaining are treated as OOS.
3. One unavailable fulfillment method does not make a product OOS when another
   valid method is available.
4. A recommendation-card Add to cart button does not make the requested item
   In Stock.
5. Use UNCERTAIN when the page is blocked, incomplete, ambiguous, or the
   selected item cannot be tied to the evidence.
6. Never invent a price, stock state, seller, variant, or page element.
7. Return one JSON object only. Do not use Markdown.

Required JSON:
{
  "verdict": "CONFIRM" | "REJECT" | "UNCERTAIN",
  "normalized_stock": "IN_STOCK" | "OOS" | "LOW_STOCK" |
                      "LIMITED_STOCK" | "QUANTITY_REMAINING" | "UNKNOWN",
  "price_matches": true | false | null,
  "selected_variant_matches": true | false | null,
  "primary_offer_matches": true | false | null,
  "confidence": 0.0,
  "reason": "brief evidence-based reason"
}
"""


@dataclass(slots=True)
class VerificationResult:
    verdict: str
    normalized_stock: str
    price_matches: bool | None
    selected_variant_matches: bool | None
    primary_offer_matches: bool | None
    confidence: float
    reason: str
    raw_response: str = ""
    elapsed_seconds: float = 0.0

    @property
    def is_confirmed(self) -> bool:
        return self.verdict == "CONFIRM"

    @property
    def is_rejected(self) -> bool:
        return self.verdict == "REJECT"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalAIChangeVerifier:
    """Verify only changed rows using a local llama.cpp model."""

    def __init__(self, config: LocalAIConfig | None = None) -> None:
        self.config = config or LocalAIConfig.load()
        self.server = LocalAIServerManager(self.config)
        self._supplier_rules = self._load_supplier_rules()

    def reload_config(self) -> None:
        new_config = LocalAIConfig.load()
        if new_config != self.config:
            self.server.stop()
            self.config = new_config
            self.server = LocalAIServerManager(self.config)

    def test(self) -> tuple[bool, str]:
        try:
            self.server.ensure_running()
            response = self.server.post_json(
                "/v1/chat/completions",
                {
                    "model": self.server.model_id(),
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                'Return the required JSON. Evidence: '
                                '{"previous_stock":"In Stock",'
                                '"detected_stock":"OOS",'
                                '"page_text":"Selected item is out of stock."}'
                                "\n/no_think"
                            ),
                        },
                    ],
                    "temperature": 0,
                    "max_tokens": 220,
                    "stream": False,
                },
                timeout=min(60, int(self.config.timeout_seconds)),
            )
            parsed = self._parse_result(self._response_text(response))
            if parsed.verdict not in {"CONFIRM", "REJECT", "UNCERTAIN"}:
                return False, "The model did not return the expected JSON."
            return True, f"Local AI ready: {self.server.model_id()}"
        except (LocalAIServerError, ValueError) as exc:
            return False, str(exc)

    def verify_change(
        self,
        *,
        row_num: int,
        supplier: str,
        url: str,
        detected_price: str,
        detected_stock: str,
        previous_price: str,
        previous_stock: str,
        title: str = "",
        selected_variation: str = "",
        variants: Iterable[Any] | None = None,
    ) -> VerificationResult:
        started = time.monotonic()
        self.reload_config()

        ready, message = self.config.readiness()
        if not self.config.enabled:
            return self._uncertain(
                "Local AI cross-check is disabled.",
                started=started,
            )
        if not ready:
            return self._uncertain(message, started=started)

        stock_changed = (
            self._stock_bucket(previous_stock)
            != self._stock_bucket(detected_stock)
        )
        price_changed = self._price_changed(previous_price, detected_price)

        if not stock_changed and not price_changed:
            return self._uncertain(
                "No sheet change was found, so AI verification was skipped.",
                started=started,
            )

        page_text, fetch_note = self._fetch_visible_text(url)
        evidence = {
            "row_number": int(row_num),
            "supplier": str(supplier or ""),
            "product_url": str(url or ""),
            "product_title": str(title or ""),
            "selected_variation": str(selected_variation or ""),
            "previous_sheet": {
                "price": str(previous_price or ""),
                "stock": str(previous_stock or ""),
            },
            "detected_change": {
                "price": str(detected_price or ""),
                "stock": str(detected_stock or ""),
                "price_changed": bool(price_changed),
                "stock_changed": bool(stock_changed),
                "business_stock_bucket": self._stock_bucket(detected_stock),
            },
            "supplier_rules": self._supplier_rules.get(
                str(supplier or ""),
                "Use conservative standard e-commerce evidence rules.",
            ),
            "scraper_variants_and_evidence": self._compact_value(
                list(variants or [])
            ),
            "page_fetch_note": fetch_note,
            "visible_page_text": page_text,
        }

        prompt = (
            "Cross-check this detected sheet change. "
            "Return only the required JSON.\n"
            + json.dumps(evidence, ensure_ascii=False, indent=2)
            + "\n/no_think"
        )

        try:
            self.server.ensure_running()
            response = self.server.post_json(
                "/v1/chat/completions",
                {
                    "model": self.server.model_id(),
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 420,
                    "stream": False,
                },
                timeout=float(self.config.timeout_seconds),
            )
            result = self._parse_result(self._response_text(response))
        except (LocalAIServerError, ValueError) as exc:
            return self._uncertain(
                f"Local AI could not complete the cross-check: {exc}",
                started=started,
            )

        result.elapsed_seconds = round(time.monotonic() - started, 2)
        return self._apply_safety_gate(
            result=result,
            detected_stock=detected_stock,
            stock_changed=stock_changed,
            price_changed=price_changed,
        )

    def _apply_safety_gate(
        self,
        *,
        result: VerificationResult,
        detected_stock: str,
        stock_changed: bool,
        price_changed: bool,
    ) -> VerificationResult:
        threshold = float(self.config.confidence_threshold)
        model_bucket = self._stock_bucket(result.normalized_stock)
        detected_bucket = self._stock_bucket(detected_stock)

        if result.confidence < threshold:
            result.verdict = "UNCERTAIN"
            result.reason = (
                f"{result.reason} Confidence {result.confidence:.2f} is "
                f"below the required {threshold:.2f}."
            ).strip()
            return result

        if result.selected_variant_matches is False:
            result.verdict = "REJECT"
            result.reason = (
                result.reason
                or "The evidence does not apply to the selected variation."
            )
            return result

        if result.primary_offer_matches is False:
            result.verdict = "REJECT"
            result.reason = (
                result.reason
                or "The evidence does not apply to the primary offer."
            )
            return result

        if stock_changed:
            if model_bucket == "UNKNOWN":
                result.verdict = "UNCERTAIN"
                result.reason = (
                    result.reason
                    or "The selected item's stock could not be determined."
                )
            elif model_bucket == detected_bucket:
                if result.verdict != "REJECT":
                    result.verdict = "CONFIRM"
            else:
                result.verdict = "REJECT"
                result.reason = (
                    result.reason
                    or (
                        f"The evidence resolves to {model_bucket}, "
                        f"not the scraper's {detected_bucket}."
                    )
                )

        if price_changed and self.config.verify_price_changes:
            if result.price_matches is False:
                result.verdict = "REJECT"
            elif result.price_matches is None and not stock_changed:
                result.verdict = "UNCERTAIN"
                result.reason = (
                    result.reason
                    or "The selected primary price could not be verified."
                )

        return result

    @staticmethod
    def _response_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ValueError("The model response did not include a completion.")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()

        raise ValueError("The model response did not contain text.")

    @classmethod
    def _parse_result(cls, text: str) -> VerificationResult:
        data = cls._extract_json(text)

        verdict = str(data.get("verdict") or "UNCERTAIN").strip().upper()
        if verdict not in {"CONFIRM", "REJECT", "UNCERTAIN"}:
            verdict = "UNCERTAIN"

        normalized_stock = str(
            data.get("normalized_stock") or "UNKNOWN"
        ).strip().upper()
        if normalized_stock not in {
            "IN_STOCK",
            "OOS",
            "LOW_STOCK",
            "LIMITED_STOCK",
            "QUANTITY_REMAINING",
            "UNKNOWN",
        }:
            normalized_stock = "UNKNOWN"

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        return VerificationResult(
            verdict=verdict,
            normalized_stock=normalized_stock,
            price_matches=cls._nullable_bool(data.get("price_matches")),
            selected_variant_matches=cls._nullable_bool(
                data.get("selected_variant_matches")
            ),
            primary_offer_matches=cls._nullable_bool(
                data.get("primary_offer_matches")
            ),
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(data.get("reason") or "").strip()[:800],
            raw_response=text[:4000],
        )

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        start = cleaned.find("{")
        if start < 0:
            raise ValueError("The local AI did not return JSON.")

        depth = 0
        in_string = False
        escaped = False

        for index in range(start, len(cleaned)):
            char = cleaned[index]

            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    parsed = json.loads(cleaned[start : index + 1])
                    if isinstance(parsed, dict):
                        return parsed
                    break

        raise ValueError("The local AI returned incomplete JSON.")

    @staticmethod
    def _nullable_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value

        normalized = str(value).strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
        return None

    @staticmethod
    def _stock_bucket(value: str) -> str:
        normalized = re.sub(
            r"[\s_-]+",
            " ",
            str(value or ""),
        ).strip().lower()

        if not normalized or normalized in {
            "unknown",
            "unable to verify",
            "conflict",
            "error",
        }:
            return "UNKNOWN"

        if (
            "out of stock" in normalized
            or normalized == "oos"
            or "sold out" in normalized
            or "backorder" in normalized
            or "low stock" in normalized
            or "limited stock" in normalized
            or re.search(
                r"\bonly\s+\d+\s+(?:remaining|left|in stock)\b",
                normalized,
            )
        ):
            return "OOS"

        if "in stock" in normalized or normalized == "instock":
            return "IN_STOCK"

        return "UNKNOWN"

    @staticmethod
    def _price_changed(previous: str, detected: str) -> bool:
        before = LocalAIChangeVerifier._decimal_price(previous)
        after = LocalAIChangeVerifier._decimal_price(detected)
        if after is None:
            return False
        if before is None:
            return True
        return abs(before - after) > Decimal("0.001")

    @staticmethod
    def _decimal_price(value: str) -> Decimal | None:
        match = re.search(
            r"-?\d+(?:,\d{3})*(?:\.\d{1,2})?",
            str(value or ""),
        )
        if not match:
            return None

        try:
            return Decimal(match.group(0).replace(",", ""))
        except InvalidOperation:
            return None

    @staticmethod
    def _fetch_visible_text(url: str) -> tuple[str, str]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/150 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(2_000_000).decode(
                    "utf-8",
                    errors="replace",
                )
                status = getattr(response, "status", 200)
        except (OSError, urllib.error.URLError) as exc:
            return "", f"Page text request failed: {exc}"

        raw = re.sub(
            r"<(?:script|style|noscript)\b[^>]*>.*?</"
            r"(?:script|style|noscript)>",
            " ",
            raw,
            flags=re.I | re.S,
        )
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        text = re.sub(r"\s+", " ", text).strip()

        blocked = any(
            phrase in text.lower()
            for phrase in (
                "access denied",
                "pardon our interruption",
                "are you a human",
                "verify you are human",
                "enable javascript",
                "captcha",
            )
        )

        if blocked:
            return (
                text[:2500],
                f"HTTP {status}; page may be blocked or incomplete.",
            )

        return text[:14000], f"HTTP {status}; visible text collected."

    @staticmethod
    def _compact_value(value: Any, *, depth: int = 0) -> Any:
        if depth > 4:
            return "[truncated]"

        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for key, item in list(value.items())[:40]:
                if str(key).lower() in {"html", "page_source", "raw_html"}:
                    continue
                output[str(key)] = LocalAIChangeVerifier._compact_value(
                    item,
                    depth=depth + 1,
                )
            return output

        if isinstance(value, (list, tuple)):
            return [
                LocalAIChangeVerifier._compact_value(
                    item,
                    depth=depth + 1,
                )
                for item in list(value)[:30]
            ]

        if isinstance(value, str):
            return value[:1200] + ("…" if len(value) > 1200 else "")

        if isinstance(value, (int, float, bool)) or value is None:
            return value

        return str(value)[:500]

    @staticmethod
    def _load_supplier_rules() -> dict[str, str]:
        path = project_root() / "config" / "ai_prompts.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        return {
            str(key): str(value)
            for key, value in data.items()
            if isinstance(value, (str, int, float))
        }

    @staticmethod
    def _uncertain(reason: str, *, started: float) -> VerificationResult:
        return VerificationResult(
            verdict="UNCERTAIN",
            normalized_stock="UNKNOWN",
            price_matches=None,
            selected_variant_matches=None,
            primary_offer_matches=None,
            confidence=0.0,
            reason=reason,
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
