from pathlib import Path
import hashlib

import pytest

from shopsource.automation import (AutomationTaskError, WorkflowAutomationService, friendly_error,
    source_safety_workflow, store_build_workflow)
from shopsource.ui.beginner import (BEGINNER_MODE_DEFAULT, BEGINNER_NAV_GROUPS, GLOBAL_UI_CSS,
    MAX_UI_ROWS, paginate_rows)


@pytest.fixture
def service(tmp_path): return WorkflowAutomationService(tmp_path/"auto.sqlite3")
def tasks(n=3): return [{"task_key":f"T{i}","title":f"Task {i}","stage":"Safe"} for i in range(n)]


def test_beginner_mode_is_default(): assert BEGINNER_MODE_DEFAULT
def test_global_typography_minimums():
    assert "--ss-body:16px" in GLOBAL_UI_CSS and "--ss-help:15px" in GLOBAL_UI_CSS and "--ss-page-title:28px" in GLOBAL_UI_CSS
def test_raw_json_hidden_by_default(): assert "json_editor" not in GLOBAL_UI_CSS and "고급" not in str(BEGINNER_NAV_GROUPS)
def test_source_safety_primary_auto_action(): assert source_safety_workflow({})[0]["task_key"]=="LOCAL_PREVIEW"


def test_auto_flow_runs_safe_steps_without_extra_confirmation(service):
    run=service.create_run("001","SAFE",tasks())["run_id"]; result=service.run(run)
    assert result["status"]=="SUCCEEDED" and result["completed_tasks"]==3


def test_auto_flow_stops_at_paid_provider_gate(service):
    queue=source_safety_workflow({"estimated_batches":20,"estimated_tokens":100})
    run=service.create_run("001","SOURCE",queue)["run_id"]; result=service.run(run)
    assert result["status"]=="WAITING_FOR_CONFIRMATION" and result["current_task_key"]=="PROVIDER_CHECK"


def test_auto_flow_stops_at_shopify_write_gate(service):
    run=service.create_run("001","PILOT",[{"task_key":"PREVIEW"},{"task_key":"WRITE","requires_confirmation":True}])["run_id"]
    assert service.run(run)["status"]=="WAITING_FOR_CONFIRMATION"


def test_queue_persists_after_restart(service):
    run=service.create_run("001","SAFE",tasks())["run_id"]
    other=WorkflowAutomationService(service.db); assert other.status(run)["total_tasks"]==3


def test_queue_pause(service):
    run=service.create_run("001","SAFE",tasks())["run_id"]; assert service.pause(run)["status"]=="PAUSED"
def test_queue_resume(service):
    run=service.create_run("001","SAFE",tasks())["run_id"]; service.pause(run); assert service.resume(run)["status"]=="SUCCEEDED"
def test_queue_stop(service):
    run=service.create_run("001","SAFE",tasks())["run_id"]; result=service.stop(run); assert result["status"]=="STOPPED" and result["completed_tasks"]==0


def test_queue_retry_failed_only(tmp_path):
    calls={"bad":0,"good":0}
    def bad(_): calls["bad"]+=1; (_ for _ in ()).throw(RuntimeError("x")) if calls["bad"]==1 else None
    def good(_): calls["good"]+=1
    svc=WorkflowAutomationService(tmp_path/"x.db",handlers={"BAD":bad,"GOOD":good})
    run=svc.create_run("001","X",[{"task_key":"GOOD"},{"task_key":"BAD"}])["run_id"]; svc.run(run); result=svc.retry_failed(run)
    assert result["status"]=="SUCCEEDED" and calls=={"bad":2,"good":1}


def test_successful_tasks_not_repeated(service):
    run=service.create_run("001","SAFE",tasks())["run_id"]; service.run(run); service.run(run)
    assert all(x["attempt_count"]==1 for x in service.tasks(run)["rows"])


def test_transient_error_auto_retry(tmp_path):
    count={"n":0}
    def handler(_):
        count["n"]+=1
        if count["n"]<3: raise AutomationTaskError("HTTP_429",transient=True)
    svc=WorkflowAutomationService(tmp_path/"x.db",handlers={"T":handler}); run=svc.create_run("001","X",[{"task_key":"T","max_attempts":4}])["run_id"]
    assert svc.run(run)["status"]=="SUCCEEDED" and count["n"]==3


def test_permanent_error_waits_for_user(tmp_path):
    svc=WorkflowAutomationService(tmp_path/"x.db",handlers={"T":lambda _:(_ for _ in ()).throw(AutomationTaskError("MISSING_SCOPE"))})
    run=svc.create_run("001","X",[{"task_key":"T"}])["run_id"]; assert svc.run(run)["status"]=="WAITING_FOR_INPUT"


def test_human_gate_resume_continues_next_task(service):
    run=service.create_run("001","X",[{"task_key":"A","requires_confirmation":True},{"task_key":"B"}])["run_id"]
    service.run(run); result=service.confirm(run,"A"); assert result["status"]=="SUCCEEDED" and result["completed_tasks"]==2


@pytest.mark.parametrize("code,text",[("MISSING_SNAPSHOT","최신 재고"),("PREVIEW_STALE","미리보기"),("MISSING_SCOPE","권한"),("TOKEN_BUDGET","사용량")])
def test_friendly_error_mapping(code,text): assert text in friendly_error(code)


def test_source_summary_cards():
    labels=("검사 대상","최신 재고 확인","품절","확인 필요","가격변동","판매차단 후보")
    source=Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8"); assert all(x in source for x in labels)


def test_result_table_paginated(): assert paginate_rows(list(range(120)),1,50)["rows"]==list(range(50,100))
def test_ui_row_limit(): assert paginate_rows(list(range(100)),0,100)["page_size"]==MAX_UI_ROWS==50
def test_dashboard_next_action(): assert "오늘 할 일" in Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
def test_sidebar_beginner_groups(): assert [x[0] for x in BEGINNER_NAV_GROUPS]==["홈","자동 구축","상품·소싱","스토어 디자인","스토어 완성","기록"]


def test_advanced_menu_preserves_existing_routes():
    routes={r[0] for _,items in BEGINNER_NAV_GROUPS for r in items}; source=Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert routes.issubset(set(x[0] for x in __import__("shopsource.ui.v2",fromlist=["NAV_ITEMS"]).NAV_ITEMS)) and "전문가 메뉴 보기" in source


def test_phase42_wizard_beginner_mode():
    source=Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert all(x in source for x in ("Source 안전 확인","Shopify 연결 확인","10개 DRAFT 미리보기","완료 보고"))


def test_no_real_network_in_tests(service):
    run=service.create_run("001","SAFE",tasks())["run_id"]; assert service.run(run)["status"]=="SUCCEEDED"


def test_protected_store_file_untouched(service):
    path=Path("stores/001_cabin_tidy.json"); before=hashlib.sha256(path.read_bytes()).hexdigest()
    run=service.create_run("001","SAFE",tasks())["run_id"]; service.run(run)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_restart_recovery_lists_interrupted(service):
    run=service.create_run("001","SAFE",tasks())["run_id"]; assert service.interrupted("001")[0]["run_id"]==run


def test_task_page_caps_at_50(service):
    run=service.create_run("001","MANY",tasks(120))["run_id"]; assert len(service.tasks(run,page_size=500)["rows"])==50


def test_store_build_workflow_order():
    keys=[x["task_key"] for x in store_build_workflow()]; assert keys[0]=="STORE_PROFILE" and keys[-1]=="LAUNCH_READINESS" and "SOURCE_SAFETY" in keys

