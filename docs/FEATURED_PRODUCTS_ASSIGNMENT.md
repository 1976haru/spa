# Featured Products / New Arrivals assignment

Phase 4.3 adds a local-first, reusable homepage product-showcase plan. The plan references MASTER products and existing Shopify product mappings; it does not copy source blobs or perform remote writes.

## Safety boundary

- Eligible items require an eligible store decision, verified Shopify mapping, real handle, valid storefront retail price, image, and `ACTIVE` storefront status.
- `DRAFT` products are never activated automatically.
- Theme capability is accepted only from actual section schema fields.
- Unknown themes and PageFly remain `MANUAL_ACTION_REQUIRED`; no external GUI automation is attempted.
- Direct product-list schemas are preferred. Featured-collection fallback uses `shopsource:homepage:featured-products` and preserves merchant tags.
- Preview and remote verification are separate. A stale preview returns `CONFLICT`.

The UI stops at `FEATURED PRODUCTS ASSIGNMENT UI READY`. A real Shopify apply requires the operator to review the preview and explicitly confirm through the existing guarded homepage write flow.
