# MASTER SPEC — ShopSource Studio

## 목적

단일 샵용 크롤러가 아니라 100~200개 Shopify 자매몰을 위한 중앙 소싱·상품판정 플랫폼을 만든다.

## 핵심 데이터 철학

1. 원본 상품은 가능한 한 MASTER DB에 보존한다.
2. 가격 조건은 삭제 조건이 아니라 스토어별 상태 분류 조건이다.
3. 위험/금지 후보도 삭제 대신 이유와 상태를 보존한다.
4. 하나의 상품이 여러 스토어에서 서로 다른 판정을 받을 수 있다.
5. 수동 판정은 자동 재분류보다 우선한다.
6. Spark/Spark Center 연동은 필수 목표다. 다만 비공식 서버 호출보다 먼저 **Spark 공식 '데이터 불러오기 → Location 선택 → 업로드' 흐름과 호환되는 handoff**를 검증한다.
7. 외부 연동 형식이 바뀌어도 MASTER DB·Rule Engine·Store Profile을 다시 만들지 않도록 Connector를 분리한다.

## 상태 모델

- PRIMARY
- RESERVE_A
- RESERVE_B
- RESERVE_C
- LOW_RESERVE
- HIGH_RESERVE
- REVIEW
- RESTRICTED
- ARCHIVED

`DUPLICATE`는 상품 삭제 상태가 아니라 occurrence 통계로 관리한다. ASIN 하나는 MASTER 상품 1개, 여러 실행에서 나온 기록은 occurrences로 보존한다.

## Store Profile

스토어별 설정 파일에는 아래가 들어간다.

- store_id / store_name / category
- 동적 price_bands
- include_keywords / exclude_keywords
- risk_rules
- minimum_fit_score
- Spark handoff/export 정책
- Shopify target/location metadata는 credential과 분리하여 관리

200개까지 동일 프로그램에서 확장 가능한 구조를 유지한다.

## Spark 연동 원칙

Spark 매뉴얼상 이전 소싱 파일을 `데이터 관리 > 데이터 불러오기`로 불러온 뒤,
Shopify Location을 선택하고 `업로드`할 수 있다.

따라서 개발 우선순위는 다음과 같다.

1. Spark 원본 소싱 파일/폴더 계약 확인
2. ShopSource Studio MASTER DB로 import
3. Store Profile로 PRIMARY/RESERVE/REVIEW 등 분류
4. 사용자가 선택한 subset을 Spark-compatible Export로 생성
5. Spark 공식 UI에서 다시 불러오기
6. Spark가 기존 AccessToken/Location 흐름으로 Shopify 업로드
7. 필요 시 Spark Center 공식 Import/API Connector 추가

정확한 Spark 불러오기 파일 계약을 확인하기 전에는 형식을 추측해서 '호환'이라고 표시하지 않는다.

## v0.1 범위

- Spark local storage import
- MASTER SQLite
- ASIN dedupe
- dynamic price bands
- keyword/risk rule engine
- store decision table
- CSV/JSON export
- simple Windows GUI
- Spark handoff용 Connector 인터페이스와 Round-trip 테스트 준비

직접 Amazon 수집, CAPTCHA 우회, Spark/Spark Center 비공식 쓰기 연동은 v0.1 범위가 아니다.

## v0.2 목표

- Spark '데이터 불러오기' 실제 샘플 계약 분석
- Spark-compatible Export MVP
- 선택 상품 subset export
- export manifest 및 검증 리포트
- Store Profile별 export preset
- 원본/선별/업로드 이력 추적

## 장기 확장

- 100~200 Store Profile
- 스토어별 서로 다른 가격 밴드
- 하나의 MASTER 상품을 여러 스토어 후보로 재사용
- Spark/Spark Center/Shopify Connector 교체 가능 구조
- 수십만~수백만 occurrence를 고려한 DB/인덱스/배치 처리
