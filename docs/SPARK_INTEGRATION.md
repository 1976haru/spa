# Spark / Spark Center Integration

## 현재 확인된 것

사용자가 제공한 `storage.zip`은 아래 형태다.

```text
storage/
  datasets/<job_id>/*.json
  key_value_stores/<job_id>/SDK_*.json
  request_queues/<job_id>/*.json
```

상품 JSON에는 ASIN, URL, title, brand, price, options, tags/category, overview/aboutThis, images, rating/reviewCount, `_sourceUrl`, `_listPage`, `_collectedAt` 등이 존재한다.

v0.1은 `datasets`를 **읽기 전용**으로 import하며 Spark 파일을 수정하지 않는다.

## Spark Center 연동 GO 조건

다음 중 하나가 확인되면 `SparkCenterConnector`를 구현한다.

1. Spark Center 공식 CSV/XLSX/JSON Import
2. 공식 API
3. 문서화되거나 안정적인 로컬 handoff 폴더/파일 계약
4. 스파미팀이 허용한 외부 입력 방식

## NO-GO

- 앱 내부 DB를 추측해서 직접 변경
- 업데이트에 쉽게 깨지는 undocumented server endpoint 재현
- 인증 우회

프로그램의 핵심 데이터와 규칙 엔진은 Connector와 분리되어 있으므로 Spark Center 계약이 확인돼도 전체를 다시 만들 필요가 없다.
