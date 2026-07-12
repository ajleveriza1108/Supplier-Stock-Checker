# Supplier Scraper Contract

## Purpose

This contract defines the result format, verification states, business-stock
rules, logging rules, and engine safety requirements for every supplier
scraper in Supplier Stock Checker.

The immediate implementation applies the contract to Walmart while preserving
the legacy behavior of the remaining suppliers until each one is migrated and
tested separately.

## Processing layers

Every supplier result must pass through these layers in order:

1. **Extraction** — collect raw observations without applying sheet rules.
2. **Supplier verification** — compare independent evidence sources.
3. **Business policy** — convert observed inventory into the sheet value.
4. **Engine safety gate** — allow only verified, internally consistent values.
5. **Review workflow** — compare safe values with the existing sheet.
6. **Google Sheets write** — apply only user-approved or explicitly allowed
   updates.

A layer must not silently perform another layer's responsibility.

## Structured result

`core.scrape_result.ScrapeResult` is the canonical result object.

Required identity fields:

- `supplier`
- `url`
- `item_id`
- `title`
- `final_url`

Required decision fields:

- `verification`
- `observed_stock`
- `price`
- `quantity`
- `sheet_stock`
- `policy_reason`
- `error_message`
- `evidence`

## Verification states

| State | Meaning | Sheet update |
|---|---|---|
| `VERIFIED` | Independent required evidence agrees | Allowed after policy |
| `PARTIAL` | Only one required source is conclusive | Never |
| `CONFLICT` | Required sources disagree | Never |
| `UNKNOWN` | Evidence is missing or inconclusive | Never |
| `BLOCKED` | Captcha, access block, or security page | Never |
| `ERROR` | Navigation, parsing, or internal failure | Never |

The word `Success` in the legacy tuple is reserved for a structured,
policy-safe `VERIFIED` result.

## Stock states

| Observed state | Sheet value |
|---|---|
| `IN_STOCK` | `In Stock`, with a verified price |
| `OOS` | `OOS`, blank price |
| `LOW_STOCK` | `OOS`, blank price |
| `LIMITED_STOCK` | `OOS`, blank price |
| `QUANTITY_REMAINING` | `OOS`, blank price |
| `UNKNOWN` | No update |

The raw state must remain available in structured metadata even when the sheet
value is converted to `OOS`.

Examples:

```text
Observed: LOW_STOCK
Sheet: OOS
Reason: Low Stock is treated as OOS.
```

```text
Observed: QUANTITY_REMAINING
Quantity: 6
Sheet: OOS
Reason: Only 6 remaining is treated as OOS.
```

## Walmart verification rules

Walmart requires two independent sources:

1. Exact-item product data from `__NEXT_DATA__` or another exact-item JSON
   payload.
2. A rendered primary-product candidate that is not inside recommendations,
   carousels, sponsored modules, search results, or similar-item containers.

### Verified In Stock

A normal In Stock result requires all of the following:

- URL item ID extracted.
- Exact item ID matched in JSON.
- JSON availability is available.
- Rendered primary product control is available.
- JSON and rendered stock agree.
- JSON and rendered prices both exist.
- Prices agree within one cent.
- No block page or contradictory signal exists.

### Verified OOS

OOS requires:

- Exact-item JSON OOS evidence; and
- Rendered primary product OOS evidence.

A blank price alone is never OOS evidence.

### Low inventory

`Low Stock`, `Limited Stock`, and `Only N remaining` must be detected as raw
inventory states. When the JSON and rendered product evidence are compatible,
the business policy converts them to `OOS`.

### Disagreement

Examples that must become `CONFLICT`:

- JSON OOS, rendered In Stock.
- JSON In Stock, rendered OOS.
- JSON price `$51.29`, rendered price `$56.99`.
- Contradictory OOS and Add-to-cart signals within the same source.

### Missing visual confirmation

When exact-item JSON is conclusive but rendered primary-product evidence is not
found, the result is `PARTIAL`. A partial result must not enter the Review
Window as an update.

## Legacy compatibility

Supplier scrapers currently return:

```python
status, price, stock, title, variants, logs
```

Structured suppliers place the serialized `ScrapeResult` inside each variant
under `_structured_result`.

`core.engine_guard.EngineResultGuard` reads that metadata before the existing
engine comparison logic.

- Structured and safe: converted back to the existing values.
- Structured and unsafe: rejected before update comparison.
- Walmart `Success` without structured metadata: rejected.
- Other suppliers: legacy behavior remains unchanged until migrated.

## Phase 2 handling

When a structured Walmart result becomes unsafe during Phase 2, the guard
passes it to the existing engine as an `Error`. This allows the current engine
to revert Phase 1 statistics and clear the Phase 2 state correctly.

## Logging

Structured events are written to:

```text
logs/structured_engine_*.jsonl
```

Each event includes:

- Timestamp
- Run ID
- Sequence number
- Supplier
- Row
- URL
- Phase
- Event
- Verification state
- Observed stock
- Sheet stock
- Price
- Quantity
- Reason

Console logs remain readable, but JSONL is the authoritative audit trail.

## Row ordering

Sequential processing must wait until the UI consumes and resolves the
previous result before announcing the next row. The engine patch uses a
result-acknowledgement token for this purpose.

Concurrent mode does not promise visual console ordering; its structured log
uses row numbers and sequence numbers to remain unambiguous.

## Testing requirements

Before a supplier is considered migrated:

1. Compile all affected Python files.
2. Run policy and engine-guard unit tests.
3. Run supplier-only with Review Mode ON.
4. Test known In Stock, OOS, low-stock, limited-stock, quantity, blocked, and
   conflict pages.
5. Confirm no `PARTIAL`, `CONFLICT`, `UNKNOWN`, `BLOCKED`, or `ERROR` result
   creates a pending update.
6. Audit every pending update against its evidence.
7. Test Google Sheet writes only after the evidence audit passes.

## Migration order

Migrate suppliers one at a time:

1. Walmart
2. Sportsman's Guide
3. Harbor Freight
4. WebstaurantStore
5. Menards
6. Lakeside
7. Collections Etc.

Do not alter an untested supplier merely to make it resemble another supplier.
Each site needs its own extraction and verification policy.
