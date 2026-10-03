# Phase 4.1 Defects

## SS41-001 — HIGH — Product preview O(N²)

- Reproduction: prepare thousands of eligible product payloads; queue persistence linearly searched the full prepared list per product.
- Root cause: `next(...)` over a list during a second catalog pass.
- Fix: index prepared items by MASTER product ID for O(1) lookup.
- Regression: `test_10k_product_sync_preview` verifies 10,000 CREATE intents with a 100-row response cap.
- Verification: focused stress suite and full suite.

## SS41-002 — HIGH — Completion collection source mismatch

- Reproduction: use real `store_collection_plans/store_collection_definitions`; completion returned zero collections and no plan reference.
- Root cause: Phase 4.0 lookup preferred legacy fixture table names.
- Fix: real Phase 3.2 tables are authoritative; legacy names are fallback only.
- Regression: all scale profiles assert exact product/collection counts.
- Verification: SMALL through MASSIVE passed.

## SS41-003 — HIGH — Incomplete secret redaction

- Reproduction: OpenAI, Bearer or Cookie-shaped secrets passed through UI/report errors.
- Root cause: separate narrow regular expressions.
- Fix: shared `security.py` redacts Shopify, OpenAI, Bearer, token assignment and Cookie forms in UI and reports.
- Regression: `test_secrets_redacted_from_all_outputs` plus existing credential tests.
- Verification: focused and full suites.

## SS41-004 — MEDIUM — Numeric collection term overlap

- Reproduction: `collection-1` also matched `collection-10` through `collection-19` in the stress matcher.
- Root cause: raw substring matching in the qualification helper.
- Fix: deterministic Unicode-aware term-boundary patterns.
- Regression: `test_50k_collection_matching` compares two full runs and exact totals.
- Verification: MASSIVE collection profile passed.

## SS41-005 — MEDIUM — Windows system temp ACL assumption

- Reproduction: harness scratch creation under a stale/inaccessible `%TEMP%` root raised WinError 5.
- Root cause: system temp was assumed readable.
- Fix: harness scratch is created below its own output folder with cleanup-error tolerance.
- Regression: Unicode/path and permission-denied tests; standard/massive harness execution.
- Verification: both harness modes passed outside the managed sandbox ACL.

## SS41-006 — MEDIUM — Completion safe-apply stale plan

- Reproduction: create a second completion plan, then apply selections from the first.
- Root cause: safe apply did not compare against the latest store plan.
- Fix: stale plan returns `CONFLICT` with zero remote writes.
- Regression: `test_stale_preview_all_mutating_modules`.
- Verification: focused suite passed.

No CRITICAL defects remain. Expected/low findings are recorded in the stress plan and do not bypass a launch blocker.
