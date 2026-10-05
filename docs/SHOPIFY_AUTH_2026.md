# Shopify 연결 (2026 인증 방식)

ShopSource는 두 가지 Shopify 연결 방식을 지원합니다.

- **Dev Dashboard 앱 (권장)**: Client ID와 Client Secret을 Windows Credential Manager에 보관합니다. ShopSource가 access token을 요청하고, 만료가 가까워지면 갱신합니다.
- **기존 Legacy 토큰**: 2026년 이전 Shopify Admin에서 만든 기존 custom app의 Admin API access token을 사용합니다. 기존 자격 증명은 삭제하거나 자동 변환하지 않습니다.

## Dev Dashboard 연결하기

1. Shopify Dev Dashboard에서 사용할 앱을 열고 앱 자격 증명 화면에서 Client ID와 Client Secret을 확인합니다.
2. ShopSource에서 **Shopify 연결 방식 → Dev Dashboard 앱 (권장)**을 고릅니다.
3. 스토어의 `*.myshopify.com` 도메인, Client ID, Client Secret을 입력하고 저장합니다. Client Secret을 채팅, 문서, 스크린샷에 붙여 넣지 마세요.
4. **Shopify 연결 / 권한 확인**을 누릅니다. ShopSource는 token endpoint와 Admin API를 읽기 전용으로 확인합니다. 이 확인 단계는 상품·테마를 수정하지 않습니다.

스토어의 정확한 `myshopify.com` 도메인은 Shopify Admin의 스토어 도메인 설정에서 확인하세요. URL의 `https://`와 뒤쪽 경로는 제외합니다.

새 Dev Dashboard 앱은 Admin에서 고정 token을 복사하는 방식이 아닙니다. Client Credentials grant는 같은 Shopify 조직의 앱과 스토어 조합에서만 허용됩니다. `shop_not_permitted`가 나오면 Dev Dashboard에서 앱과 대상 스토어가 같은 조직에 있는지, 앱이 해당 스토어에 설치되어 있는지 확인하세요.

## Token과 scope

ShopSource는 token을 메모리에만 보관하고 만료 약 5분 전부터 새 token을 요청합니다. 앱을 다시 시작한 뒤에는 안전하게 보관된 Client ID/Secret으로 새 token을 발급받습니다. HTTP 401은 refresh 후 한 번만 재시도합니다.

연결 확인 화면에는 인증 방식, 자격 증명 존재 여부, token 상태, 대략적인 만료 시간, 마지막 확인, granted/missing scope를 표시합니다. Token과 secret 원문은 표시하지 않습니다.

권한은 앱의 Dev Dashboard 버전 설정에서 확인합니다. 누락된 읽기 권한이 있으면 새 앱 버전에 권한을 추가하고 Shopify가 요구하는 release/approval/install 과정을 완료한 뒤 다시 확인하세요. 쓰기 권한은 실제 쓰기 단계에서 별도로 요구되며 읽기 전용 G0 통과를 위해 미리 요구하지 않습니다.

## Legacy 토큰

기존 Admin-created custom app에서 발급받아 사용 중인 token이 있으면 **기존 Legacy 토큰**을 선택하세요. Token은 Windows Credential Manager에 저장되고 SQLite에는 저장되지 않습니다. 이전 연결에 `auth_mode`가 없으면 Legacy로 해석합니다.

## 보안

- Client Secret과 Legacy token은 OS Credential Manager에만 저장합니다.
- access token은 SQLite, 파일, 로그에 저장하지 않습니다. SQLite에는 만료 시각 같은 비밀이 아닌 상태 정보만 저장합니다.
- 자격 증명을 회전하려면 새 값을 입력해 다시 저장하세요. 저장된 인증정보 삭제 버튼은 현재 선택된 방식의 자격 증명만 삭제합니다.
- 이 UI에 실 secret을 붙여 넣은 후에도 원문을 다시 보여주지 않습니다. 실제 secret을 ChatGPT 대화나 일반 문서에 붙여 넣지 마세요.

공식 문서:

- [Authenticate an app for stores in your organization](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant)
- [Access tokens](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens)
- [Manage your app credentials](https://shopify.dev/docs/apps/build/authentication-authorization/manage-credentials)
