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

## 대용량 원칙

200개 스토어 × 수십만 상품을 대비해 raw 원본과 store decision을 분리한다. 이미지 바이너리는 v0.1에서 다운로드하지 않고 URL/원본 JSON만 저장한다.
