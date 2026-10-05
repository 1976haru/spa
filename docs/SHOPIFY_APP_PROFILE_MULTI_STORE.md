# Shopify Production App Profile / Multi-store 연결

ShopSource는 앱 자체(Shopify App Profile)와 설치된 개별 스토어(Shop connection)를 분리합니다. 앱 이름만으로 같은 앱이라고 판단하지 않습니다.

## 권위 있는 identity

Client Credentials token을 새로 발급받은 직후 read-only GraphQL `ShopSourceAppIdentity`를 실행합니다. 입력 Client ID는 Shopify 응답의 `app.apiKey`와 비교하고, 이미 연결된 프로필의 `app.id` 및 저장된 Store의 `shop.id`도 비교합니다. 어느 하나라도 불일치하면 `APP_IDENTITY_MISMATCH` 또는 `STORE_IDENTITY_MISMATCH`로 중단하며 token을 정상 연결로 표시하지 않습니다.

Client ID/Secret은 입력 즉시 저장되지 않습니다. token grant, 앱 identity, 도메인/Shop identity를 확인한 뒤에만 Client ID/Secret을 Windows Credential Manager의 app-profile 항목에 저장합니다. SQLite에는 Client ID fingerprint, App/Shop/Installation GID 및 승인 scope metadata만 저장합니다. 기존 per-store Credential Manager 항목은 읽거나 삭제/변환하지 않습니다.

새 스토어에서는 **기존 Production App 프로필 재사용**을 선택하고 shop domain만 입력하면 같은 앱 자격증명으로 별도 token grant 및 Store ID 확인을 합니다. 앱/조직에서 client-credentials grant를 허용하지 않으면 `SHOP_NOT_PERMITTED`가 `EXTERNAL_ORG_OAUTH_REQUIRED` 안내로 처리됩니다. Legacy Admin token 경로는 자동 fallback 되지 않습니다.

## Scope contract

`config/shopify_scope_contract.json`은 저장소 GraphQL query/mutation 정적 조사로 작성한 `2026-07.1` 계약입니다. 실제 선언과 설치 grant는 GraphQL readback이 기준입니다. G0는 현재 `read_themes`를 필요로 하며, write scope가 없다는 이유로 인증/G0 identity를 실패 처리하지 않습니다. `write_themes`는 Shopify exemption이 적용될 수 있어 일반 baseline 요구 권한에서 제외됩니다.

## Shopify CLI

기존 Dashboard 앱을 연결하는 안전 wrapper와 사용법은 `tools/shopify_app/`에 있습니다. `shopify app config link`는 사용자가 직접 선택해야 하고, `shopify app deploy`는 wrapper에서 확인 switch와 `DEPLOY` 직접 입력을 모두 요구합니다. CLI 로그인, 링크, deploy는 자동으로 실행하지 않았습니다.

## 현재 Cabin Tidy 상태 해석

이 코드 배포만으로 Cabin Tidy의 Production App 연결 또는 G0를 완료한 것으로 처리하지 않습니다. 현재 인증된 앱이 `hps-automation`이고 target profile이 미연결이면 UI는 `NOT BOUND` 및 실제 authenticated app identity를 표시해야 합니다. Production App을 검증/연결한 뒤 실제 grant에 `read_themes`가 있고 published MAIN theme read에 성공할 때까지 G0는 `WAITING_FOR_INPUT`입니다.
