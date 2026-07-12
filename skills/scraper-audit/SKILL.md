---
name: scraper-audit
description: Audit Supplier Stock Checker scraper results and code changes against the structured scraper contract.
---

# Scraper Audit Skill

Use this skill when reviewing supplier logs, adding a supplier policy, or
changing the engine's handling of scraper results.

## Required inputs

Collect:

- Complete run log
- Supplier code
- Supplier policy code
- `core/scrape_result.py`
- `core/result_policy.py`
- `core/engine_guard.py`
- Relevant engine integration
- Known product pages for each expected state

Never request or include credentials, service-account files, VPN
authentication, browser profiles, or private keys.

## Audit procedure

1. Identify every processed row and URL.
2. Separate raw evidence from the final sheet result.
3. Confirm the exact URL item or variation was matched.
4. Confirm independent evidence sources.
5. Flag source disagreement as `CONFLICT`.
6. Flag a missing required source as `PARTIAL`.
7. Confirm low-inventory business rules preserve the raw reason.
8. Confirm OOS does not carry a sheet price.
9. Confirm normal In Stock has a verified price.
10. Confirm blocked and error pages cannot update the sheet.
11. Confirm unsafe rows do not enter the Review Window.
12. Confirm log sequence and row identity are unambiguous.

## Walmart-specific checks

Reject a Walmart update when any of these occurs:

- Visual evidence comes from a carousel, recommendation, sponsored item, or
  search-result card.
- JSON and rendered stock disagree.
- JSON and rendered prices disagree.
- Only JSON is conclusive.
- Only rendered evidence is conclusive.
- Item ID does not match the URL.
- Price is blank for normal In Stock.
- OOS is inferred only from a blank price.
- Captcha or access-block text appears.

Treat these as OOS by business policy only after verification:

- Low Stock
- Limited Stock
- Only N remaining

## Output format

Report:

- Total rows
- Verified rows
- Partial rows
- Conflict rows
- Blocked rows
- Error rows
- Low-inventory conversions
- Pending updates that are safe
- Pending updates that must be rejected
- Exact files and functions requiring repair

Do not call a result verified merely because the legacy status says
`Success`.
