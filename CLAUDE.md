# Claude Code Review Rules

Review against `docs/MASTER_SPEC.md` and `AGENTS.md`.

Focus on:
- accidental deletion of reserve candidates
- hard-coded Cabin Tidy logic that should live in Store Profiles
- DB integrity and idempotent imports
- manual override preservation
- Windows path handling
- memory/CPU behavior for large imports
- connector isolation
- unsupported assumptions about Spark Center

Default to review-only unless explicitly asked to implement.
