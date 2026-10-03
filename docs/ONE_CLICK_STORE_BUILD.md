# One-click Store Build (Phase 3.5)

## Beginner flow

1. Connect the selected Shopify store in the collection automation settings. The Admin token stays in the OS credential store/environment and is never shown in reports.
2. Open **스토어 자동 구축**, choose `DIRECT_SHOPIFY` (recommended) or `SPARK_FALLBACK`, and create a preview. Preview reads local planning data and Shopify catalog state; it does not launch Amazon sourcing or write Shopify.
3. Select **실제 실행** and click **스토어 자동 구축 시작** only when ready. Amazon sourcing uses the existing ShopSource browser worker; the app does not bypass CAPTCHA or automate browser clicks. If a stage needs operator work, it pauses as `MANUAL_ACTION_REQUIRED` with instructions.
4. Use **일시정지**, **계속**, or **실패 항목 재시도** to resume from the persisted stage/product checkpoint. Spark requires the user to upload in SparkShopify and explicitly confirm before collection work continues.
5. Review `exports/store_build_reports/<store>/<run_id>/summary.md`, `summary.json`, and `failures.json`.

## Stages and safety

The durable run progresses through plan, sourcing, validation, product preview/sync/verification, collection planning/image/sync/verification, homepage planning, final verification, and completion. Each stage records status, counts, checkpoint, and sanitized errors. The existing Phase 3.1 campaign, Phase 3.2 planner, Phase 3.3 collection/image services, Phase 3.4 homepage planner, and Spark safe staging are called rather than copied.

`DIRECT_SHOPIFY` writes require a fresh product preview, verified `read_products` and `write_products` scopes, and the explicit live confirmation. `SPARK_FALLBACK` creates and safely stages a package, then waits for manual SparkShopify confirmation; no Spark GUI clicking occurs. Paid collection-image generation is skipped unless the paid-image opt-in is enabled. Shopify theme API writes remain disabled in this phase: the homepage JSON is backed up/exported and the run waits for manual Theme Editor confirmation instead of falsely reporting a completed theme write.

## Before Cabin Tidy production use

- Verify Shopify `read_products`, `write_products`, collection/publication, and `read_themes` scopes. Collection publication requires its existing publication scope.
- Configure an explicit store selling-price source. Amazon/source `price` is not used as retail price; missing store price is reported and skipped.
- Confirm the product variant strategy (the current safe price updater only manages exactly one Shopify variant), currency, product status, and inventory/location policy. Inventory remains unmanaged.
- Review source-image reuse rights and configure a media adapter/provider before selecting SOURCE_MEDIA/GENERATED_MEDIA/MIXED.
- Verify product/collection preview outputs and test a small, manually supervised catalog before expanding toward 2,000–10,000 products.
- Prepare Shopify theme write approval/exemption separately if ever required. This phase uses a local patch and manual Theme Editor gate; it never writes theme files or uses browser automation.
