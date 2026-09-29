# Automated Candidate Sourcing

## 범위

ShopSource Studio는 Amazon 웹페이지를 scraping하지 않는다. CAPTCHA/WAF 우회, stealth browser,
proxy rotation, Amazon cookie/session 재사용도 하지 않는다. Amazon Creators/PA API 콘텐츠는
Amazon 외 판매 sourcing feed 용도로 사용하지 않는다. 첫 provider는 제3자 서비스 Keepa이며,
상업적 사용 조건은 운영자가 Keepa 약관과 plan에서 별도로 확인해야 한다.

## 구성

`SourcingEngine`은 provider-neutral 실행, dedupe, checkpoint, MASTER upsert와 Store classify를 담당한다.
`KeepaProvider`는 HTTPS Product Finder `/query`와 Product Request `/product`만 담당한다. Finder keyword는
각각 별도 recipe이며 hydration은 최대 100 ASIN 배치다. 대량 1차 요청에 offers/rating 옵션을 강제하지 않는다.

Cabin Tidy 기본 recipe는 차량 정리 keyword 10개, `$30~120`, rating 4.0+, review 30+, image 2+,
adult/hazmat 제외 조건을 사용한다. 조건이 0건을 반환해도 자동 완화하지 않는다.

## Credential과 비용

API key는 `KEEPA_API_KEY` 환경변수 또는 GUI 현재 session 입력으로만 받는다. CLI 인자, DB, profile,
manifest, 로그, Git에는 저장하지 않는다. plaintext fallback은 없다.

응답의 `tokensLeft`, `refillIn`, `refillRate`, `tokensConsumed`, `processingTimeInMs`를 해석한다.
`max_tokens_per_run` 또는 `min_tokens_reserve` 경계에 닿으면 `PAUSED_TOKEN_BUDGET`으로 멈춘다.
429/5xx/timeout은 제한된 exponential backoff만 수행한다.

## 실행

```powershell
shopsource source-auto --store 001 --target 5 --dry-run
$env:KEEPA_API_KEY = "<Keepa에서 발급한 값>"
shopsource source-auto --store 001 --target 5
```

DRY RUN은 network를 호출하지 않는다. 실제 검증은 5 → 50 → 500 순서로 확대하며 처음부터
30,000개를 실행하지 않는다. `source-status`, `source-resume`, `source-cancel`로 실행을 관리한다.

## 원본과 Spark mapping

Keepa Product Object 원본은 occurrence에, canonical 상품은 MASTER에 저장한다. 가격은 Buy Box Shipping,
Amazon, New 순으로 유효값을 선택하고 없으면 `price=None`으로 보존되어 분류 시 REVIEW가 된다.
성인/HazMat 후보도 삭제하지 않고 RESTRICTED/REVIEW 및 이유로 보존한다.

Spark 원본은 기존 raw payload를 사용하지만 Keepa 상품은 `to_spark_product_payload()` adapter를 거친다.
이 mapping은 `KEEPA_TO_SPARK_MAPPING_UNVERIFIED`이며 Spark Center 실제 5상품 round-trip 전에는 호환 완료로
표시하지 않는다. 이미지 binary를 다운로드하지 않으며 이미지 사용권과 재사용 조건도 별도 확인이 필요하다.
