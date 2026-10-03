# Launch Readiness

`READY`는 점수가 높은 상태가 아니라 필수 blocker가 하나도 없고 모든 수동 확인이 끝난 상태입니다. 98점이어도 배송 경로, 사용 가능한 결제 수단, cart/checkout 경로 또는 기본 검색이 확인되지 않으면 `NOT_READY`입니다.

점수는 진행 상황을 설명하기 위한 0~100 지표입니다. 핵심 commerce, storefront 구조, 콘텐츠·정책, 품질, commerce 설정, 최종 검증을 결정적으로 계산합니다.

보고서는 `exports/store_completion_reports/<store>/<run_id>/`에 생성되며 readiness 요약, blocker, 수동 작업, warning을 포함합니다. 보고서는 runtime 산출물이므로 Git에 커밋하지 않습니다.

자동 접근성 검사는 기본 휴리스틱일 뿐 WCAG 적합성 인증이 아닙니다. 정책 검사도 법률 적합성을 보증하지 않습니다.
