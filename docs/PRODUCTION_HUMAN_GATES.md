# 실전 증거 점검의 사람 확인 단계

이 문서는 Cabin Tidy 실전 점검에서 프로그램이 확인할 수 있는 항목과 사람이 직접 결정해야 하는 항목을 구분합니다. 모든 확인은 현재 production run에 저장되며 Shopify 변경은 하지 않습니다.

## G2 원본 재고·가격

미리보기로 대상 수와 batch/token 수를 확인한 뒤 사용자가 **실제 Source 안전검사 승인**을 눌러야 provider 요청이 시작됩니다. Keepa 기본 Product 요청은 ASIN당 1 token으로 추정하고 offers/stock 추가 파라미터는 요청하지 않습니다. 따라서 화면에서 현금 비용을 계산할 수 없으면 비용을 임의로 표시하지 않습니다 ([Keepa Product Request token rules](https://keepa.com/api-docs/product.html)). 승인 후 내부적으로 100개 단위 checkpoint로 진행합니다. Provider 장애는 `SOURCE_ERROR`로 남고 품절로 바뀌지 않습니다. CAPTCHA/challenge 우회는 지원하지 않습니다. Keepa의 공식 `stats.current`에서 Marketplace NEW와 COUNT_NEW만 읽으며, product type/도메인/배열 증거가 불충분하면 `UNKNOWN`으로 남깁니다 ([Statistics Object price indices](https://keepa.com/api-docs/statistics-object.html)). offers 또는 stock 데이터를 더 요청하지 않습니다.

## G4 상품 이미지 사용 권리

상품을 직접 선택하고 `SUPPLIER_AUTHORIZED`, `MERCHANT_OWNED`, `LICENSED`, `MANUAL_REVIEW_REQUIRED`, `NO_RIGHTS_CONFIRMED` 중 사실에 맞는 결과를 기록합니다. 선택한 대상과 명시적 저장 동작이 필요하며 전체 자동 승인은 없습니다. 검토 시각과 메모가 저장됩니다. 일반 라이프스타일 생성 이미지는 실제 상품 사진의 사용 권리를 증명하지 않습니다.

## G5 가격·마진 정책

통화, 원가 buffer, 최소 마진, 미확인 수수료 처리, 가격 변경 경고 임계값을 운영자가 입력하고 확인합니다. ShopSource는 추천값을 승인된 정책으로 간주하지 않습니다. 자동 재가격은 이 흐름에서 허용하지 않습니다.

## G11 페이지·정책

Shopify Admin GraphQL `pages` 및 `Shop.shopPolicies`에서 현재 연결에 노출되는 제목/handle/본문을 읽습니다 ([pages query](https://shopify.dev/docs/api/admin-graphql/latest/queries/pages), [ShopPolicy object](https://shopify.dev/docs/api/admin-graphql/latest/objects/shoppolicy)). 실제 실행은 해당 연결에 고정된 API version과 granted scope를 사용합니다. 존재만으로 법률 적합성이나 사업정보의 정확성을 판정하지 않습니다. 사용자가 각 페이지 내용을 직접 확인해야 하며 주소, 전화, 이메일, 반품 기간, 배송 약속을 ShopSource가 만들어 넣지 않습니다. API scope나 노출이 없는 항목은 수동 검증 상태로 유지됩니다.

## G12 데스크톱·모바일 검수

자동 점검은 기본 SEO와 명백한 누락을 찾는 보조 수단입니다. 운영자는 현재 theme fingerprint에 대해 데스크톱, 모바일, Hero crop, 카테고리 카드, 메뉴, footer, 가독성을 직접 확인하고 sign-off 합니다. theme나 homepage 증거가 달라지면 이전 sign-off는 재사용하지 않습니다. 자동으로 WCAG 완전 준수를 선언하지 않습니다.

## G13 판매 설정

Shopify API로 확실히 읽을 수 있는 통화/primary domain/SSL 등의 증거와, Shopify 관리자에서 확인해야 하는 시장·배송·세금·결제·checkout·password 상태를 분리 저장합니다. 수동 확인도 사용자가 직접 확인한 사실에 대한 기록일 뿐 설정을 변경하지 않습니다.

## 파일럿 경계

G0~G13이 증거 기준으로 완료되면 상태는 `READY_FOR_PILOT`입니다. 이 상태는 출시 완료가 아니며 G14의 실제 상품 write를 실행하지 않습니다. 10개 DRAFT 파일럿은 별도 사용자 지시와 별도 화면 확인이 필요합니다.
