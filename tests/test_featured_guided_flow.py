from pathlib import Path

from shopsource.featured_guided_flow import resolve_featured_guided_state


def _plan(plan_id="plan-1", status="READY", *, storefront_verified=False):
    return {"plan_id": plan_id, "status": status, "requested_count": 4,
            "items": [{"position": number, "remote_status": "ACTIVE"} for number in range(1, 5)],
            "storefront_verified": storefront_verified}


def _reviewed_state(plan_id="plan-1"):
    return {"featured_products_reviewed_plan_id": plan_id}


def _preview(plan_id="plan-1", preview_hash="preview-1"):
    return {"status": "PREVIEW", "plan_id": plan_id, "preview_hash": preview_hash}


def _preflight(state, plan, preview, *, status="READY_TO_APPLY", reason_code="READY_TO_APPLY"):
    state = dict(state)
    state["featured_preflight_plan_id"] = plan["plan_id"]
    state["featured_preflight_preview_hash"] = preview["preview_hash"]
    return state, {"status": status, "reason_code": reason_code}


def test_guided_flow_starts_with_product_load_then_review():
    empty = resolve_featured_guided_state({}, None, None, None, None)
    assert (empty["current_step"], empty["primary_action"]) == (1, "REFRESH_PRODUCTS")

    plan = _plan()
    flow = resolve_featured_guided_state({}, plan, None, None, None)
    assert (flow["current_step"], flow["primary_action"]) == (2, "REVIEW_PRODUCTS")
    assert flow["steps"][0]["status"] == "complete"


def test_guided_flow_review_preview_and_preflight_progression():
    plan, state = _plan(), _reviewed_state()
    flow = resolve_featured_guided_state(state, plan, None, None, None)
    assert (flow["current_step"], flow["primary_action"]) == (3, "BUILD_PREVIEW")

    preview = _preview()
    flow = resolve_featured_guided_state(state, plan, preview, None, None)
    assert (flow["current_step"], flow["primary_action"]) == (4, "RUN_PREFLIGHT")

    state, result = _preflight(state, plan, preview)
    flow = resolve_featured_guided_state(state, plan, preview, result, None)
    assert (flow["current_step"], flow["primary_action"]) == (5, "OPEN_APPLY_GATE")


def test_remote_read_failure_has_one_read_only_recheck_action():
    plan, preview = _plan(), _preview()
    state, result = _preflight(_reviewed_state(), plan, preview,
                               status="READ_FAILED", reason_code="REMOTE_READ_FAILED")
    flow = resolve_featured_guided_state(state, plan, preview, result, None)
    assert flow["current_step"] == 4
    assert flow["primary_action"] == "RECHECK_THEME_CONNECTION"
    assert flow["primary_label"] == "4단계 · 연결 상태 다시 확인"
    assert flow["problem_code"] == "REMOTE_READ_FAILED"
    assert "Shopify는 변경하지 않습니다" in flow["primary_help"]


def test_raw_and_semantic_mismatch_never_suggest_apply():
    plan, preview = _plan(), _preview()
    state, raw = _preflight(_reviewed_state(), plan, preview,
                            status="CONFLICT", reason_code="REMOTE_HASH_DRIFT_RAW_ONLY")
    flow = resolve_featured_guided_state(state, plan, preview, raw, None)
    assert flow["current_step"] == 4
    assert flow["primary_action"] == "NONE"
    assert flow["steps"][3]["status"] == "problem"
    assert flow["primary_action"] != "OPEN_APPLY_GATE"

    state, semantic = _preflight(_reviewed_state(), plan, preview,
                                 status="CONFLICT", reason_code="REMOTE_HASH_DRIFT_SEMANTIC")
    flow = resolve_featured_guided_state(state, plan, preview, semantic, None)
    assert flow["current_step"] == 3
    assert flow["primary_action"] == "BUILD_PREVIEW"
    assert flow["steps"][2]["status"] == "problem"


def test_verified_apply_storefront_and_assignment_steps():
    plan, preview = _plan(), _preview()
    state = _reviewed_state()
    state, preflight = _preflight(state, plan, preview)
    last_apply = {"status": "REMOTE_JSON_VERIFIED"}
    state["featured_last_apply_plan_id"] = plan["plan_id"]
    flow = resolve_featured_guided_state(state, plan, preview, preflight, last_apply)
    assert (flow["current_step"], flow["primary_action"]) == (6, "OPEN_STOREFRONT")

    verified_plan = _plan(status="STOREFRONT_VERIFIED", storefront_verified=True)
    state["featured_assignment_status"] = None
    flow = resolve_featured_guided_state(state, verified_plan, preview, preflight, last_apply)
    assert (flow["current_step"], flow["primary_action"]) == (7, "OPEN_ASSIGNMENT_CHECK")

    state["featured_assignment_status"] = "ASSIGNMENT_READY"
    flow = resolve_featured_guided_state(state, verified_plan, preview, preflight, last_apply)
    assert flow["primary_action"] == "NONE"
    assert all(step["status"] == "complete" for step in flow["steps"])

    state["featured_assignment_status"] = "STOREFRONT_VERIFIED"
    flow = resolve_featured_guided_state(state, verified_plan, preview, preflight, last_apply)
    assert (flow["current_step"], flow["primary_action"]) == (7, "OPEN_ASSIGNMENT_CHECK")


