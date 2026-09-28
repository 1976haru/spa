# Spark / Spark Center Integration

## 1. 공식 사용법에서 확인된 Spark 흐름

사용자가 제공한 스파크 사용법 문서 기준으로 다음 흐름이 확인됐다.

1. `작업 설정 > 수집 링크 추가`
   - 검색 결과: 대량 상품 소싱
   - 상품 상세: 단일 상품 소싱
2. 소싱 결과는 `모니터링`에서 완료 상품만 확인한다.
3. `데이터 관리 > 최근 작업 데이터 불러오기`로 가장 최근 소싱 데이터를 불러올 수 있다.
4. `데이터 관리 > 데이터 불러오기`로 이전에 소싱한 파일 중 원하는 파일을 선택해 다시 불러올 수 있다.
5. 업로드 전에 Shopify 위치(Location)를 선택한다.
6. `데이터 관리 > 업로드`를 누르면 불러온 상품을 Shopify 스토어로 업로드한다.
7. Spark 설정에는 스토어 이름과 Shopify AccessToken, 상품 마진 설정이 존재한다.

따라서 ShopSource Studio의 1차 연동 목표는 무조건 Spark Center 서버 API를 추측하는 것이 아니라,
**Spark가 공식 UI에서 다시 불러올 수 있는 데이터 handoff 형식**을 확인하고 호환 Export를 만드는 것이다.

## 2. 현재 로컬 데이터에서 확인된 것

사용자가 제공한 `storage.zip`은 아래 형태다.

```text
storage/
  datasets/<job_id>/*.json
  key_value_stores/<job_id>/SDK_*.json
  request_queues/<job_id>/*.json
```

상품 JSON에는 ASIN, URL, title, brand, price, options, tags/category, overview/aboutThis,
images, rating/reviewCount, `_sourceUrl`, `_listPage`, `_collectedAt` 등이 존재한다.

v0.1은 `datasets`를 **읽기 전용**으로 import하며 Spark 파일을 수정하지 않는다.

## 3. 연동 우선순위

### A. 최우선 — Spark 공식 '데이터 불러오기' 호환 Export

다음을 실제 Round-trip 테스트로 확인한다.

```text
Spark 정상 소싱
→ 데이터 관리에서 저장/불러오기 가능한 원본 파일 확보
→ ShopSource Studio에서 동일 스키마 분석
→ 선별 상품만 새 Export 생성
→ Spark 데이터 불러오기
→ 목록 정상 표시
→ 미국 Location 선택
→ Shopify 테스트 스토어 업로드
```

이 방식이 성공하면 ShopSource Studio는 상품 선별·분류를 담당하고,
실제 Shopify 업로드는 Spark의 기존 공식 UI/AccessToken 흐름을 그대로 이용한다.

### B. 차선 — Spark Center 공식 Import/API

100~200개 샵 운영에서 Spark Center가 필수라면 다음 중 하나가 공식적으로 확인된 뒤 구현한다.

1. Spark Center 공식 CSV/XLSX/JSON Import
2. 공식 API
3. 문서화된 handoff 폴더/파일 계약
4. 스파미팀이 허용한 외부 입력 방식

### C. 향후 — Shopify 직접 Connector

Spark/Spark Center 의존성을 낮출 필요가 생기면 별도 Connector로 검토한다.
단, v0.1~v0.2에서 Spark의 동작을 복제하려고 하지 않는다.

## 4. 중요한 운영 규칙

- 가격 미달/초과 상품을 삭제하지 않는다.
- 모든 유효 원본 상품은 MASTER DB에 보존한다.
- Store Profile이 PRIMARY / RESERVE / REVIEW / RESTRICTED 등을 결정한다.
- Spark로 내보낼 때만 사용자가 선택한 상태/가격대/스토어 기준으로 Export subset을 만든다.
- 예: Cabin Tidy에서 현재 PRIMARY가 $40~100이어도 $35 상품은 RESERVE로 보존하며, 나중에 $30~120으로 변경하면 재수집 없이 승격될 수 있어야 한다.
- 업로드용 Location은 Spark 매뉴얼에 따라 미국 주소지로 된 Shopify Location을 사용한다.
- Spark가 자동 제외하는 약물성·규정 위반·재고 없음 등의 실패 결과와 ShopSource Studio의 위험 분류는 별개로 기록한다.

## 5. 아직 확인되지 않은 것

다음은 현재 문서만으로 확정할 수 없다.

- '데이터 불러오기'가 정확히 어떤 파일 확장자/폴더 구조를 요구하는지
- datasets JSON만 있으면 되는지, key_value_stores/request_queues도 필요한지
- 파일명을 바꿔도 된다는 규칙이 어느 단위(작업파일/폴더)에 적용되는지
- Spark Center가 Spark 로컬 파일을 직접 수용하는지
- 100~200개 샵을 Spark/Spark Center에서 어떤 방식으로 계정 분할해야 하는지

따라서 Codex는 이 부분을 추측해서 구현하면 안 된다.

## 6. Round-trip 검증 게이트

Spark-compatible Export는 아래가 모두 PASS일 때만 '지원' 상태로 전환한다.

- 원본 Spark 작업 1개를 다시 불러오기 PASS
- 원본에서 상품 5개만 추린 Export 불러오기 PASS
- ASIN/제목/가격/이미지/옵션 보존 PASS
- Spark UI에서 Location 선택 가능 PASS
- 테스트 Shopify 스토어 업로드 PASS
- 재불러오기/재업로드 시 중복 동작 확인
- 실패 상품이 MASTER DB에서 사라지지 않음
- 실제 상품 DB/AccessToken/credential이 Git에 포함되지 않음

## 7. NO-GO

- Spark 내부 DB를 추측해서 직접 변경
- undocumented server endpoint 재현
- 인증 우회
- CAPTCHA/차단 우회
- 실제 Shopify AccessToken을 저장소에 커밋

핵심 데이터와 규칙 엔진은 Connector와 분리한다.
Spark 파일 계약이 확인되면 `SparkHandoffConnector`만 구현/교체하도록 유지한다.
