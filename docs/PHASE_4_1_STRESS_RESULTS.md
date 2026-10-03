# Phase 4.1 Stress Results

Environment observation: Windows, Python 3.14. Measurements are observations, not universal SLAs.

| Profile | Products | Collections | Result | Seed elapsed / peak | Completion elapsed / peak | Report elapsed / peak |
|---|---:|---:|---|---|---|---|
| SMALL | 100 | 5 | PASS | 0.290s / 32KB | 0.030s / 120KB | 0.006s / 218KB |
| MEDIUM | 2,000 | 8 | PASS | 0.268s / 576KB | 0.019s / 105KB | 0.005s / 218KB |
| LARGE | 10,000 | 15 | PASS | 0.189s / 2.74MB | 0.018s / 107KB | 0.005s / 217KB |
| MASSIVE | 50,000 | 30 | PASS | 0.689s / 15.01MB | 0.022s / 123KB | 0.006s / 218KB |

MASSIVE DB size was 17,043,456 bytes and `PRAGMA integrity_check` returned `ok`. The 50k×30 deterministic collection matcher completed in 6.794s with about 37KB traced peak working memory. Total massive harness elapsed was 7.639s.

Multi-store profiles passed: 10×2,000 (20,000 decisions), 50×1,000 (50,000), and 200×100 (20,000); cross-store leakage was zero. UI service returned at most 200 rows and rejected a 50,000-row page request.

The crash matrix recreated the service and resumed after injected exceptions at 15 major stages. Retry tests observed bounded exponential waits of 0.25/0.5/1.0 seconds through mocked callbacks, with no real sleep. Permanent user errors were attempted once. Network calls and remote writes were zero.

Raw machine output is intentionally not committed. The standard and massive harness summaries were written under `exports/stress_reports`.
