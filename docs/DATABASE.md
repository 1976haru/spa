# Database

## products
ASIN 당 1행. 최신 관측값과 raw JSON을 보존한다.

## product_occurrences
동일 ASIN이 Spark의 여러 실행/파일에서 반복 등장한 이력을 보존한다. 따라서 재수집 중복량과 job별 통계를 계산할 수 있다.

## import_runs
storage ZIP/폴더 단위 import 상태와 통계.

## stores
Store Profile JSON 스냅샷.

## store_product_decisions
`(store_id, product_id)` 당 1행. 가격 상태, 위험 상태, 자동 상태, 최종 상태, 수동 override, 메모를 저장한다.

### 수동 Override
`manual_override=1`이면 Store Profile을 수정해 다시 classify해도 final_status를 덮어쓰지 않는다.
