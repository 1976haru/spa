# Production Source Safety Provider Bootstrap

## Why G2 has a separate provider

G1 reviews whether catalog candidates fit a store. G2 checks whether the original source listing is currently available and whether its price evidence is fresh. Keepa is only the G2 observation provider here; this does not replace Spark or another sourcing workflow.

## Configure once, reuse across stores

On the Production Evidence screen, open G2 and enter a Keepa API key under **Provider settings**. ShopSource stores the key in Windows Credential Manager, not in SQLite or reports. The profile metadata (provider type, display name, health time, and token telemetry) is stored in SQLite without the key. Other stores can bind to the same shared profile and do not need the key entered again. The legacy `KEEPA_API_KEY` environment variable and existing Keepa Credential Manager entry remain supported as compatibility fallbacks.

## Required sequence

1. Save or select a shared Keepa provider profile.
2. Run **Health check**. This is an explicit provider request. It records `tokensLeft`, `refillIn`, and `refillRate`; monetary cost stays `UNKNOWN` unless the provider supplies a verifiable plan quote.
3. Run **100-item Provider Pilot**. This is a separate, explicit source-provider test (not the 10-product Shopify DRAFT pilot). It performs at most one batch of 100 and validates normalized responses, unknown/error rates, and token use. It does not create source snapshots and can never verify G2 by itself.
4. Only after a clean pilot and sufficient available tokens is **Full Source Audit** enabled. It processes the full due catalog in durable 100-item checkpoints and supports pause, resume, stop, and failed-item retry.

Provider errors are never converted to out-of-stock. If the pilot has UNKNOWN or provider errors, it remains `REVIEW_REQUIRED`; resolve the provider/data issue and repeat the pilot. If the available token budget is insufficient, the full run stays blocked. No monetary cost is guessed.

## Safety and current state

The app never calls Keepa merely by opening the G2 page or generating a preview. A failed/missing provider preflight cannot set the `approved_at` checkpoint. Actual provider calls require explicit UI action. G2 is not marked `VERIFIED` by implementation, health success, or pilot success; it requires a completed full audit with no unresolved provider errors. Shopify, Theme, Spark, and Amazon writes are not part of this flow.
