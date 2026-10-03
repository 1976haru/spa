# Cabin Tidy 통제형 LIVE PILOT

Phase 4.2는 자동 배포 기능이 아니라, 첫 실제 Shopify 변경을 작은 범위에서 확인하기 위한 안전 절차입니다. 프로그램이나 미리보기를 여는 것만으로는 Shopify가 변경되지 않습니다.

## 고정된 gate 순서

1. **GATE A — READ-ONLY PREFLIGHT**: `001 | Cabin Tidy`, 예상 domain, credential 존재, 인증, API 버전, 상품 권한과 현재 스토어 상태를 읽기 전용으로 검사합니다.
2. **GATE B — 상품 미리보기**: Phase 3.6의 결정적 selector로 최대 10개를 고릅니다. 유효한 ShopSource 판매가가 없는 상품, 제한/보관 상품, 안전하지 않은 variant는 제외합니다.
3. **GATE C — 상품 write**: 화면에서 `LIVE PILOT`을 선택하고 확인 대화상자의 `실행`을 직접 눌러야 합니다. 상품은 DRAFT, 이미지는 OFF, 안전한 inventory 전략이 없으면 UNMANAGED입니다.
4. **GATE D — 원격 검증**: Shopify API로 ID, ASIN mapping, 제목, DRAFT, 판매가, 태그, variant와 중복 여부를 재확인합니다. mutation 성공 응답만으로 성공 처리하지 않습니다.
5. **GATE E — 컬렉션**: 상품 검증 성공 뒤 최대 3개를 미리봅니다. 기존 컬렉션은 NO_CHANGE/UPDATE/CONFLICT/SAFE ADOPT로 판단하며 별도 확인이 필요합니다.
6. **GATE F 이후**: Shop branch만 다루는 Navigation, 기존 Logo/Favicon 검사, Hero/Category Shortcut 미리보기, 조건부 Theme 적용, StoreCompletion 재검사를 순서대로 진행합니다.

## 즉시 중단 조건

다른 domain, 인증/권한 실패, identity mismatch, 중복 상품, 예상하지 않은 mutation, schema 비호환, stale preview, theme drift, secret 노출, 보호 파일 변경, checkpoint/report 저장 실패가 발견되면 `STOPPED`를 DB에 저장합니다. 중단된 run은 다음 gate로 자동 진행하지 않습니다.

## Theme 및 정책 안전

Theme 적용에는 published theme, 최신 preview, high-confidence schema mapping, backup, 승인된 이미지, 사용자 별도 확인이 모두 필요합니다. 하나라도 없으면 `MANUAL_ACTION_REQUIRED`입니다. 브라우저 클릭으로 우회하지 않습니다.

지원 이메일, 사업자명, 주소, 반품 기간/주소, 처리·배송 시간, 배송비, 전화번호, 준거법을 추측하지 않습니다. 누락 시 `REQUIRES_BUSINESS_INPUT`으로 남기고 정책을 publish하지 않습니다. Shipping, Tax, Payment, Domain은 Phase 4.2에서 읽기 전용입니다.

## 보고서와 rollback

보고서는 `exports/live_pilot_reports/001_cabin_tidy/<run_id>/` 아래에 gate별 JSON, 수동 작업, rollback manifest, 요약으로 생성됩니다. secret은 redaction합니다. 상품과 컬렉션을 자동 DELETE하지 않으며, 생성된 remote ID는 수동 정리를 위해 manifest에 남깁니다. Menu/Theme의 안전한 이전값 복원도 별도 사용자 확인이 필요합니다.

## 사용자가 화면에서 하는 일

1. Cabin Tidy 스토어를 선택합니다.
2. `GATE A · READ-ONLY PREFLIGHT`를 누릅니다.
3. 결과와 domain을 확인하고 `GATE B · 10개 DRAFT 미리보기`를 누릅니다.
4. ASIN, 판매가, 상태, 작업, 경고를 모두 확인합니다.
5. 준비가 되었을 때만 `LIVE PILOT 선택`을 체크합니다.
6. `GATE C · 10개 실제 업로드`를 누른 뒤 대화상자에서 `실행`을 직접 누릅니다.
7. `GATE D · Shopify API 재조회 검증`이 VERIFIED인지 확인합니다.
8. VERIFIED인 경우에만 `GATE E · 최대 3개 컬렉션 미리보기`로 이동합니다.

