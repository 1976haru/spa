# MASTER SPEC — ShopSource Studio

## 목적

단일 샵용 크롤러가 아니라 100~200개 Shopify 자매몰을 위한 중앙 소싱·상품판정 플랫폼을 만든다.

## 핵심 데이터 철학

1. 원본 상품은 가능한 한 MASTER DB에 보존한다.
2. 가격 조건은 삭제 조건이 아니라 스토어별 상태 분류 조건이다.
3. 위험/금지 후보도 삭제 대신 이유와 상태를 보존한다.
4. 하나의 상품이 여러 스토어에서 서로 다른 판정을 받을 수 있다.
5. 수동 판정은 자동 재분류보다 우선한다.
6. Spark Center 연동은 필수 목표지만, 안정적인 입력 계약을 확인하기 전 내부 DB 조작이나 비공식 서버 호출을 구현하지 않는다.

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

200개까지 동일 프로그램에서 확장 가능한 구조를 유지한다.

## v0.1 범위

- Spark local storage import
- MASTER SQLite
- ASIN dedupe
- dynamic price bands
- keyword/risk rule engine
- store decision table
- CSV/JSON export
- simple Windows GUI

직접 Amazon 수집, CAPTCHA 우회, Spark Center 비공식 쓰기 연동은 v0.1 범위가 아니다.
