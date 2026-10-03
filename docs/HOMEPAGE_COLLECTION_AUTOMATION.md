# Homepage collection automation (Phase 3.4)

The homepage planner uses Shopify Admin GraphQL API `2026-07` and is intentionally read-only. It queries the app scopes, finds the published theme (`role: MAIN`), reads `templates/index.json` and section files, and parses each Liquid `{% schema %}` block instead of assuming a theme vendor's section names. The app needs `read_themes` to inspect theme metadata and file bodies.

It ranks enabled Phase 3.2 collection definitions by planner priority, estimated product count, warnings, and image readiness, then recommends up to four (bounded to three through five when available). It maps each selected collection to a Shopify handle and builds a minimal JSON proposal with deterministic ShopSource section IDs. Existing sections outside the managed IDs retain their values and order. It selects a schema-supported Square image ratio and leaves the theme default untouched with a warning when Square is not offered. Product-card count is not overridden.

The dry run returns the current homepage JSON, proposed JSON, hashes, and structured `CREATE SECTION`, `UPDATE SECTION`, `MOVE`, `NO CHANGE`, or `CONFLICT` actions. On subsequent runs, ShopSource-managed sections are compared with the last verified managed state; settings/type edits and ordering drift become conflicts. Conflicts are never automatically overwritten. Deterministic IDs and existing section inspection prevent duplicate recommendations.

The UI's “홈페이지 추천 컬렉션 적용 (SAFE/DRY-RUN)” action does not call a Shopify mutation. It writes only a local manual-patch/backup package under `exports/theme_backups/<store>/<timestamp>/` containing `before.json`, `proposed.json`, and `diff.md`. The rollback control displays a manual rollback plan from `before.json`; it does not call Shopify. The verified-state recorder is local bookkeeping for a future separately authorized/mock-tested publisher and is not connected to the UI button.

If the store lacks `read_themes`, no published theme is found, the homepage template is unavailable, or no compatible section schema exists, the planner reports `MANUAL PATCH MODE`. It never uses browser click automation. There is deliberately no production theme write adapter in this phase. Shopify documents `themeFilesUpsert` as requiring `write_themes` and an exemption from Shopify; having the scope alone is insufficient. A future publisher must separately validate both permission and explicit user confirmation, back up, write only the targeted template, then refetch/verify before offering rollback.

Official Shopify references (pinned version):

- [OnlineStoreTheme and theme file connection](https://shopify.dev/docs/api/admin-graphql/2026-07/objects/OnlineStoreTheme) — `read_themes`, theme role, file filtering and pagination.
- [OnlineStoreThemeFileBodyText](https://shopify.dev/docs/api/admin-graphql/2026-07/objects/OnlineStoreThemeFileBodyText) — text file body content and `read_themes` requirement.
- [themeFilesUpsert](https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/themeFilesUpsert) — theme mutation requires `write_themes` and a Shopify exemption.
