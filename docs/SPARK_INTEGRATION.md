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

사용자가 제공한 `storage.zip` 및 추가 샘플에서 아래 구조가 실제로 확인됐다.

```text
storage/
  datasets/<job_id>/*.json
  key_value_stores/<job_id>/...
  request_queues/<job_id>/*.json
```

실제 화면에서 `datasets`와 `request_queues`에 동일한 job id
(`0929_071617_c102`)가 존재하는 것이 확인됐다.

### datasets

상품 JSON에는 ASIN, URL, title, brand, price, options, tags/category, overview/aboutThis,
images, rating/reviewCount, `_sourceUrl`, `_listPage`, `_collectedAt` 등이 존재한다.

v0.1은 `datasets`를 **읽기 전용**으로 import하며 Spark 파일을 수정하지 않는다.
Malformed JSON은 해당 파일만 건너뛰고 `import_errors`에 기록한다. ZIP 입력은 경로 이탈 항목을 거부한다.

### request_queues

실제 request queue 샘플은 상품 payload가 아니라 Crawlee/Playwright의 요청 상태와 재시도/오류 메타데이터다.

확인된 주요 필드:

- id
- url
- uniqueKey
- method
- retryCount
- errorMessages
- userData.sourceUrl
- userData.listPage
- userData.label
- userData.__crawlee 상태
- handledAt

샘플에는 `page.goto: net::ERR_ABORTED; maybe frame was detached?` 오류가 포함돼 있으며,
Spark 내부 경로에 `@crawlee/playwright`가 나타난다.

따라서 현재 증거상 `request_queues`는 **크롤링 실행/실패/재시도 상태 관리용 메타데이터**로 보는 것이 타당하며,
Shopify 업로드용 상품 원본의 주 저장소는 `datasets`일 가능성이 높다.

단, 이것만으로 `request_queues`가 Spark의 '데이터 불러오기'에 불필요하다고 확정하지 않는다.
실제 round-trip 테스트 전까지는 계약 미확정 상태를 유지한다.

### key_value_stores

실제 `key_value_stores/<job_id>` 샘플에서 최소 두 종류의 런타임 상태 파일이 확인됐다.

1. `SDK_CRAWLER_STATISTICS_0.json`
   - requestsFinished / requestsFailed / requestsRetries
   - 요청 최소/최대/평균 시간
   - crawlerStartedAt / crawlerFinishedAt / runtime
   - HTTP status 통계
   - navigation timeout, browser new page 실패, ERR_ABORTED 등 오류/재시도 통계
2. `SDK_SESSION_POOL_STATE.json`
   - usable / retired session 수
   - 각 session id, usageCount, errorScore, expiresAt
   - cookie jar 및 Amazon 세션 쿠키/토큰

따라서 현재 증거상 `key_value_stores`는 **크롤러 통계와 브라우저/session-pool 런타임 상태 저장소**로 보는 것이 타당하다.
특히 session pool 파일에는 세션 쿠키와 토큰이 포함될 수 있으므로 민감 런타임 데이터로 취급한다.

ShopSource Studio 원칙:

- `key_value_stores`를 상품 MASTER 원본으로 사용하지 않는다.
- session cookie/token 값을 DB, 로그, export, manifest, Git에 복사하지 않는다.
- 진단이 필요하면 통계/필드명만 읽고 cookie value는 redaction 한다.
- Spark handoff export에 key_value/session 파일을 포함하지 않는 방향을 **우선 가설**로 둔다.
- 단, 실제 Spark '데이터 불러오기' round-trip이 통과하기 전까지 datasets-only 계약으로 확정하지 않는다.

### schema probe

`shopsource probe-schema <파일|폴더|ZIP>`은 원본을 수정하지 않고 파일/JSON/상품 수,
ASIN 중복, 키·필드 타입·누락률, Job ID, 가능한 버전 필드를 출력한다.

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

### B. 우선 운영 경로 — Spark Center 수동 폴더 업로드

2026-09-29 사용자 운영 확인 기준으로 Spark Center 측에서 **사용자가 직접 소싱한 파일/폴더를 업로드하는 방식이 허용됨**을 안내받았다.
따라서 100~200개 스토어 운영의 기본 handoff는 C드라이브 Spark runtime 폴더가 아니라 프로젝트 로컬의 관리 가능한 출력 폴더를 사용한다.

권장 기본 구조:

```text
<repo>/exports/spark_center/
  001_Cabin_Tidy/
    ready/<package_id>/
      000000001.json
      000000002.json
      ...
    manifests/<package_id>.manifest.json
    reports/<package_id>.validation.json
```

Spark Center에 실제 업로드하는 대상은 `ready/<package_id>` 폴더이며 그 안에는 상품 JSON만 둔다.
Manifest와 validation report는 ShopSource Studio 내부 추적용으로 폴더 밖에 둔다.

운영 기본값은 **프로젝트 로컬 출력**이다. `%APPDATA%\\spark\\storage\\datasets` 직접 출력은 Spark 데스크톱 호환 검증용 고급 옵션으로만 유지한다.

단, Spark Center 포털이 현재 생성된 datasets JSON 폴더를 실제로 그대로 수용하는지는 다음 수동 round-trip에서 최종 확인한다.
지원팀의 '외부 소싱 파일/폴더 업로드 가능' 안내와 '현재 JSON package가 포털에서 정상 수용됨'은 서로 다른 검증 단계로 기록한다.

Phase 2.1 구현 상태:

```text
exports/spark_center/<store_id>_<safe_store_name>/
  ready/<package_id>/000000001.json
  manifests/<package_id>.manifest.json
  reports/<package_id>.validation.json
```

