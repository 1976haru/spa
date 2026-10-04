# Production Release Gates

17개 gate는 순서가 있으며, 앞선 gate가 미확인인데 뒤 gate를 READY/VERIFIED로 올리면 서비스가 BLOCKED 처리합니다. 상태는 `NOT_STARTED`, `RUNNING`, `READY`, `READY_WITH_WARNINGS`, `REVIEW_REQUIRED`, `WAITING_FOR_INPUT`, `WAITING_FOR_CONFIRMATION`, `BLOCKED`, `VERIFIED`입니다.

1. 환경/스토어 identity
2. 소싱 품질
3. Source 안전성
4. 상품 콘텐츠
5. 상품 미디어 권리/품질
6. 가격/마진
7. 컬렉션 구조
8. 컬렉션/카테고리 이미지
9. 브랜드/헤더/내비게이션
10. 홈페이지
11. 상품/컬렉션 템플릿
12. 페이지/정책
13. SEO/접근성/모바일
14. 상거래 설정
15. 통제된 live pilot
16. 배치 확대
17. 최종 출시 점검

각 gate에는 redacted evidence와 blocker 이유를 보존합니다. 근거를 보내지 않으면 확인 필요로 남습니다. 상품 publish 후보는 fresh `IN_STOCK`, sellability PASS, 명시 판매가, 정책상 margin PASS, 허가된 exact-product media 및 확인된 콘텐츠가 모두 필요합니다. Source 장애는 품절로 추정하지 않고 미확인/차단으로 남깁니다.

업로드는 세 단계로 표시합니다. (1) 최대 10개 DRAFT Pilot, (2) source freshness·미디어 준비·API 안전 증거에 따라 100~200개 DRAFT 검증 batch, (3) 나머지 승인 카탈로그를 하나의 메인 stage로 진행합니다. 각 stage의 명시적 write 확인과 Shopify 재조회 검증이 완료되어야 checkpoint가 이동하며, 내부 chunking은 UI에 별도 단계로 과도하게 노출하지 않습니다. 불일치는 rollout을 영구 정지시킵니다. 이 gate 서비스는 Shopify write를 실행하지 않습니다.
