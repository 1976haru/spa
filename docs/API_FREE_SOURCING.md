# API-free browser sourcing

Phase 2.6 adds persistent batches for candidate dedupe, sequential detail enrichment, optional automatic MASTER import/classification, and pause/resume/cancel/retry. It captures all qualifying product cards currently rendered in the active search page DOM, not only cards inside the viewport; it never auto-paginates. Batch operation and safety limits are documented in [BATCH_SOURCING.md](BATCH_SOURCING.md).

ShopSource Capture is a user-started Chrome/Edge Manifest V3 extension for recording Amazon.com product information visible in the active browser page. It has no Keepa dependency and sends captured page fields only to the local ShopSource UI at `127.0.0.1:8081`.

## Workflow

1. Start UI V2 and choose a Store.
2. In Settings, create a local browser pairing code. Configure the unpacked extension with that code, Store ID, and `http://127.0.0.1:8081`.
3. Use Keyword Studio recommendations and open one Amazon search keyword.
4. Capture the currently visible search results. Pagination is manual.
5. Open up to five candidate detail tabs at a time and click the extension's product capture button.
6. Send `DETAIL_COMPLETE` candidates to MASTER. The ordinary importer/upsert path records BROWSER_CAPTURE occurrences and runs the existing Store classifier.
7. Review Store decisions, create the ordinary Spark Center package, and upload only `ready/<package_id>` manually.

The extension does not read browser cookies, Local Storage, Session Storage, authorization headers, accounts, addresses, or order details; does not download image binaries; and does not transmit analytics. CAPTCHA/robot-check pages stop capture. CAPTCHA/WAF evasion, stealth browsing, proxy rotation, API scraping, and unattended pagination are not implemented.

Search-page fields may be incomplete. Search candidates remain in the capture queue; detail capture is recommended before MASTER import. Missing values are left absent/null rather than inferred. The canonical BROWSER_CAPTURE-to-Spark mapping is `BROWSER_CAPTURE_TO_SPARK_MAPPING_UNVERIFIED` until a real Spark Center portal round-trip is manually verified. Image reuse rights and Amazon page structure must be reviewed by the operator; selectors may require maintenance after Amazon HTML changes.

This workflow has no API usage cost, but it is a human-operated candidate workflow, not a fully unattended 30,000-product sourcing engine. Keepa remains an optional paid automation provider for larger/repeatable sourcing runs.
