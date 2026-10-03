# Shopify collection publisher (Phase 3.3)

Shopify integration is isolated in `shopsource.shopify_collections`. It targets
the stable Shopify Admin GraphQL API **2026-07** and uses the current
`collectionCreate(collection:)` / `collectionUpdate(collection:)` sources model.
It does not build on deprecated `ruleSet`. `collectionUpdate` changes only the
managed conditions source and collection fields; there is no delete operation.

## Credentials and stores

`shopify_connections` contains each store's `*.myshopify.com` domain, pinned
API version, status, checked scopes, publication metadata, and verification
time. `shopify_collection_mappings` stores the stable collection-key/handle to
Shopify GID mapping and last-synced content/image metadata. Neither table has a
token or API key column. Shopify access tokens are stored using the optional
`keyring` package in Windows Credential Manager. `SHOPIFY_ACCESS_TOKEN` is an
environment-only single-store fallback; it is never copied to SQLite. OpenAI
uses `OPENAI_API_KEY` from the process environment and never persists it.
Credential fields are password inputs and are cleared from the UI after save.

For each Store, enter its Shopify shop domain and an Admin API access token in
**컬렉션 자동화 → Shopify 연결 설정**, save, then select **Shopify 연결 확인 /
권한 확인**. The verifier displays missing scopes and Online Store publication
IDs. Required collection scopes are `read_products`, `write_products`, and
`read_publications`; publishing additionally requires `write_publications`,
and staged file upload requires `write_files`.

## Preview, conflict detection, and writes

`dry_run` reads collections and returns CREATE, UPDATE, NO CHANGE, CONFLICT, or
SKIP without a mutation. Existing unmapped handles are adopted only when their
current content exactly matches the plan; otherwise they are conflicts.
Previously mapped collections whose Shopify fields, conditions, ID, or image
drift from the last ShopSource sync are conflicts. Missing mapped collections
are also conflicts, never recreated automatically. Repeated syncs therefore
reuse stable mappings rather than adding duplicates.

The UI requires a preview followed by a separate confirmation dialog before
calling `sync(confirmed=True)`. The publisher rejects calls without that
confirmation. Sync never deletes collections. Each collection is isolated;
image, collection, and publication errors are recorded per item while later
items continue. Retry-failed-only reads the prior run and does not retry
successful items. Shopify product counts (and exact/estimated precision when
returned) are recorded after create/update for comparison with the local
estimate.

Phase 3.2 TITLE rules map to `productTitle`; existing Spark-uploaded products
can therefore use a title fallback. Future ShopSource tags map to `productTag`
with `TAGGED_WITH`; in TAG_PREFERRED mode tag and title fallback rules are
combined as ANY. Tag negation maps to `NOT_TAGGED_WITH`. Shopify `sources` use
ANY/ALL inclusion matching. Unsupported canonical rules are skipped instead of
being silently converted to a different rule.

## Collection images and publication

`CollectionImageProvider` defines the provider boundary. MANUAL registers an
operator-selected local image. OPENAI_IMAGES uses the official Images
generation endpoint with model `gpt-image-2.5-sunburst`, square `1024x1024`
output, and the Phase 3.2 prompt plus ecommerce/no-text/no-logo/no-watermark
constraints. It requires both an `OPENAI_API_KEY` and explicit UI opt-in. Bulk
generation estimates call count and isolates per-image errors. Generated files
are stored under `exports/collection_images/<store>/<collection_key>/vN.png`;
exports are git-ignored.

Images are staged to Shopify using `stagedUploadsCreate`, multipart upload,
`fileCreate`, and a final HTTPS URL check before they are attached to a
collection. Phase 3.2 alt text is passed to both Shopify Files and the
collection image input. An image error does not abort the collection batch.
Publication is not implicit: only the checked **온라인 스토어에 공개** option
allows `publishablePublish`, after the Online Store publication is found and
`write_publications` is verified.

Run reports are written to
`exports/collection_sync_reports/<store>/<run_id>/sync_report.json` and `.md`.
They include plans, conditions, Shopify IDs, image/publication outcomes,
errors, counts, and timing; never credentials. `exports/` and generated images
are ignored by Git.

## Safety boundary

Plan generation and dry-run do not write Shopify. Shopify writes happen only
after the operator presses the final UI confirmation. OpenAI calls happen only
after the explicit image-generation opt-in. Automated tests use a fake GraphQL
client and mocked image/file transports. No Cabin Tidy token, Shopify mutation,
paid image call, publication write, product upload, Amazon run, or Spark upload
is executed during implementation.

Official references:

- [Shopify API versioning](https://shopify.dev/docs/api/usage/versioning)
- [Shopify 2026-07 `collectionCreate`](https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/collectionCreate)
- [Shopify 2026-07 `collectionUpdate`](https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/collectionUpdate)
- [Shopify 2026-07 `publishablePublish`](https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/publishablePublish)
- [OpenAI image generation](https://developers.openai.com/api/docs/guides/image-generation)
