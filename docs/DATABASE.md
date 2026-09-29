# Database

## products
ASIN 당 1행. 최신 관측값과 raw JSON을 보존한다.

## product_occurrences
동일 ASIN이 Spark의 여러 실행/파일에서 반복 등장한 이력을 보존한다. 따라서 재수집 중복량과 job별 통계를 계산할 수 있다.

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
