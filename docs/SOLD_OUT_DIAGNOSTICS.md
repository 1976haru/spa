# Shopify Sold Out 원인 진단

Shopify의 **Sold out 표시는 Amazon/source 품절을 자동으로 뜻하지 않습니다.** Shopify inventory tracking, available quantity, inventory policy, publication 또는 상품 상태 때문에 같은 표시가 생길 수 있습니다.

진단은 source snapshot과 Shopify의 읽기 전용 `availableForSale`, quantity, tracked, policy, product/publication 상태를 나란히 비교합니다. Source가 IN_STOCK인데 Shopify 수량이 0이면 Shopify 설정 문제 가능성으로, source가 OOS인데 Shopify가 판매 가능하면 oversale 위험으로 표시합니다. 증거가 오래됐거나 없으면 확정 표현 대신 UNKNOWN/STALE로 남깁니다.

진단은 Shopify mutation이나 inventory write를 수행하지 않습니다.

