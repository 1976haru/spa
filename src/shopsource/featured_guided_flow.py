"""Pure state resolver for the beginner Featured Products guided workflow."""

from __future__ import annotations


FEATURED_GUIDED_STEP_LABELS = (
    "상품 불러오기",
    "상품 4개 확인",
    "미리보기 만들기",
    "적용 전 안전 검사",
    "Shopify 적용",
    "스토어 확인",
    "과제 제출 확인",
)


def _step(number: int, status: str) -> dict:
    return {"number": number, "label": FEATURED_GUIDED_STEP_LABELS[number - 1], "status": status}


def resolve_featured_guided_state(state: dict | None, plan: dict | None, preview: dict | None,
                                  preflight_result: dict | None,
                                  last_apply_result: dict | None) -> dict:
    """Resolve the single safe next action without performing any I/O."""
    state = state or {}
    plan = plan or {}
    preview = preview or {}
    preflight_result = preflight_result or {}
    last_apply_result = last_apply_result or {}
    plan_id = plan.get("plan_id")
    items = plan.get("items") or []
    plan_ready = (plan.get("status") in {"READY", "REMOTE_JSON_VERIFIED", "STOREFRONT_VERIFIED"}
                  and len(items) == 4)
    steps = [_step(number, "waiting") for number in range(1, 8)]

    def finish(current_step: int, action: str, label: str, help_text: str,
               problem_code: str | None = None, can_advance: bool = True) -> dict:
        if steps[current_step - 1]["status"] == "waiting":
            steps[current_step - 1]["status"] = "problem" if problem_code else "current"
        return {"current_step": current_step, "steps": steps, "primary_action": action,
                "primary_label": label, "primary_help": help_text,
                "problem_code": problem_code, "can_advance": can_advance}

    if not plan_ready:
        steps[0]["status"] = "problem" if state.get("featured_load_problem") else "current"
        return {"current_step": 1, "steps": steps, "primary_action": "REFRESH_PRODUCTS",
                "primary_label": "1단계 · Shopify 상품 불러오기",
                "primary_help": "스토어에서 추천 상품 후보를 확인합니다. 아직 Shopify 테마는 변경하지 않습니다.",
                "problem_code": state.get("featured_load_problem"), "can_advance": True}
    steps[0]["status"] = "complete"

    reviewed = state.get("featured_products_reviewed_plan_id") == plan_id
    if not reviewed:
        return finish(2, "REVIEW_PRODUCTS", "2단계 · 상품 4개 확인",
                      "상품 네 개와 가격·이미지를 확인하고 완료를 눌러 주세요.")
    steps[1]["status"] = "complete"

    preview_current = (preview.get("status") == "PREVIEW" and preview.get("plan_id") == plan_id)
    preflight_matches = (
        state.get("featured_preflight_plan_id") == plan_id
        and state.get("featured_preflight_preview_hash") == preview.get("preview_hash")
    )
    apply_plan_matches = state.get("featured_last_apply_plan_id") == plan_id
    if (apply_plan_matches
            and last_apply_result.get("status") not in {None, "REMOTE_JSON_VERIFIED"}):
        current_result = last_apply_result
    else:
        current_result = preflight_result if preflight_matches else {}
    preflight_code = current_result.get("reason_code")
    if preflight_code == "REMOTE_HASH_DRIFT_SEMANTIC":
        steps[2]["status"] = "problem"
        return finish(3, "BUILD_PREVIEW", "3단계 · 최신 테마로 미리보기 다시 만들기",
                      "미리보기 이후 테마 내용이 바뀌었습니다. 최신 상태를 다시 읽어 미리보기를 만듭니다.",
                      problem_code=preflight_code)
    if not preview_current:
        return finish(3, "BUILD_PREVIEW", "3단계 · 추천 상품 미리보기 만들기",
                      "선택한 상품 네 개를 현재 홈페이지 제안에 안전하게 반영해 봅니다.")
    steps[2]["status"] = "complete"

    active_result = current_result
    code = active_result.get("reason_code")
    if not active_result:
        return finish(4, "RUN_PREFLIGHT", "4단계 · 적용 전 안전 검사",
                      "MAIN 테마와 상품 구성이 맞는지 읽기 전용으로 확인합니다. Shopify는 변경하지 않습니다.")
    if active_result.get("status") == "READY_TO_APPLY":
        steps[3]["status"] = "complete"
    elif code == "REMOTE_READ_FAILED":
        return finish(4, "RECHECK_THEME_CONNECTION", "4단계 · 연결 상태 다시 확인",
                      "Theme Access로 현재 MAIN 테마 파일을 읽을 수 있는지 다시 확인합니다. Shopify는 변경하지 않습니다.",
                      problem_code=code)
    elif code == "REMOTE_HASH_DRIFT_RAW_ONLY":
        return finish(4, "NONE", "파일 형식 차이 확인 필요",
                      "안전 검사에서 파일 형식 차이를 발견했습니다. 고급 / 문제 해결의 정보를 확인하세요. 적용은 진행할 수 없습니다.",
                      problem_code=code, can_advance=False)
    elif code == "REMOTE_HASH_DRIFT_SEMANTIC":
        steps[2]["status"] = "problem"
        steps[3]["status"] = "waiting"
        return finish(3, "BUILD_PREVIEW", "3단계 · 최신 테마로 미리보기 다시 만들기",
                      "미리보기 이후 테마 내용이 바뀌었습니다. 최신 상태를 다시 읽어 미리보기를 만듭니다.",
                      problem_code=code)
    else:
        return finish(4, "RUN_PREFLIGHT", "4단계 · 안전 검사 다시 실행",
                      active_result.get("human_message") or "읽기 전용 안전 검사를 다시 실행해 원인을 확인합니다.",
                      problem_code=code)

    applied = (apply_plan_matches and last_apply_result.get("status") == "REMOTE_JSON_VERIFIED")
    if not applied:
        applied = plan.get("status") == "REMOTE_JSON_VERIFIED"
    if not applied:
        return finish(5, "OPEN_APPLY_GATE", "5단계 · Shopify에 추천 상품 4개 적용",
                      "다음 버튼은 기존 확인 화면만 엽니다. 두 확인란을 직접 선택하고 최종 적용을 눌러야 합니다.")
    steps[4]["status"] = "complete"

    storefront_verified = bool(plan.get("storefront_verified"))
    if not storefront_verified:
        return finish(6, "OPEN_STOREFRONT", "6단계 · 스토어에서 확인",
                      "새 탭에서 스토어를 열어 New Arrivals 영역과 상품 링크를 직접 확인합니다.")
    steps[5]["status"] = "complete"

    assignment_status = state.get("featured_assignment_status")
    if assignment_status != "ASSIGNMENT_READY":
        return finish(7, "OPEN_ASSIGNMENT_CHECK", "7단계 · 과제 제출용 확인",
                      "스토어에서 확인한 결과와 제출 체크 항목을 저장합니다.")
    steps[6]["status"] = "complete"
    return {"current_step": 7, "steps": steps, "primary_action": "NONE",
            "primary_label": "모든 단계 완료", "primary_help": "추천 상품 준비와 스토어 확인이 모두 완료되었습니다.",
            "problem_code": None, "can_advance": False}
