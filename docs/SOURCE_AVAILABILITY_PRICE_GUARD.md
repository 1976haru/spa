# Source 재고·가격 안전 가드

ShopSource는 이제 “스토어에 어울리는 상품”과 “지금 판매해도 안전한 상품”을 별도로 판단합니다. PRIMARY 상품도 source 재고가 없거나, 확인 시각이 오래됐거나, 가격 정책상 차단되면 live-safe 전달에서 제외됩니다. 상품은 MASTER에 그대로 남으므로 일시 품절 때문에 삭제되지 않습니다.

판매 가능 판정에는 최신 detail/provider snapshot, `IN_STOCK`, `FRESH`, 제한/보관 아님, 그리고 store별 가격 정책이 필요합니다. 과거 상품은 재고를 추측하지 않고 `NEVER_VERIFIED`로 시작합니다. 기본 pre-list 유효시간은 60분이지만 Store 설정에서 변경할 수 있는 운영 기본값입니다.

Source 가격과 Shopify 판매가는 서로 다른 값입니다. Source 가격 변화는 이력과 margin 경고를 만들지만 Shopify 가격을 자동으로 올리거나 내리지 않습니다. AUTO_REPRICE 기본값은 OFF입니다. 실제 최소 margin을 입력하지 않았다면 안전하다고 꾸미지 않고 `NEEDS_PRICING_POLICY`로 표시합니다.

Phase 4.2 전에 `소스 재고·가격 안전` 화면에서 `Source 안전 검사`를 눌러 대상·batch·예상 token을 확인한 뒤, 사용자가 명시적으로 실제 audit을 시작해야 합니다. 구현 자체는 Keepa/Amazon/Shopify 네트워크 실행을 시작하지 않습니다.

