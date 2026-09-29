# Third-party library review

Optional runtime dependencies reviewed for Phase 2.4:

| Library | Purpose | License / use |
| --- | --- | --- |
| NiceGUI | Python operator UI | MIT; installed only with the `ui` extra |
| AG Grid Community (through NiceGUI) | Product/keyword grids | MIT; `modules="community"` only |
| RapidFuzz | Fuzzy keyword deduplication | MIT; optional `similarity` extra |
| KeyBERT | Optional phrase extraction | MIT; optional `nlp` extra |
| sentence-transformers | Optional local semantic embeddings | Apache-2.0; optional `nlp` extra; model weights have their own licenses |
| keyring | Windows Credential Manager access | MIT; optional `credentials` extra |
| `keepa` Python client by akaszynski | Adapter spike review | Apache-2.0; not added as a dependency |

AG Grid Enterprise is not enabled. KeyBERT/SentenceTransformer model files are not fetched automatically.
Confirm the selected model's license before using it commercially.
