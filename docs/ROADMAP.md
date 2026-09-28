# Roadmap

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

## v0.1.1 STABILITY AUDIT — 다음
- [ ] 대량 import idempotency 검증
- [ ] 수동 override 보존 검증
- [ ] Windows 한글/공백 경로 검증
- [ ] DB 인덱스/트랜잭션/배치 성능 점검
- [ ] Git에 실제 상품 DB/credential이 포함되지 않는지 점검

## v0.2 SPARK HANDOFF MVP
- [ ] Spark '데이터 관리 > 데이터 불러오기'에 사용하는 실제 원본 샘플 확보
- [ ] 원본 파일/폴더 스키마 분석
- [ ] 원본 그대로 재불러오기 Round-trip PASS
- [ ] 원본 중 상품 5개 subset Export 생성
- [ ] Spark UI에서 subset 재불러오기 PASS
- [ ] ASIN/제목/가격/이미지/옵션 보존 확인
- [ ] 미국 Shopify Location 선택 가능 확인
- [ ] 테스트 Shopify 스토어 업로드 확인
- [ ] Export manifest / validation report
- [ ] 재업로드 시 중복 동작 기록

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
