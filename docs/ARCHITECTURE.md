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
- `spark_handoff`: 공식 Data Import 계약 확인 전까지 `CONTRACT_PENDING`
- `spark_center`: 공식 API/Import 계약 확인 전까지 `CONTRACT_PENDING`
- `manifest`: 향후 Connector가 공통으로 쓸 credential-free export 메타데이터

`schema_probe.py`는 JSON/폴더/ZIP을 수정하지 않고 키, 타입, 누락률, ASIN/Job 통계만 분석한다.

## 대용량 원칙

200개 스토어 × 수십만 상품을 대비해 raw 원본과 store decision을 분리한다. 이미지 바이너리는 v0.1에서 다운로드하지 않고 URL/원본 JSON만 저장한다.
분류는 전체 MASTER 행을 메모리에 적재하지 않고 SQLite cursor를 순회한다. Import는 하나의 트랜잭션과 savepoint로 처리하여 치명적 오류 때 부분 MASTER 변경을 남기지 않는다.