- `ready/<package_id>` 하나만 사용자가 Spark Center에 업로드한다.
- `SparkCenterPackageService`는 기존 dataset writer/validation을 재사용한다.
- 생성 시 package history는 `CREATED`, validation 실패 시 `FAILED`로 기록한다.
- 사용자는 업로드 수행 후 `UPLOADED`로 수동 표시할 수 있다.
- `spark_center_manual_upload_allowed=true`는 지원 측 운영 허용 기록이다.
- `portal_package_verified=false`는 실제 포털 round-trip이 아직 미검증임을 뜻한다.
- 직접 API/네트워크 업로드는 구현하지 않는다.

### C. 차선 — Spark Center 공식 Import/API

100~200개 샵 운영에서 Spark Center가 필수라면 다음 중 하나가 공식적으로 확인된 뒤 구현한다.

1. Spark Center 공식 CSV/XLSX/JSON Import
2. 공식 API
3. 문서화된 handoff 폴더/파일 계약
4. 스파미팀이 허용한 외부 입력 방식

### D. 향후 — Shopify 직접 Connector

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
- request queue 오류 메타데이터는 MASTER 상품 삭제 근거가 아니라 수집 상태/오류 이력으로 취급한다.

## 5. 데이터 불러오기 UI에서 추가로 확인된 것

실제 Spark의 `데이터 관리 > 데이터 불러오기`를 실행했을 때 Windows **폴더 선택** 대화상자가 열렸고,
기본 위치가 `C:\\Users\\user\\AppData\\Roaming\\spark\\storage\\datasets`로 잡혀 있었다.
화면에는 각 job id 폴더가 나열되며 하단 버튼도 `폴더 선택`이다.

따라서 현재 증거상 Spark의 수동 데이터 불러오기 입력 단위는 개별 JSON 파일이 아니라
**`datasets/<job_id>` 폴더**일 가능성이 매우 높다.

다음 검증 단계:

1. 기존 정상 job 폴더를 그대로 선택해 재불러오기 PASS 확인
2. 해당 job에서 상품 5개 JSON만 복제한 신규 test job 폴더 생성
3. test job 폴더를 Spark에서 불러오기
4. 상품명/가격/이미지/옵션 보존 확인
5. Location 선택 및 테스트 업로드

실제 5개 상품 JSON만 넣은 테스트 폴더 `TEST_5ITEMS_20260929_192255`를 Spark에서 선택했을 때,
Spark 데이터 관리 화면에 **전체 5개 / 포함 5개 / 제외 0개**로 정상 로드되었다.
화면에서 상품 이미지, ASIN, 상품명, 옵션 라벨, 브랜드가 표시되는 것도 확인됐다.

이 테스트에서는 `request_queues`와 `key_value_stores`를 함께 제공하지 않았으므로,
**Spark의 수동 '데이터 불러오기'에는 datasets/<job_id> 폴더만으로 상품 목록 로드가 가능함이 확인됐다.**

따라서 datasets-only handoff의 '목록 불러오기' 단계는 PASS로 승격한다.
다만 Shopify 실제 업로드까지의 end-to-end 호환은 Location 선택 및 테스트 업로드가 끝난 뒤 최종 확정한다.

## 6. 아직 확인되지 않은 것

다음은 현재 문서/샘플만으로 확정할 수 없다.

- Shopify 실제 업로드 단계에서도 datasets-only handoff가 충분한지
- 파일명을 바꿔도 된다는 규칙이 어느 단위(작업파일/폴더)에 적용되는지
- 현재 ShopSource JSON package가 Spark Center 포털에서 그대로 수용되는지
- 100~200개 샵을 Spark/Spark Center에서 어떤 방식으로 계정 분할해야 하는지

따라서 Codex는 이 부분을 추측해서 구현하면 안 된다.
`SparkHandoffConnector`의 datasets 폴더 load capability는 `DATASET_LOAD_VERIFIED`다.
이는 5개 상품 폴더가 Spark UI에 로드된 범위만 의미하며 `shopify_upload_verified=false`를 유지한다.
Spark Center Connector는 별도 계약이므로 계속 `CONTRACT_PENDING`이다.

자동 handoff 구조:

```text
exports/spark_handoff/
  jobs/<job_id>/000000001.json
  manifests/<job_id>.manifest.json
  reports/<job_id>.validation.json
```

상품은 `final_status`와 manual override가 반영된 최종 판정으로 선택하며 ASIN 오름차순으로 쓴다.
최근 유효 occurrence raw JSON을 우선하고 없으면 product raw JSON으로 fallback한다.
job 폴더에는 상품 JSON 외의 manifest, runtime, request queue, key-value 파일을 만들지 않는다.

## 7. Round-trip 검증 게이트

Spark-compatible Export는 아래가 모두 PASS일 때만 '지원' 상태로 전환한다.

- 원본 Spark 작업 1개를 다시 불러오기 PASS
- 원본에서 상품 5개만 추린 Export 불러오기 PASS
- ASIN/제목/가격/이미지/옵션 보존 PASS
- Spark UI에서 Location 선택 가능 PASS
- 테스트 Shopify 스토어 업로드 PASS
- 재불러오기/재업로드 시 중복 동작 확인
- 실패 상품이 MASTER DB에서 사라지지 않음
- 실제 상품 DB/AccessToken/credential이 Git에 포함되지 않음

## 8. NO-GO

- Spark 내부 DB를 추측해서 직접 변경
- undocumented server endpoint 재현
- 인증 우회
- CAPTCHA/차단 우회
- 실제 Shopify AccessToken을 저장소에 커밋

핵심 데이터와 규칙 엔진은 Connector와 분리한다.
Spark 파일 계약이 확인되면 `SparkHandoffConnector`만 구현/교체하도록 유지한다.
