# Shopify Navigation and Mega-Menu Automation

Phase 3.8 builds a versioned navigation plan from enabled Phase 3.2 collection
definitions and the collection publisher's `shopify_collection_mappings`.
Children link only to the mapped Shopify collection resource ID and its mapped
handle. A collection without a remote mapping is shown as `SKIP` and is never
repointed to another collection.

The service reads the pinned Shopify Admin GraphQL API version from the store
connection. It discovers the `main-menu` handle (or an unambiguous Main menu
title), preserves existing unrelated menu items, and edits only the Shop
branch. Preview actions include CREATE, UPDATE, MOVE, NO CHANGE, SKIP, and
CONFLICT. Previews are hash-bound to the current menu, source collection plan,
and Shopify collection mappings. Menu mutations require an explicit UI
confirmation and `write_online_store_navigation`; discovery requires
`read_online_store_navigation`.

The assignment helper warns when multiple child labels share one target or
when links/children are missing. Those warnings do not block assignment review.
Remote manual drift in the managed branch is surfaced as a conflict rather
than overwritten.

Mega-menu detection inspects the published theme's header section schema,
global settings schema, and current theme settings. Only a direct setting with
an unambiguous runtime location can produce a high-confidence patch. Dropdown-
only, block-only/ambiguous, missing schema, and permission cases remain manual
actions. A theme apply preview changes only the resolved setting in
`config/settings_data.json`, backs up that file before mutation, and verifies
by re-reading it. No browser GUI automation is used.

All run reports are under `exports/navigation_reports/<store>/<run>/`. External
GraphQL calls are injectable; tests use a synthetic Admin API fixture and make
no network requests. Codex implementation tests do not perform Shopify writes.
