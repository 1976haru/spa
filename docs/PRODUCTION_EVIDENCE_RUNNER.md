# Production Evidence Runner

Phase 5.1 turns the production page into an evidence collector. The runner reuses an unfinished run for the selected store and persists each G0–G13 gate result. Progress is calculated from those fourteen gates only; software tests or mock evidence never count as store readiness.

## What runs automatically

- Local MASTER sourcing/content audit and its real counts
- A zero-cost source freshness preview
- Existing local collection plan and approved collection-image lookup
- Existing Shopify read-only theme discovery when the store connection and credential are configured
- Any explicitly configured read-only evidence adapters

The runner stops at `READY_FOR_PILOT` after G0–G13 are verified. It never invokes the G14 pilot write. Shopify, theme, inventory, shipping, tax, payment, Amazon, Keepa, Spark, and paid image writes/calls are outside this runner.

## What still needs a person

- Source/provider checks may consume API tokens. They remain at `WAITING_FOR_CONFIRMATION` until the user approves the complete run.
- Amazon/source images do not prove reuse rights. Product-media rights are never inferred or assigned automatically.
- Pricing/margin policy, legal/business facts, unknown Shopify fields, and final desktop/mobile visual review require explicit review or input.
- Shopify credentials and scopes are reported as present/missing without printing secrets.

## Progress and recovery

The percentage is the number of evidence-verified G0–G13 gates divided by fourteen. A blocker does not become ready just because a score is high. The same store's unfinished run is resumed; a separate run requires explicit confirmation. Evidence input fingerprints are stored per gate so a changed input can be audited on the next run. The pilot remains a separately confirmed, DRAFT-only action.

Runtime Shopify reads use the project's existing credential and GraphQL integration. Unit tests inject fake collectors and do not access the network.
# Phase 5.2 evidence completion

Phase 5.2 completes the production gate adapters and explicit human workflows:

- G2 runs an explicitly approved configured source adapter in checkpointed batches; provider errors remain source errors.
- G4 records rights only for selected, explicitly confirmed products, with reviewer timestamp and notes.
- G5 stores only an operator-confirmed pricing policy; automatic repricing stays off.
- G11 reads Shopify pages/policies where the configured API and granted scopes expose them. Content presence is not legal approval.
- G12 combines local/theme read-only checks with a human visual sign-off tied to a theme fingerprint.
- G13 keeps API evidence separate from manual commerce confirmations and does not change shipping, tax, payment, market, domain, or checkout settings.

See [PRODUCTION_HUMAN_GATES.md](PRODUCTION_HUMAN_GATES.md) for the beginner-facing decision checklist. `READY_FOR_PILOT` means only that pre-pilot evidence is complete; it does not run G14 or publish products.
