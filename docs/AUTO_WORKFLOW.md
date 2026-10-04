# AUTO Workflow

자동 모드의 queue는 DB에 저장되므로 앱을 닫아도 사라지지 않습니다. 각 task는 `QUEUED`, `RUNNING`, `PAUSED`, `WAITING_FOR_CONFIRMATION`, `WAITING_FOR_INPUT`, `SUCCEEDED`, `FAILED`, `STOPPED` 등의 상태를 갖습니다. 확인 대기는 실패가 아닙니다.

안전한 단계는 추가 질문 없이 다음 단계로 이동합니다. 유료 provider 호출 또는 실제 외부 변경은 한 번의 명확한 확인을 기다립니다. 승인 후에는 처음부터 다시 시작하지 않고 해당 checkpoint에서 이어서 실행합니다.

timeout, 연결 중단, 429와 일시적인 server/provider 오류는 설정된 횟수 안에서만 자동 재시도합니다. credential, scope, CAPTCHA, 사업자 입력과 conflict는 반복하지 않고 사람이 확인하도록 멈춥니다. stack trace는 기본 화면에 노출하지 않습니다.

Automation Settings에서 자동 모드, 자동 재시도, 최대 횟수, 다음 안전 단계 자동 진행, 재시작 안내와 provider token limit을 변경할 수 있습니다.

홈페이지 자동 workflow는 `HERO → CATEGORY_SHORTCUTS → FEATURED_COLLECTIONS → LINK_CHECK → THEME_PREVIEW`까지 자동 처리한 후 `THEME_WRITE`에서 한 번만 확인을 기다립니다. 승인 뒤에는 원격 검증과 과제 제출용 확인으로 계속됩니다. 외부 페이지 빌더만 가능한 경우에는 write task를 만들지 않고 수동 입력 대기 상태로 멈춥니다.
