# Phase 2.6 — Batch Sourcing Queue

## Operator flow

1. Start UI V2 and choose a Store.
2. In Sourcing → API-free browser sourcing, choose a saved keyword and target count.
3. Start a Batch. ShopSource opens the Amazon search page; the operator reviews it and presses the extension's “capture current results” control.
4. The bridge merges unique ASINs into the active Store/keyword batch and reports this page's added/duplicate counts. It only reads result cards present in the current page DOM; pagination remains a manual action.
5. Once queued, the extension opens one detail page at a time using a URL fragment marker. The fragment is not sent in the Amazon HTTP request. A successful detail capture advances after at least four seconds.
6. DETAIL_COMPLETE items can be imported to MASTER automatically. Existing importer/upsert and Store classifier are reused.
7. The operator reviews decisions and creates a small Spark Center package. Browser Capture → Spark Center mapping and portal upload remain unverified.

## Queue and safety

`browser_batch_runs`, `browser_batch_items`, and `browser_batch_events` are additive SQLite tables. Run state and item state are persisted so the operator can pause, resume, cancel, inspect active runs after restart, and retry failures. A run has a unique ASIN constraint per item. Existing MASTER ASINs are recorded as duplicates; excluded keyword matches are retained as `PREFILTER_REJECTED`, not deleted from MASTER. Price never permanently excludes a candidate. Risk terms affect queue priority only; classification remains the existing Store classifier's responsibility.

The current implementation intentionally uses concurrency 1, a 4-second minimum detail-open interval, and a maximum of two automatic retries per item. CAPTCHA/robot-check detection pauses with `PAUSED_NEEDS_USER`; it does not solve or evade the challenge. No automatic search pagination, background page flood, browser storage, cookies, credentials, or telemetry are used. The extension `tabs` permission is used only to create the next Amazon detail tab in the explicitly started batch; the queued URL is host-checked before opening.

Search capture permits a missing title when ASIN and the observed Amazon URL are present. Such a candidate can be queued for detail enrichment, but MASTER import still requires the regular detail payload validation (ASIN and title). Search-only import threshold remains conservative and is not enabled by default.

## Current scope limits

The UI currently exposes candidate-count targeting, a target size, automatic MASTER import, one-at-a-time detail processing, and batch lifecycle controls. Primary-count target selection, multi-keyword batch execution/statistics, user-selectable 20/50/100 chunking, and configurable concurrency up to three are not implemented in this phase. A shortage of current-page results ends the available queue; the operator must navigate to and capture another search page. Begin live testing at five products, then validate the Spark Center portal manually before increasing batch size.
