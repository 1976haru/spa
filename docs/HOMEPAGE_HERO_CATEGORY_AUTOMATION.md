# Homepage hero and category automation

Phase 3.9 adds a persisted homepage plan that is composed from the selected
BrandProfile, Phase 3.2 collection plan, confirmed Shopify collection mappings,
approved Phase 3.3 collection imagery, and the published theme schema read by
the Phase 3.4 theme reader.

## Planning and assignment mode

The planner stores JSON metadata only; image bytes stay under
`exports/homepage_assets/<store>/<plan>/hero/`. Hero copy is category-oriented,
avoids unverifiable claims and requires a real remote collection mapping for
its CTA. Category shortcuts use enabled collection definitions, priority and
estimated counts. A missing Shopify mapping remains `SKIP_REMOTE`. Collection
art is reused only after the explicit approval field is set; otherwise the plan
records the simple-icon fallback and its warning.

Hero image files can be selected manually, inspected, explicitly approved, and
uploaded through the existing Shopify Files uploader. OpenAI image generation
uses the existing image provider and is available only after the paid opt-in.
Generated assets remain `NEEDS_REVIEW` until the user approves them. Reports
are written to `exports/homepage_reports/<store>/<plan>/` and contain hashes,
copy, mappings, actions, and warnings, never credentials.

## Theme safety

Theme section schemas—not theme names—are inspected for hero and collection
list/multicolumn capabilities. Only proven setting IDs are patched. Unknown or
review-required mappings fall back to manual Theme Editor instructions. A
preview is bound to plan, theme files, assets, and current homepage JSON. The
apply service requires a current published theme, read/write theme scopes,
approved Shopify-hosted hero asset, high-confidence mappings, a fresh preview,
and explicit confirmation. It backs up `before.json`, `proposed.json`, and
`diff.md`, changes only the homepage JSON template, and verifies by reading it
back. Rollback is explicit and refuses to overwrite remote drift.

Shopify documents `themeFilesUpsert` as requiring `write_themes` and Shopify
approval/exemption. ShopSource does not automate Theme Editor clicks; a missing
scope or exemption means `MANUAL_ACTION_REQUIRED`.

## Tests

All Shopify/theme interactions in `tests/test_homepage_automation.py` use fake
clients. The suite covers copy, real CTA mappings, image opt-in/validation,
approved collection image reuse, schema discovery, minimal diff, stale previews,
backup, verification, rollback, and Store Build stage order. No live Shopify,
Amazon, Spark, or paid image API operation is needed to test the planner.
