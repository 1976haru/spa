# 초보자 화면과 자동 실행

ShopSource의 기본 화면은 BEGINNER 모드입니다. 큰 글자와 넓은 카드로 “지금 할 일”, 진행률, 확인할 문제를 먼저 보여 줍니다. 내부 ID, GraphQL ID, snapshot ID, JSON과 기술 로그는 `고급 정보`를 열 때만 보입니다.

왼쪽 메뉴는 홈, 자동 구축, 상품·소싱, 스토어 디자인, 스토어 완성, 기록으로 묶었습니다. 이전 세부 화면은 삭제하지 않았으며 `전문가 메뉴 보기`에서 접근할 수 있습니다.

`자동 안전검사 시작` 또는 `자동 구축 시작`을 누르면 안전한 로컬 계산과 읽기 전용 검사는 계속 진행합니다. 실제 비용, Shopify write, Theme write, 외부 Spark 작업, 법률·사업자 판단, CAPTCHA, 권리 검토와 불확실한 충돌에서만 멈춥니다.

실제 Source Audit이나 Shopify write는 화면을 열거나 자동 버튼을 누른 것만으로 시작되지 않습니다.

## 홈페이지 과제 자동화

`홈페이지 자동 완성`은 메인 배너, 카테고리 바로가기, 추천 컬렉션, 링크 검사와 테마 미리보기를 차례로 준비합니다. 안전한 로컬 단계는 이어서 처리하지만 실제 Theme write는 미리보기 뒤 한 번의 명시적 승인을 기다립니다. `메인 배너 자동 만들기`, `카테고리 바로가기 자동 만들기`로 각 과제만 준비할 수도 있습니다.

카테고리는 실제 Shopify remote collection 매핑이 확인된 항목만 사용합니다. Collection List schema를 우선하고, 없으면 Multicolumn을 검토용 fallback으로 제안합니다. 같은 target을 서로 다른 카테고리에 잘못 재사용하면 적용을 차단하고, remote collection이 없으면 해당 shortcut을 건너뜁니다. 승인된 collection 이미지는 다시 사용합니다.

사용자 제공 완성형 배너는 `MANUAL_ASSET`으로 허용합니다. 이미지 안에 문자가 포함된 경우 mobile crop과 접근성 중복 문구를 사람이 검토해야 합니다. 새로 생성할 이미지는 무문자 이미지와 theme text overlay가 기본입니다.

PageFly 같은 외부 페이지 빌더는 GUI를 자동 클릭하지 않습니다. native theme schema로 안전하게 적용할 수 없으면 `EXTERNAL_PAGE_BUILDER_MANUAL` 또는 `UNSUPPORTED_MANUAL`로 표시하고 정확한 수동 경로를 안내합니다.

`과제 제출용 확인`은 Hero, 문구, CTA/실제 링크, 승인 이미지, 4개 이상의 shortcut, 이미지와 정확한 collection target, 빈 링크/잘못된 중복 target, desktop/mobile 확인을 쉬운 체크리스트로 보여 줍니다. 실제 적용 및 원격 검증 전에는 제출 준비 완료로 표시하지 않습니다.
