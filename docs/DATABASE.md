# Database

## products
ASIN 당 1행. 최신 관측값과 raw JSON을 보존한다.

## product_occurrences
동일 ASIN이 Spark의 여러 실행/파일에서 반복 등장한 이력과 occurrence별 raw JSON을 보존한다. 따라서 재수집 중복량과 job별 통계를 계산하고 과거 관측 payload를 감사할 수 있다.
기존 DB에는 nullable `raw_json` 열을 additive migration으로 추가하며, 이후 재import에서 비어 있는 과거 occurrence payload를 채울 수 있다.

## import_runs
storage ZIP/폴더 단위 import 상태와 통계.

## import_errors
Import run별 malformed JSON 및 필수 필드 누락의 파일명, 오류 코드, 메시지를 보존한다. 한 파일 오류는 전체 import를 중단하지 않는다.

## stores
Store Profile JSON 스냅샷.

## store_product_decisions
`(store_id, product_id)` 당 1행. 가격 상태, 위험 상태, 자동 상태, 최종 상태, 수동 override, 메모를 저장한다.

### 수동 Override
`manual_override=1`이면 Store Profile을 수정해 다시 classify해도 final_status를 덮어쓰지 않는다.
override를 명시적으로 해제한 뒤 재분류하면 최신 자동 판정으로 돌아간다.

## 인덱스와 occurrence 멱등성

- `products.asin` UNIQUE가 MASTER identity를 보장한다.
- `product_occurrences(product_id, job_id, source_file)` UNIQUE가 동일 Spark occurrence 재입력을 막는다.
- store/status, decision product, occurrence product/job/import run, import error run에 인덱스를 둔다.
- 기존 DB에는 `CREATE ... IF NOT EXISTS` 방식으로 테이블/인덱스를 추가하여 호환성을 유지한다.

## export_runs

Spark handoff의 job id, store, 선택 status, 출력 경로, 상품 수, ASIN hash, validation 결과를
run 단위로 기록한다. 이번 단계에서는 수백만 행으로 커질 수 있는 상품별 export membership는
추가하지 않고 manifest를 상세 감사 기록으로 사용한다.

Phase 2.1 additive migration 필드:

- `package_id`, `target`
- 생성 당시 `store_name` snapshot
- `requested_limit`
- `package_status`: CREATED / UPLOADED / FAILED / ARCHIVED
- `uploaded_at`, `note`
- `portal_package_verified`

기존 행은 `target=SPARK_DESKTOP`, `package_id=job_id`로 보존한다. `UPLOADED`는 사용자 수동
표시이며 포털 또는 Shopify 성공 검증을 의미하지 않는다.

## sourcing_runs / sourcing_run_candidates

`sourcing_runs`는 provider, Store, recipe snapshot, target, token telemetry, 진행 통계와
`checkpoint_json`을 보존한다. 상태는 `PENDING / RUNNING / PAUSED / PAUSED_TOKEN_BUDGET /
DONE / FAILED / CANCELLED`이다. `sourcing_run_candidates`는 ASIN별 recipe/page/rank/query hash와
위험 거부 이유를 보존한다.

`products.source_kind`와 `product_occurrences.source_kind`는 additive migration으로 추가된다.
Keepa의 provider raw는 occurrence에, canonical payload는 MASTER `raw_json`에 저장하여 Spark 원본과
혼동하지 않는다.