def test_step6_storefront_confirmation_advances_without_completing_assignment():
    plan, preview = _plan(), _preview()
    state, preflight = _preflight(_reviewed_state(), plan, preview)
    last_apply = {"status": "REMOTE_JSON_VERIFIED"}
    state["featured_last_apply_plan_id"] = plan["plan_id"]

    before_confirmation = resolve_featured_guided_state(state, plan, preview, preflight, last_apply)
    assert (before_confirmation["current_step"], before_confirmation["primary_action"]) == (6, "OPEN_STOREFRONT")

    # confirm_storefront() persists storefront_verified on the refreshed durable plan;
    # its status is not assignment completion and assignment remains unset.
    confirmed_plan = _plan(status="STOREFRONT_VERIFIED", storefront_verified=True)
    state["featured_storefront_status"] = "STOREFRONT_VERIFIED"
    state["featured_assignment_status"] = None
    after_confirmation = resolve_featured_guided_state(state, confirmed_plan, preview, preflight, last_apply)
    assert (after_confirmation["current_step"], after_confirmation["primary_action"]) == (7, "OPEN_ASSIGNMENT_CHECK")
    assert after_confirmation["steps"][5]["status"] == "complete"
    assert after_confirmation["steps"][6]["status"] == "current"


def test_reselection_and_new_preview_invalidate_old_progress():
    old_plan, new_plan = _plan("old"), _plan("new")
    old_preview = _preview("old", "old-preview")
    state, old_preflight = _preflight(_reviewed_state("old"), old_plan, old_preview)

    # A new plan id invalidates review, preview, and preflight from the old selection.
    flow = resolve_featured_guided_state(state, new_plan, old_preview, old_preflight, None)
    assert (flow["current_step"], flow["primary_action"]) == (2, "REVIEW_PRODUCTS")

    state["featured_products_reviewed_plan_id"] = "new"
    new_preview = _preview("new", "new-preview")
    # Even if the old READY result is still held in memory, its preview hash cannot advance.
    flow = resolve_featured_guided_state(state, new_plan, new_preview, old_preflight, None)
    assert (flow["current_step"], flow["primary_action"]) == (4, "RUN_PREFLIGHT")


def test_guided_ui_has_one_primary_route_and_keeps_recovery_advanced():
    source = (Path(__file__).resolve().parents[1] / "src" / "shopsource" / "ui" / "v2.py").read_text(encoding="utf-8")
    assert 'ui.expansion("고급 / 문제 해결"' in source
    assert 'ui.button("Shopify 상품 다시 읽기"' in source
    assert 'ui.button("다시 선택"' in source
    assert 'ui.button("추천 상품 롤백"' in source
    assert 'ui.button("연결 상태 다시 확인"' in source
    assert 'ui.button("나중에 하기"' in source
    assert "Theme Access가 현재 MAIN 테마 파일을 읽는 과정에서 멈췄습니다" in source
    assert 'ui.button(flow["primary_label"], on_click=run_featured_primary_action' in source
    assert '"OPEN_APPLY_GATE": featured_apply_gate' in source
    assert '"OPEN_STOREFRONT": featured_storefront_check' in source
    assert '"OPEN_ASSIGNMENT_CHECK": featured_assignment_check' in source
    assert "featured_preflight_plan_id" in source and "featured_preflight_preview_hash" in source

    save_handler = source.split("def save_checks():", 1)[1].split('ui.button("확인 저장"', 1)[0]
    assert 'state["featured_storefront_status"] = confirmation.get("status")' in save_handler
    assert 'state["featured_assignment_status"] = None' in save_handler
    assert '"스토어 확인 완료. 다음은 7단계 과제 제출 확인입니다."' in save_handler
    assert '"스토어 확인 항목이 아직 남아 있습니다."' in save_handler
    assert '"추천 상품 상태: " +' not in save_handler

    # The guided dispatcher opens the old gate; only the gate callback calls apply.
    dispatcher = source.split("def run_featured_primary_action():", 1)[1].split("async def save_hero_upload", 1)[0]
    assert "FeaturedProductThemeApplyService().apply" not in dispatcher
    assert "FeaturedProductThemeApplyService().preflight" not in dispatcher
    assert "rollback_featured" not in dispatcher


def test_recheck_is_read_only_and_apply_gate_keeps_both_explicit_confirmations():
    import ast

    source = (Path(__file__).resolve().parents[1] / "src" / "shopsource" / "ui" / "v2.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    preflight = ast.get_source_segment(source, functions["execute_featured_preflight"])
    recheck = ast.get_source_segment(source, functions["featured_recheck_connection"])
    dispatch = ast.get_source_segment(source, functions["run_featured_primary_action"])
    gate = ast.get_source_segment(source, functions["featured_apply_gate"])

    assert "FeaturedProductThemeApplyService().preflight" in preflight and "allow_live=False" in preflight
    for unsafe in ("FeaturedProductThemeApplyService().apply", "ThemeWriteBackendRouter.write",
                   "themeFilesUpsert", "shopify theme push", ".rollback"):
        assert unsafe not in preflight and unsafe not in recheck and unsafe not in dispatch
    assert "execute_featured_preflight()" in recheck
    assert '"OPEN_APPLY_GATE": featured_apply_gate' in dispatch
    assert "추천 상품 section만 Shopify 테마에 적용하는 것을 확인했습니다." in gate
    assert "I confirm this targets the current MAIN theme and is a live Shopify theme write." in gate
    assert "confirmed.value" in gate and "live_confirmed.value" in gate
    assert source.count("FeaturedProductThemeApplyService().preflight") == 1
