# 무료 Source Safety 운영 경로

ShopSource의 G2 기본값은 `FREE_LOCAL_SOURCE_CHECK`입니다. 이 경로는 Spark에서 이미 가져온 자료를 로컬에서 확인하고, 필요한 경우 사용자가 기존 Browser Capture로 Amazon 상품 페이지를 직접 확인해 얻은 관찰값을 기록합니다. Keepa는 기존 호환을 위해 남아 있지만 선택형 유료 Provider이며 기본 검사를 막지 않습니다.

## 검사 대상

- DRAFT 파일럿: 실제 Shopify DRAFT 후보 선택기와 같은 PRIMARY 집합에서 최대 10개
- 검증 batch: 사용자가 업로드 대상으로 명시 선택한 PRIMARY 100~200개
- 나머지 승인 catalog: 승인된 PRIMARY 중 다음 최대 200개씩
- REVIEW, RESTRICTED는 자동 제외합니다. RESERVE는 명시적으로 승격하기 전까지 검사하지 않습니다.

MASTER 전체 수와 현재 upload batch 수는 별개로 보여줍니다. Spark에 상품이 들어왔거나 `quantity` 값이 있다는 사실은 원본 재고 근거가 아닙니다.

## Evidence 규칙

IN_STOCK 등 availability는 JSON-LD Offer availability 또는 명시적 재고 관찰만 인정합니다. availability가 없으면 UNKNOWN입니다. 가격이 없거나 0 이하이면 검토 대상입니다. 캡처가 freshness window를 넘으면 다시 확인해야 합니다. CAPTCHA, Robot Check, 로그인 요구는 차단/사람 확인 상태로 남기며 우회하지 않습니다.

이전 Spark capture의 가격은 해당 capture 시점의 값이지 현재 Amazon 가격이 아닙니다. 새 Browser Capture 결과가 없는 로컬 검사는 미리보기/큐 준비일 뿐 G2를 VERIFIED로 만들지 않습니다.

## Keepa

기존 Keepa Provider, Credential Manager profile, token-health 기능은 선택 유료 경로로 보존합니다. Keepa 미설정은 정상이며 무료 G2의 blocker가 아닙니다. Keepa 호출은 사용자가 해당 유료 Provider를 직접 선택하고 승인한 별도 작업에서만 합니다.

## 주의

이 기능은 소스 재고를 Shopify inventory와 자동 동기화하지 않습니다. 실제 source capture 이후 batch 범위와 freshness에 대해 검증하며, 배치 검증을 전체 카탈로그의 영구 안전 보증으로 해석하면 안 됩니다.
