# Automated Collection Planner (Phase 3.2)

Phase 3.2 creates local, versioned collection-design drafts. It does not create or
update Shopify collections, write product tags, call an image API, stage Spark,
upload Shopify products, or run Amazon sourcing.

The planner uses the latest Store Sourcing Plan (or a newly-created local plan
snapshot when none exists), its enabled categories and active keywords, Store
Profile identity, MASTER product titles/types/tags/vendors/prices, and the
read-only Store Decision status for preview breakdowns. Category keywords are
ranked by the Phase 3.0 score/history before distinctive title phrases are
proposed. Generic one-word rules such as `TITLE CONTAINS "Car"` are rejected.

Each revision persists a plan, collection definitions, and canonical conditions
in `store_collection_plans`, `store_collection_definitions`, and
`store_collection_conditions`. Re-running creates a new draft version and a
non-destructive diff. Collections default to title fallback rules. The hidden
advanced setting can propose ShopSource-owned tag conditions; it retains title
fallback conditions for current Spark-imported catalogs and estimates matches
locally without writing tags. Conditions are evaluated against current MASTER
records; overlap is allowed but high overlap, broad coverage, zero matches,
small categories, and unmatched products are reported. Empty collections are
omitted by default.

Collection descriptions, handles, category alignment, representative product
samples, status breakdowns, image prompts, and alt text are generated from
local profile/category/product data. Image generation itself is intentionally
deferred. JSON and Markdown exports are written only when the user explicitly
selects export, under `exports/collection_plans/<store>/<plan_id>/`.

The UI entry is **컬렉션 자동화**. **컬렉션 자동 설계** creates and previews a
local draft; **다시 설계** creates a new revision; **JSON/MD 내보내기** writes
the local artifacts. Shopify and image-generation buttons remain disabled for
the later phases.

The title-rule specificity estimate is the share of local matches supported by
at least one multi-word title phrase. It is a heuristic, not a semantic
relevance guarantee.
