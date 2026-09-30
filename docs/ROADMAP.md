# Roadmap

## v0.2.5 API-free browser capture (implementation status)

- [x] User-started Edge/Chrome MV3 capture extension and loopback pairing bridge
- [x] Search candidate/detail capture queue, completeness score, existing MASTER importer and Store classifier reuse
- [x] Source-aware Spark-compatible payload adapter (portal mapping still unverified)
- [ ] Live Amazon selector verification and manual five-product Spark Center portal round-trip

## v0.1 MASTER FOUNDATION — 현재
- [x] Spark storage ZIP/폴더 import
- [x] ASIN MASTER dedupe
- [x] occurrence/history 보존
- [x] SQLite
- [x] 동적 가격구간
- [x] Store Profile
- [x] 위험/검토 상태 보존
- [x] CSV/JSON export
- [x] 간단 GUI
- [x] 3개 Store Profile 예제
- [x] Spark 공식 사용법 기준 handoff 전략 문서화

## v0.1.1 STABILITY AUDIT — Phase 1B
- [x] import idempotency 회귀 테스트
- [x] 수동 override 보존/해제 검증
- [x] Windows 한글/공백 경로 및 ZIP/폴더 테스트
- [x] DB 인덱스/트랜잭션/streaming 분류 점검
- [x] Git에 실제 상품 DB/credential이 포함되지 않는지 점검
- [x] 읽기 전용 schema probe와 malformed import report

## v0.2 SPARK HANDOFF MVP
- [x] Spark '데이터 관리 > 데이터 불러오기'에 사용하는 실제 datasets 샘플 확보
- [x] datasets/request_queues/key_value_stores 역할 분석
- [x] datasets 작업폴더 선택 방식 확인
- [x] 5개 상품 subset 테스트 폴더 생성
- [x] Spark UI에서 5개 subset 재불러오기 PASS
- [~] 이미지/ASIN/제목/옵션/브랜드 표시 확인; 가격/세부필드 추가 검증 필요
- [x] Store Decision 기반 datasets handoff generator
- [x] Export manifest / validation report
- [x] 수동 override/status/limit/ASIN 선택 및 RESTRICTED 안전 게이트
- [ ] 미국 Shopify Location 선택 가능 확인
- [ ] 테스트 Shopify 스토어 업로드 확인
- [ ] 재업로드 시 중복 동작 기록

## v0.2.1 SPARK CENTER PACKAGE WORKFLOW — 현재
- [x] 운영자가 Spark Center 측에 외부 소싱 파일/폴더 업로드 가능 여부 확인
- [x] 기본 출력 위치를 프로젝트 로컬 `exports/spark_center/<store>/...`로 정리
- [x] 스토어/상태/상품수 기준 업로드 폴더 원클릭 생성
- [x] 업로드 폴더에는 상품 JSON만 포함
- [x] manifest/validation은 업로드 폴더 밖에 보관
- [x] GUI에 'Spark Center 업로드 폴더 만들기'와 '폴더 열기' 추가
- [x] package history / CREATED-UPLOADED 수동 상태 추적
- [x] 기존 Spark desktop handoff는 고급 호환 옵션으로 유지
- [ ] 실제 Spark Center 포털에 생성 폴더 업로드 round-trip 검증

## v0.2.2 AMAZON SOURCE INBOX
- [x] 프로젝트 로컬 `source/amazon/` 작업공간 자동 생성
- [x] 일반 상품 JSON 폴더를 MASTER DB로 가져오기
- [x] 기존 Spark storage/ZIP import는 레거시/고급 입력으로 유지
- [x] 원본 읽기 전용, ASIN dedupe, occurrence/history 유지
- [x] GUI 소싱 폴더 열기/가져오기/MASTER 0 안내/자동 재분류
- [x] source/amazon 실제 상품 데이터 Git 제외

## v0.2.3 AUTOMATED AMAZON CANDIDATE SOURCING — 현재
- [x] Provider abstraction 기반 자동 소싱 엔진
- [x] 1차 provider: Keepa API Product Finder / Product Request
- [x] Keepa API key를 Git 밖의 환경변수/GUI session에서 관리
- [x] Store Profile 기반 keyword/price/risk sourcing recipe
- [x] 후보 ASIN 검색 및 checkpoint/resume/cancel
- [x] Keepa token budget / retry / bounded backoff
- [x] Product Finder 결과 ASIN → 최대 100개 Product Request 보강
- [x] Keepa raw occurrence와 ShopSource canonical MASTER 분리
- [ ] Amazon Creators API는 Shopify 재판매용 sourcing feed로 사용하지 않음
- [x] 위험/성인/HazMat 후보 상태와 이유 보존
- [x] 동일 ASIN MASTER dedupe 및 Store별 classification 연결
- [x] Cabin Tidy 자동 소싱 recipe 1차 구현
- [x] GUI: 설정/미리보기/시작/일시정지/계속/취소/진행 요약
- [x] DRY RUN과 실제 API 실행 분리
- [ ] Keepa→Spark mapping 5상품 portal round-trip 검증
- [ ] 5개 → 50개 → 500개 단계별 live test
- [ ] 30,000개/Store 확장 benchmark 전에는 대규모 실행 금지


