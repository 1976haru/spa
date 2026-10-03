# Brand, Logo, and Favicon Automation

The `/brand` page creates a versioned, store-specific visual identity and keeps
all binary assets outside SQLite. SQLite stores profile/asset metadata, hashes,
approval state, and optional Shopify file identifiers. Files and sanitized
reports are written under `exports/brand_assets/<store>/<version>/` and
`exports/brand_reports/`.

## Workflow

1. Seed BrandProfile from the existing Store Profile. Existing brand names are
   retained unless a user explicitly selects a candidate.
2. Generate ten editable name candidates and a reusable name prompt. Domain and
   trademark clearance is always a separate user task.
3. Generate a text-free logo mark, or select a local PNG/JPG/SVG. The exact
   brand wordmark is typeset locally into a horizontal PNG and SVG; inspect and
   approve each asset before use.
4. Derive a centered, padded favicon master and high-quality 32x32/64x64 PNGs
   from an approved mark. Near-white transparency is an explicit option.
5. Upload approved raster assets to Shopify Files, inspect the published
   theme's settings schema/data, then review a minimal settings-data diff.
6. Theme apply requires a current high-confidence preview, `read_themes`,
   `write_themes` (and any Shopify-required exemption), and a separate
   confirmation. It backs up the current settings before writing and reads
   back to verify. Rollback also requires confirmation and verifies restoration.
   If detection or permissions are incomplete, use the generated manual guide:
   Online Store → Themes → Customize → Theme settings → Logo/Favicon → Save.

## Image provider and secrets

MANUAL is supported without an external image call. OpenAI Images is optional;
install the project dependencies normally (Pillow is included) and configure
`OPENAI_API_KEY` via the environment or an OS credential store. The UI's
“이미지 자동 생성 사용” opt-in is off by default. Select the model through
`OPENAI_IMAGE_MODEL` or the UI. Credentials are never persisted in the profile,
database, or reports.

Generated assets begin in `NEEDS_REVIEW`. Approving a newer asset supersedes an
older approved asset of the same type; generation never overwrites the older
file. Assignment mode accepts manual logo/favicon files and records them as
reviewable BrandAssets.

Store Build inserts brand planning and approval stages before collection work,
and has separate brand-apply preview/apply stages. A missing scope, asset, or
high-confidence theme mapping remains a manual-action gate; it is not reported
as a successful apply.

Tests use synthetic raster fixtures and mocked Shopify GraphQL/file upload
transports. No live Shopify theme write or paid image generation is part of the
test suite.
