# Phase 4.1 Stress Test Plan

이 단계는 실제 서비스 호출 없이 Phase 3.1~4.0의 release candidate 품질을 검증한다.

| Component | Normal | Scale | Failure injection | Resume/idempotency | Integrity invariant |
|---|---|---|---|---|---|
| Core DB/migrations | existing suite | 50k rows, 200 stores | permission/lock-safe failure | repeated init | no reset/data loss |
| Product sync | payload/unit tests | 10k preview | timeout, 429, userErrors, partial batch | failed-only/checkpoint | ASIN identity, bounded preview |
| Collections | planner tests | 50k × 30 rules | overlap/zero/broad/unicode | deterministic repeat | no product loss |
| Brand/navigation/homepage/theme | Phase 3.x tests | 12-schema matrix | malformed/drift/missing mappings | stale preview/idempotency | unrelated settings preserved |
| StoreBuild | orchestration tests | 10k synthetic context | exception at 15 major stages | service recreation/resume | no false COMPLETE |
| StoreCompletion | 34 Phase 4.0 tests | all four profiles | stale safe apply | repeat plan/report | blocker overrides score |
| Multi-store | store-scoped queries | 10×2000, 50×1000, 200×100 | interleaved plans | repeated plan creation | leakage count zero |
| Links/reports/UI | link/report/UI tests | 500 links, 50k DB | bad paths/permission denied | deterministic files | JSON valid, UI ≤200 rows |
| Security/network | existing credential tests | synthetic token corpus | socket kill switch | N/A | secret/network/write count zero |

Profiles are SMALL 100/5, MEDIUM 2,000/8, LARGE 10,000/15 and MASSIVE 50,000/30. The committed harness is `scripts/stress_phase_4_1.py`; raw output remains under ignored `exports/stress_reports`.

Static audit classifies abstract-provider `NotImplementedError` as EXPECTED, bounded `while True` database pagination/retry loops as EXPECTED, and the two disabled legacy collection buttons as LOW because active Phase 3.x controls exist below them. No production-only Cabin Tidy branch or unbounded remote retry was found.
