# Validation against the provided storage.zip

v0.1 importer was tested against the user's supplied Spark `storage.zip`.

Observed import result:

- JSON occurrences read: 2,375
- Unique ASIN products: 448
- Repeated occurrences: 1,927
- Spark job folders: 7
- Invalid product JSON: 0
- Price range: $4.99 ~ $69.99

Cabin Tidy default profile classification at validation time:

- LOW_RESERVE: 381
- RESERVE_C: 2
- RESERVE_B: 31
- PRIMARY: 30
- REVIEW: 4
- Total: 448

This confirms the intended behavior: products below the current PRIMARY price band are retained as reserve candidates instead of being deleted.

Note: these counts reflect the current v0.1 profile rules and can change when Store Profile price/risk/fit settings are edited and reclassified. No re-sourcing is required for that change.
