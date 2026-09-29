# Spark handoff fixtures

This directory is reserved for validating Spark's official **Data Import** contract.

- Never commit real user product data, credentials, access tokens, bulk exports, or a complete Spark storage archive.
- Do not infer a Spark handoff schema before an original sample from the official Spark UI workflow is available.
- Round-trip tests must be skipped when no approved real sample is available.
- Synthetic fixtures must be explicitly labelled as synthetic and kept separate from approved, sanitized Spark contract fixtures.
- Real samples should be inspected read-only outside Git; only a deliberately sanitized minimal fixture may be added after review.
