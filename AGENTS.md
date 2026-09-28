# Codex Operating Rules

## Mission
Build ShopSource Studio as a local-first sourcing/product decision platform capable of scaling to 100-200 Shopify stores.

## Hard rules
1. Never delete a product merely because its current price is outside the store's PRIMARY range.
2. Preserve raw source data and exclusion/restriction reasons.
3. MASTER product identity is ASIN for current Amazon/Spark imports.
4. Store-specific decisions live outside the MASTER product row.
5. Manual override must survive automatic reclassification.
6. Spark local storage is read-only.
7. Do not implement undocumented Spark Center writes until an accepted import/API/handoff contract is documented in `docs/SPARK_INTEGRATION.md`.
8. Do not implement CAPTCHA bypass, stealth bypass, credential extraction, or anti-bot evasion.
9. Never commit `data/*.sqlite3`, `.env`, credentials, or bulk product exports.
10. Windows 10/11 is the primary local target.

## Quality gate
Before completing a phase:
- `python -m compileall -q src tests`
- `pytest -q`
- run a smoke import on the provided/synthetic Spark structure
- show `git diff --check`
- summarize files changed and any unresolved assumptions

## Development pattern
One small phase per branch. Avoid giant rewrites. Preserve backwards-compatible DB migration paths once real user data exists.
