# Architecture

```text
Spark storage.zip / storage/
            |
            v
  SparkStorageConnector
            |
            v
   MASTER SQLite DB
 products + occurrences
            |
            v
      Rule Engine
       /   |   \
      /    |    \
 Cabin   Garage  Bathroom ... Store 200
      \    |    /
       store_product_decisions
            |
       +----+------------------+
       |                       |
    CSV/JSON            Spark Center Connector
                         (contract pending)
```

자동 소싱 경로는 `Store Profile → Sourcing Recipe → SourcingProvider → canonical MASTER → classifier`이다.
현재 provider는 Keepa이며 `sourcing/providers` 뒤에 격리된다. Keepa provider raw는 occurrence에,
ShopSource canonical JSON은 MASTER에 저장한다. API key는 두 위치 모두에 저장하지 않는다.

Spark export는 source-aware하다. `SPARK_STORAGE`는 검증된 최신 Spark occurrence payload를 사용하고,
`KEEPA`는 canonical payload를 별도 adapter로 매핑한다. 후자는 실제 portal round-trip 전까지
`KEEPA_TO_SPARK_MAPPING_UNVERIFIED`이다.

## 모듈 경계

- `connectors/`: 외부/로컬 시스템 입출력. 핵심 DB/규칙 엔진과 분리한다.
- `core/`: 가격·위험·적합도 규칙. 특정 Spark 구현을 알면 안 된다.
- `db.py`: SQLite 스키마와 연결.
- `importer.py`: Connector 결과를 MASTER DB에 upsert.
- `classifier.py`: 스토어 프로필별 판정 저장.
- `exporter.py`: 외부 전달 형식 생성.
- `ui/`: 사용자 작업 화면. 비즈니스 규칙을 직접 구현하지 않는다.

Connector 하위 구조:

- `spark_storage`: 로컬 `datasets/` ZIP/폴더 읽기 전용 입력
- `spark_handoff`: 검증된 `datasets/<job_id>` 상품 폴더 생성 및 자동 validation (`DATASET_LOAD_VERIFIED`)
- `spark_center`: 공식 API/Import 계약 확인 전까지 `CONTRACT_PENDING`
- `spark_center_package`: 프로젝트 로컬 Spark Center 수동 업로드 package 생성·history 관리
- `manifest`: 향후 Connector가 공통으로 쓸 credential-free export 메타데이터

`schema_probe.py`는 JSON/폴더/ZIP을 수정하지 않고 키, 타입, 누락률, ASIN/Job 통계만 분석한다.

## 대용량 원칙

200개 스토어 × 수십만 상품을 대비해 raw 원본과 store decision을 분리한다. 이미지 바이너리는 v0.1에서 다운로드하지 않고 URL/원본 JSON만 저장한다.
분류는 전체 MASTER 행을 메모리에 적재하지 않고 SQLite cursor를 순회한다. Import는 하나의 트랜잭션과 savepoint로 처리하여 치명적 오류 때 부분 MASTER 변경을 남기지 않는다.

Spark handoff는 `store_product_decisions.final_status`로 상품을 선택하고 ASIN 오름차순으로 쓴다.
상품 job 폴더에는 9자리 JSON만 두며 manifest/report는 형제 디렉터리에 분리한다.

`SparkCenterPackageService`는 별도 JSON writer를 구현하지 않고 `SparkHandoffConnector`의
상품 선택, raw payload, 순차 파일명, validation을 재사용한다. API connector와 수동 package
workflow는 명확히 분리하며 portal 자동 호출은 하지 않는다.