## v0.2.4 SOURCING INTELLIGENCE + OPERATOR UI V2 — 구현 완료, live 운영 검증 전
- [x] Store Profile 기반 deterministic 검색 주제어 추천 엔진
- [x] 기존 recipe + MASTER/Keepa 결과 제목에서 1~4 gram 후보 추출
- [x] KeyBERT / sentence-transformers 선택적, 로컬 전용 의미 기능과 lexical fallback
- [x] RapidFuzz 선택적 유사 keyword deduplication 및 표준 라이브러리 fallback
- [x] Keepa Product Finder 소규모 사용자 실행 검증과 후보/가격/품질/위험/MASTER 중복 통계
- [x] 추천 주제어별 설명 점수/근거와 recipe 추가/제외
- [x] Store 생성 wizard 기반 Store Profile 저장
- [x] Tkinter GUI와 CLI를 유지하고 NiceGUI UI V2 별도 추가
- [x] AG Grid Community 기반 7개 운영 화면
- [x] 상품 검색/DB 필터/정렬/page 및 다중 행 수동 override
- [x] 선택적 keyring credential 저장과 환경변수 fallback
- [x] SQLite OLTP 유지; DuckDB/Polars는 데이터 규모 근거 확보 후 재검토
- [ ] 실제 Keepa token을 이용한 1~3 keyword live 검증
- [ ] 001 Cabin Tidy 전체 흐름과 Spark Center portal package round-trip 검증


## v0.2.5 API-FREE BROWSER SOURCING + SPARK READY PIPELINE — 구현 완료, live 검증 진행 중
- [x] Keepa 없이도 동작하는 브라우저 기반 무료 소싱 모드
- [x] Edge/Chrome Manifest V3 ShopSource Capture 확장
- [x] localhost-only Capture Bridge + pairing token
- [x] Amazon 검색결과 페이지에서 ASIN/title/price/image/url/rating/reviewCount 후보 수집
- [x] 상품 상세 페이지에서 brand/category/bullets/overview/options/images 등 상세 보강
- [x] cookie/session/localStorage/auth header를 수집하지 않도록 제한
- [x] search capture → detail enrichment → completeness score → MASTER 흐름
- [x] source_kind=BROWSER_CAPTURE 및 원본 occurrence 보존
- [x] Spark-compatible payload builder + validation
- [x] 불완전 상품은 NEEDS_DETAIL로 보존하고 자동 삭제하지 않음
- [x] Keyword Studio 추천어 → Amazon 검색 열기 → 현재 결과 가져오기 연결
- [x] 5개 단위 사용자 시작형 상세 보강
- [x] CAPTCHA/robot check 감지 시 중단하고 우회하지 않음
- [x] 실제 Amazon 검색 결과 후보 capture 성공
- [x] 실제 Amazon 상품 상세 1개 DETAIL_COMPLETE 성공
- [ ] 실제 상세 5개 → MASTER → 분류 → Spark Center package 완료
- [ ] 실제 Spark Center 포털 5상품 round-trip 검증


## v0.2.6 BATCH SOURCING QUEUE — 다음
- [ ] 검색결과 현재 DOM 전체 상품 일괄 capture (viewport 5개 제한 제거)
- [ ] 후보 자동선택/중복제거/가격·위험·Store fit 선필터
- [ ] DETAIL_COMPLETE 불필요 후보와 NEEDS_DETAIL 후보 자동 분리
- [ ] 사용자 시작형 상세보강 batch queue
- [ ] 기본 동시 처리 1, 최대 3, batch 20개 안전 제한
- [ ] 상세 페이지 marker 기반 자동 capture로 상품별 클릭 제거
- [ ] CAPTCHA/Robot Check 감지 시 queue 즉시 PAUSED_NEEDS_USER
- [ ] pause/resume/cancel/checkpoint
- [ ] 상세 성공 시 다음 후보 자동 진행
- [ ] 준비도 기준 자동 MASTER 반영 옵션
- [ ] MASTER 반영 후 Store 자동 classify
- [ ] 목표 PRIMARY 수량/전체 후보 수량 진행률 표시
- [ ] 검색어별/페이지별/ASIN별 중복률·성공률 통계
- [ ] 실패 queue와 재시도
- [ ] 20 → 50 → 100 상품 단계별 운영성 테스트
- [ ] 실제 Spark Center 5상품 round-trip 성공 전 대규모 package 자동 생성 금지
- [ ] 완전 무인 페이지네이션/CAPTCHA 우회/stealth/proxy 기능은 구현하지 않음

## v0.2.6 implementation update

Batch candidate queue, sequential detail capture, checkpointed lifecycle, automatic MASTER import/classify option, and UI controls are implemented. Manual Amazon search-page pagination and five-product Spark Center portal verification remain operator tasks. Primary-count targeting, multi-keyword batch statistics, selectable 20/50/100 chunks, and concurrency above one remain future work.

## v0.3 STORE ENGINE
- [ ] UI에서 가격구간 직접 편집
- [ ] Store 추가/복제/비활성
- [ ] 다중 스토어 fit 분석
- [ ] 수동 승격/강등 UI
- [ ] 상품 상세/이미지 미리보기
- [ ] 대량 필터/검색/페이지네이션
- [ ] Store Profile별 Spark Export preset

## v0.4 SPARK CENTER CONNECTOR
- [ ] Spark Center가 100~200개 샵 운영에 필요한지 실제 계약 확인
- [ ] 공식 Import/API/hand-off 계약 확인
- [ ] dry-run
- [ ] export validation
- [ ] 실패/재시도/전송 이력
- [ ] 계정당 스토어 수 제한에 맞춘 운영 구조 검토

## v0.5 SMART SOURCING
- [ ] 허용되는 입력 방식에 기반한 후보 수집
- [ ] 가격 선필터
- [ ] checkpoint/resume
- [ ] CPU/RAM throttle
- [ ] 실패 큐

## v0.6 200 STORE MANAGER
- [ ] 200 store profile manager
- [ ] 상품풀 부족 경고
- [ ] 스토어 간 중복률
- [ ] 샵별 상품 배정/승인 큐
- [ ] 중앙 dashboard
